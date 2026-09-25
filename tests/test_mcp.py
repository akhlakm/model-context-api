import json
from types import SimpleNamespace
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

from django.test import RequestFactory
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError
from ninja import Router

from mca.base import MCAError
from mca.mcp import MCPHost
from mca.ninja import NinjaMCARouter


def _authenticated_registry():
    def authenticate(request):
        return request.headers.get("X-MCP-Token")

    registry = NinjaMCARouter(Router(auth=authenticate))

    @registry.register("/items", response=dict)
    def get_items(request):
        return {"principal": request.auth}

    return registry


def _mcp_context(server, token="trusted-principal"):
    request = RequestFactory().get(
        "/mcp",
        HTTP_X_MCP_TOKEN=token,
    )
    return Context(
        request_context=SimpleNamespace(request=request),
        mcp_server=server,
    )


class AsyncMCPHostTests(IsolatedAsyncioTestCase):
    async def test_single_tool_lists_registered_apis(self):
        registry = NinjaMCARouter(Router())
        host = MCPHost()
        host.configure(
            description_prefix="This tool accesses the public API.\nUse it for item operations."
        )

        default_registration = host.register(
            registry,
            "items",
            api_base_path="/api/v2/items",
            description="Public item API.",
        )
        custom_registration = host.register(
            registry,
            "billing",
            api_base_path="/billing",
            description="Billing API.",
        )

        self.assertEqual(default_registration.api_base_path, "/api/v2/items")
        self.assertEqual(custom_registration.api_base_path, "/billing")
        tools = await host.build_server().list_tools()
        self.assertEqual([tool.name for tool in tools], ["mc_api"])
        self.assertTrue(
            tools[0].description.startswith(
                "This tool accesses the public API.\nUse it for item operations.\n\n"
            )
        )
        self.assertIn("/api/v2/items: Public item API.", tools[0].description)
        self.assertIn("/billing: Billing API.", tools[0].description)
        with self.assertRaisesRegex(RuntimeError, "before host initialization"):
            host.register(
                registry,
                "late",
                api_base_path="/api/v2/late",
                description="Late API.",
            )

    async def test_manual_registration_rejects_duplicate_and_overlapping_api_paths(self):
        registry = NinjaMCARouter(Router())
        host = MCPHost()
        host.register(
            registry,
            "items",
            api_base_path="/api/v2/items",
            description="Items API.",
        )

        with self.assertRaisesRegex(RuntimeError, "path"):
            host.register(
                registry,
                "other",
                api_base_path="/api/v2/items",
                description="Other API.",
            )
        with self.assertRaisesRegex(RuntimeError, "overlaps"):
            host.register(
                registry,
                "nested",
                api_base_path="/api/v2/items/private",
                description="Nested API.",
            )

    async def test_single_tool_routes_multiple_registered_apis(self):
        items = NinjaMCARouter(Router())
        billing = NinjaMCARouter(Router())

        @items.register("/items", response=dict)
        def get_items(request):
            return {"api": "items"}

        @billing.register("/invoices", response=dict)
        def get_invoices(request):
            return {"api": "billing"}

        host = MCPHost()
        host.register(
            items,
            "items",
            api_base_path="/api/v2/items",
            description="Item API.",
        )
        host.register(
            billing,
            "billing",
            api_base_path="/billing",
            description="Billing API.",
        )

        items_result = await host._call_route("GET /api/v2/items/items", None)
        billing_result = await host._call_route("GET /billing/invoices", None)

        self.assertEqual(json.loads(items_result), {"api": "items"})
        self.assertEqual(json.loads(billing_result), {"api": "billing"})
        with self.assertRaisesRegex(MCAError, "No registered API"):
            await host._call_route("GET /api/items-extra", None)

    async def test_custom_mcp_path_routes_only_one_transport_application(self):
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
            None,
            None,
        )
        await host._dispatch(
            {"type": "http", "path": "/api/items", "raw_path": b"/api/items"},
            None,
            None,
        )

        self.assertEqual(calls[0][0], "mcp")
        self.assertEqual(calls[0][1]["path"], "/")
        self.assertEqual(calls[1][0], "django")
        with self.assertRaisesRegex(RuntimeError, "before initialization"):
            host.configure("/other/mcp")
        with self.assertRaisesRegex(ValueError, "description prefix"):
            MCPHost().configure(description_prefix=None)

    async def test_mcp_can_execute_async_discovery_and_operations(self):
        registry = NinjaMCARouter(Router())

        @registry.register("/items/{item_id}", response=dict)
        async def get_item(request, item_id: int):
            return {"item_id": item_id}

        @registry.register("/sync-items", response=dict)
        def get_sync_items(request):
            return {"kind": "sync"}

        host = MCPHost()
        host.register(
            registry,
            "items",
            api_base_path="/api/items",
            description="Items API.",
        )

        discovery = await host._call_route(
            "GET /api/items",
            None,
        )
        self.assertIn("get_item", json.loads(discovery)["available_operations"])

        result = await host._call_route(
            "GET /api/items/items/7",
            None,
        )
        self.assertEqual(json.loads(result), {"item_id": 7})

        sync_result = await host._call_route(
            "GET /api/items/sync-items",
            None,
        )
        self.assertEqual(json.loads(sync_result), {"kind": "sync"})

    async def test_mcp_composes_async_remote_schemas_and_guides(self):
        class AsyncClient:
            def __init__(self):
                self.calls = []

            async def adiscover(self, *, guide=None, operation=None):
                self.calls.append((guide, operation))
                return {
                    "operations": {
                        "get_invoice": {
                            "route": "GET private/invoices/{invoice_id}",
                            "description": "Read an invoice.",
                            "guides": ["invoices.md"],
                            "request_schema": None,
                            "response_schema": {"type": "object"},
                        }
                    }
                }

        registry = NinjaMCARouter(Router())
        client = AsyncClient()
        registry.mount("billing", client)

        @registry.register(
            "/invoices/{invoice_id}",
            response=dict,
            delegate_to="billing.get_invoice",
        )
        def get_invoice(request, invoice_id: int):
            return {"invoice_id": invoice_id}

        host = MCPHost()
        host.register(
            registry,
            "items",
            api_base_path="/api/items",
            description="Items API.",
        )
        root = json.loads(
            await host._call_route(
                "GET /api/items",
                None,
            )
        )
        self.assertIn("billing/invoices.md", root["available_guides"])

        details = json.loads(
            await host._call_route(
                "GET /api/items?operation=get_invoice",
                None,
            )
        )
        self.assertEqual(
            details["operations"]["get_invoice"]["response_schema"],
            {"type": "object"},
        )
        self.assertEqual(client.calls, [(None, "get_invoice")])

    async def test_mcp_request_context_supplies_application_authentication(self):
        registry = _authenticated_registry()

        unauthenticated_host = MCPHost()
        unauthenticated_host.register(
            registry,
            "items",
            api_base_path="/api/items",
            description="Items API.",
        )
        with self.assertRaises(ToolError):
            await unauthenticated_host._call_route(
                "GET /api/items/items",
                None,
            )

        received_headers = {}

        def authenticated_context(
            app_label,
            method,
            path,
            path_params,
            query_params,
            body,
            request_headers,
        ):
            received_headers.update(request_headers)
            return RequestFactory().generic(
                method,
                path,
                headers={
                    "X-MCP-Token": request_headers["X-MCP-Token"],
                },
            )

        host = MCPHost(request_context_factory=authenticated_context)
        host.register(
            registry,
            "items",
            api_base_path="/api/items",
            description="Items API.",
        )
        result = await host._call_route(
            "GET /api/items/items",
            None,
            request_headers={"X-MCP-Token": "trusted-principal"},
        )
        self.assertEqual(json.loads(result), {"principal": "trusted-principal"})
        self.assertEqual(received_headers, {"X-MCP-Token": "trusted-principal"})

    async def test_mcp_tool_passes_transport_headers_to_request_context_factory(self):
        registry = _authenticated_registry()

        def authenticated_context(
            app_label,
            method,
            path,
            path_params,
            query_params,
            body,
            request_headers,
        ):
            return RequestFactory().generic(method, path, headers=dict(request_headers))

        host = MCPHost(request_context_factory=authenticated_context)
        host.register(
            registry,
            "items",
            api_base_path="/api/items",
            description="Items API.",
        )
        server = host.build_server()

        result = await server.call_tool(
            "mc_api",
            {"route": "GET /api/items/items"},
            _mcp_context(server),
        )

        self.assertFalse(result.is_error)
        self.assertEqual(result.content[0].text, '{"principal": "trusted-principal"}')

    async def test_mcp_tool_forwards_transport_headers_by_default(self):
        registry = _authenticated_registry()

        host = MCPHost()
        host.register(
            registry,
            "items",
            api_base_path="/api/items",
            description="Items API.",
        )
        server = host.build_server()

        result = await server.call_tool(
            "mc_api",
            {"route": "GET /api/items/items"},
            _mcp_context(server),
        )

        self.assertFalse(result.is_error)
        self.assertEqual(result.content[0].text, '{"principal": "trusted-principal"}')
