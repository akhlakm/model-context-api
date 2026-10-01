import json
from types import SimpleNamespace
from typing import Any
from unittest import IsolatedAsyncioTestCase

from django.conf import settings

if not settings.configured:
    settings.configure(
        DEFAULT_CHARSET="utf-8",
        INSTALLED_APPS=[],
        SECRET_KEY="mca-mcp-test-key",
    )

import django

django.setup()

from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.test import RequestFactory, override_settings
from django.urls import path
from django.utils.deprecation import MiddlewareMixin
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError
from ninja import Body, NinjaAPI, Router

from mca.base import MCAError
from mca.mcp import MCPHost
from mca.ninja import NinjaMCARouter

middleware_events = []
operation_calls = []


class TrackingMiddleware(MiddlewareMixin):
    def process_request(self, request):
        middleware_events.append(("request", request.path))
        request.middleware_marker = "passed"
        if request.headers.get("X-Block"):
            return JsonResponse({"code": "blocked", "detail": "Rejected by middleware."}, status=403)
        if request.headers.get("X-Redirect"):
            return HttpResponseRedirect("/api/items/")
        if request.headers.get("X-Plain"):
            return HttpResponse("plain text")
        if request.headers.get("X-Empty"):
            return HttpResponse(status=204)
        return None

    def process_view(self, request, view_func, view_args, view_kwargs):
        middleware_events.append(("view", request.path))
        if request.headers.get("X-View-Block"):
            return JsonResponse({"code": "view_blocked"}, status=403)
        return None

    def process_response(self, request, response):
        middleware_events.append(("response", response.status_code))
        response["X-Middleware"] = "passed"
        return response


def authenticate(request):
    token = request.headers.get("X-MCP-Token")
    return token if token == "trusted-principal" else None


items_registry = NinjaMCARouter(Router())


@items_registry.register("/items", response=dict, auth=authenticate)
def get_items(request):
    operation_calls.append("get_items")
    return {
        "principal": request.auth,
        "middleware": getattr(request, "middleware_marker", None),
        "cookie": request.COOKIES.get("sessionid"),
        "client": request.META.get("REMOTE_ADDR"),
    }


@items_registry.register("/async-items/{item_id}", response=dict)
async def get_async_item(request, item_id: int):
    return {"item_id": item_id}


@items_registry.register("/query", response=dict)
def get_query(request):
    return {"tags": request.GET.getlist("tag"), "empty": request.GET.get("empty")}


@items_registry.register("/echo", response=dict)
def make_echo(request, payload: dict[str, Any] = Body(...)):
    return {"payload": payload, "tag": request.GET.get("tag")}


@items_registry.register("/error", response=dict)
def get_error(request):
    raise MCAError("item_missing", "The item does not exist.", status=404)


billing_registry = NinjaMCARouter(Router())


@billing_registry.register("/invoices", response=dict)
def get_invoices(request):
    return {"api": "billing"}


api = NinjaAPI(title="MCP test API", urls_namespace="mcp-test-api")
api.add_router("/api/items", items_registry.api)
api.add_router("/billing", billing_registry.api)
urlpatterns = [path("", api.urls)]


def _host(*, include_billing=False):
    host = MCPHost()
    host.register(items_registry, api_base_path="/api/items", description="Items API.")
    if include_billing:
        host.register(billing_registry, api_base_path="/billing", description="Billing API.")
    return host


def _mcp_context(server, token="trusted-principal"):
    request = RequestFactory().get("/mcp", HTTP_X_MCP_TOKEN=token)
    return Context(request_context=SimpleNamespace(request=request), mcp_server=server)


class AsyncMCPHostTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings_override = override_settings(
            ROOT_URLCONF=__name__,
            ALLOWED_HOSTS=["localhost", "testserver"],
            MIDDLEWARE=[],
        )
        self.settings_override.enable()
        middleware_events.clear()
        operation_calls.clear()

    async def asyncTearDown(self):
        self.settings_override.disable()

    async def test_single_tool_lists_registered_apis_and_late_registration(self):
        host = _host()
        host.configure(description_prefix="Use the public API.")
        server = host.build_server()
        tools = await server.list_tools()
        self.assertEqual([tool.name for tool in tools], ["mc_api"])
        self.assertIn("/api/items: Items API.", tools[0].description)
        self.assertTrue(tools[0].description.startswith("Use the public API.\n\n"))

        host.register(billing_registry, api_base_path="/billing", description="Billing API.")
        tools = await server.list_tools()
        self.assertIn("/billing: Billing API.", tools[0].description)
        self.assertEqual(
            json.loads(await host._call_route("GET /billing/invoices", None)),
            {"api": "billing"},
        )

    async def test_tool_call_uses_ninja_route_auth_and_forwards_credentials(self):
        host = _host()
        with self.assertRaises(ToolError) as denied:
            await host._call_route("GET /api/items/items", None)
        self.assertEqual(json.loads(str(denied.exception))["status"], 401)

        result = await host._call_route(
            "GET /api/items/items",
            None,
            request_headers={"X-MCP-Token": "trusted-principal", "Cookie": "sessionid=abc"},
            transport_scope={"client": ("192.0.2.8", 1234)},
        )
        self.assertEqual(
            json.loads(result),
            {"principal": "trusted-principal", "middleware": None, "cookie": "abc", "client": "192.0.2.8"},
        )

    async def test_middleware_runs_request_view_and_response_hooks(self):
        with override_settings(MIDDLEWARE=[f"{__name__}.TrackingMiddleware"]):
            host = _host()
            result = await host._call_route(
                "GET /api/items/items",
                None,
                request_headers={"X-MCP-Token": "trusted-principal"},
            )
        self.assertEqual(json.loads(result)["middleware"], "passed")
        self.assertEqual(
            middleware_events,
            [("request", "/api/items/items"), ("view", "/api/items/items"), ("response", 200)],
        )

    async def test_middleware_can_reject_before_the_operation(self):
        with override_settings(MIDDLEWARE=[f"{__name__}.TrackingMiddleware"]):
            host = _host()
            with self.assertRaises(ToolError) as denied:
                await host._call_route(
                    "GET /api/items/items",
                    None,
                    request_headers={"X-Block": "1", "X-MCP-Token": "trusted-principal"},
                )
        self.assertEqual(json.loads(str(denied.exception))["code"], "blocked")
        self.assertEqual(operation_calls, [])
        self.assertEqual(middleware_events, [("request", "/api/items/items"), ("response", 403)])

    async def test_middleware_process_view_can_reject(self):
        with override_settings(MIDDLEWARE=[f"{__name__}.TrackingMiddleware"]):
            host = _host()
            with self.assertRaises(ToolError) as denied:
                await host._call_route(
                    "GET /api/items/items", None, request_headers={"X-View-Block": "1"},
                )
        self.assertEqual(json.loads(str(denied.exception))["code"], "view_blocked")
        self.assertEqual(operation_calls, [])
        self.assertEqual([event[0] for event in middleware_events], ["request", "view", "response"])

    async def test_discovery_sync_async_query_and_body_use_mounted_urls(self):
        host = _host()
        discovery = json.loads(await host._call_route("GET /api/items/", None))
        self.assertIn("get_async_item", discovery["available_operations"])
        self.assertEqual(
            json.loads(await host._call_route("GET /api/items/async-items/7", None)),
            {"item_id": 7},
        )
        self.assertEqual(
            json.loads(await host._call_route("GET /api/items/query?tag=a&tag=b&empty=", None)),
            {"tags": ["a", "b"], "empty": ""},
        )
        self.assertEqual(
            json.loads(await host._call_route("POST /api/items/echo?tag=one", {"name": "Éclair"})),
            {"payload": {"name": "Éclair"}, "tag": "one"},
        )

    async def test_django_url_must_be_mounted_including_discovery_slash(self):
        host = _host()
        with self.assertRaises(ToolError) as missing_slash:
            await host._call_route("GET /api/items", None)
        self.assertEqual(json.loads(str(missing_slash.exception))["status"], 404)

        unmounted = NinjaMCARouter(Router())

        @unmounted.register("/items", response=dict)
        def get_unmounted(request):
            return {"unexpected": True}

        host.register(unmounted, api_base_path="/unmounted", description="Unmounted API.")
        with self.assertRaises(ToolError) as missing_mount:
            await host._call_route("GET /unmounted/items", None)
        self.assertEqual(json.loads(str(missing_mount.exception))["status"], 404)

    async def test_structured_error_and_non_json_responses(self):
        host = _host()
        with self.assertRaises(ToolError) as missing:
            await host._call_route("GET /api/items/error", None)
        self.assertEqual(json.loads(str(missing.exception))["code"], "item_missing")

        with override_settings(MIDDLEWARE=[f"{__name__}.TrackingMiddleware"]):
            host = _host()
            with self.assertRaises(ToolError) as redirected:
                await host._call_route(
                    "GET /api/items/query", None, request_headers={"X-Redirect": "1"},
                )
            self.assertEqual(json.loads(str(redirected.exception))["status"], 302)
            with self.assertRaises(ToolError) as plain:
                await host._call_route("GET /api/items/query", None, request_headers={"X-Plain": "1"})
            self.assertEqual(json.loads(str(plain.exception))["code"], "mcp_endpoint_error")
            self.assertEqual(
                await host._call_route("GET /api/items/query", None, request_headers={"X-Empty": "1"}),
                "null",
            )

    async def test_only_registered_routes_are_callable(self):
        host = _host()
        with self.assertRaisesRegex(MCAError, "No registered API"):
            await host._call_route("GET /billing/invoices", None)
        with self.assertRaisesRegex(MCAError, "No route matches"):
            await host._call_route("GET /api/items/not-registered", None)
        with self.assertRaisesRegex(MCAError, "cannot receive a JSON body"):
            await host._call_route("GET /api/items/query", {"unexpected": True})
        with self.assertRaisesRegex(MCAError, "No route matches"):
            await host._call_route("GET /api/items/async-items/7%2Fextra", None)

    async def test_tool_forwards_transport_headers_and_list_is_ungated(self):
        with override_settings(MIDDLEWARE=[f"{__name__}.TrackingMiddleware"]):
            host = _host()
            server = host.build_server()
            tools = await server.list_tools()
            self.assertEqual([tool.name for tool in tools], ["mc_api"])
            self.assertEqual(middleware_events, [])

            result = await server.call_tool(
                "mc_api", {"route": "GET /api/items/items"}, _mcp_context(server),
            )
        self.assertFalse(result.is_error)
        self.assertEqual(json.loads(result.content[0].text)["principal"], "trusted-principal")
        self.assertEqual([event[0] for event in middleware_events], ["request", "view", "response"])

    async def test_custom_mcp_path_routes_only_transport_to_mcp(self):
        host = MCPHost()
        host.configure("/gateway/mcp/")
        calls = []

        async def mcp_application(scope, receive, send):
            calls.append(("mcp", scope))

        async def django_application(scope, receive, send):
            calls.append(("django", scope))

        host._django_application = django_application
        host._mcp_application = mcp_application
        await host._dispatch(
            {"type": "http", "path": "/gateway/mcp/", "raw_path": b"/gateway/mcp/"},
            None, None,
        )
        await host._dispatch(
            {"type": "http", "path": "/api/items", "raw_path": b"/api/items"},
            None, None,
        )
        self.assertEqual(calls[0][0], "mcp")
        self.assertEqual(calls[0][1]["path"], "/")
        self.assertEqual(calls[1][0], "django")
        with self.assertRaisesRegex(RuntimeError, "before initialization"):
            host.configure("/other/mcp")

    async def test_registration_rejects_overlapping_paths(self):
        host = _host()
        with self.assertRaisesRegex(RuntimeError, "overlaps"):
            host.register(items_registry, api_base_path="/api/items/private", description="Duplicate")

    async def test_registration_replaces_duplicate_path_with_warning(self):
        host = _host()
        replacement = NinjaMCARouter(Router())

        with self.assertLogs("mca.mcp", level="WARNING") as logs:
            registration = host.register(
                replacement,
                api_base_path="/api/items/",
                description="Reloaded items API.",
            )

        self.assertEqual(
            logs.output,
            [
                "WARNING:mca.mcp:MCA API path '/api/items' was registered again; "
                "replacing the previous registration."
            ],
        )
        self.assertIs(registration.registry, replacement)
        self.assertEqual(registration.description, "Reloaded items API.")
        self.assertEqual(host._registrations, (registration,))

    async def test_registration_rejects_distinct_parent_child_paths(self):
        host = MCPHost()
        host.register(items_registry, api_base_path="/api", description="API.")

        with self.assertRaisesRegex(RuntimeError, "overlaps"):
            host.register(
                billing_registry,
                api_base_path="/api/items",
                description="Items API.",
            )
