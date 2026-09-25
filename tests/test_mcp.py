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
        "/api/items/mcp",
        HTTP_X_MCP_TOKEN=token,
    )
    return Context(
        request_context=SimpleNamespace(request=request),
        mcp_server=server,
    )


class AsyncMCPHostTests(IsolatedAsyncioTestCase):
    async def test_manual_registration_uses_conventions_and_overrides(self):
        registry = NinjaMCARouter(Router())
        host = MCPHost()

        default_route = host.register(registry, "items")
        custom_route = host.register(
            registry,
            "billing",
            path="/mcp/billing",
            tool_name="billing_api",
            api_base_path="/api",
        )

        self.assertEqual(default_route.path, "/api/items/mcp")
        self.assertEqual(default_route.tool_name, "items_api")
        self.assertEqual(custom_route.path, "/mcp/billing")
        self.assertEqual(custom_route.tool_name, "billing_api")

    async def test_manual_registration_rejects_duplicate_paths_and_tools(self):
        registry = NinjaMCARouter(Router())
        host = MCPHost()
        host.register(registry, "items")

        with self.assertRaisesRegex(RuntimeError, "path"):
            host.register(registry, "other", path="/api/items/mcp")
        with self.assertRaisesRegex(RuntimeError, "tool"):
            host.register(registry, "items", path="/other")

    async def test_mcp_can_execute_async_discovery_and_operations(self):
        registry = NinjaMCARouter(Router())

        @registry.register("/items/{item_id}", response=dict)
        async def get_item(request, item_id: int):
            return {"item_id": item_id}

        @registry.register("/sync-items", response=dict)
        def get_sync_items(request):
            return {"kind": "sync"}

        host = MCPHost()

        discovery = await host._call_route(
            registry,
            "items",
            "GET /",
            None,
            "/api/items",
        )
        self.assertIn("get_item", json.loads(discovery)["available_operations"])

        result = await host._call_route(
            registry,
            "items",
            "GET /items/7",
            None,
            "/api/items",
        )
        self.assertEqual(json.loads(result), {"item_id": 7})

        sync_result = await host._call_route(
            registry,
            "items",
            "GET /sync-items",
            None,
            "/api/items",
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
        root = json.loads(
            await host._call_route(
                registry,
                "items",
                "GET /",
                None,
                "/api/items",
            )
        )
        self.assertIn("billing/invoices.md", root["available_guides"])

        details = json.loads(
            await host._call_route(
                registry,
                "items",
                "GET /?operation=get_invoice",
                None,
                "/api/items",
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
        with self.assertRaises(ToolError):
            await unauthenticated_host._call_route(
                registry,
                "items",
                "GET /items",
                None,
                "/api/items",
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
        result = await host._call_route(
            registry,
            "items",
            "GET /items",
            None,
            "/api/items",
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
        server = host.build_server(registry, "items")

        result = await server.call_tool(
            "items_api",
            {"route": "GET /items"},
            _mcp_context(server),
        )

        self.assertFalse(result.is_error)
        self.assertEqual(result.content[0].text, '{"principal": "trusted-principal"}')

    async def test_mcp_tool_forwards_transport_headers_by_default(self):
        registry = _authenticated_registry()

        host = MCPHost()
        server = host.build_server(registry, "items")

        result = await server.call_tool(
            "items_api",
            {"route": "GET /items"},
            _mcp_context(server),
        )

        self.assertFalse(result.is_error)
        self.assertEqual(result.content[0].text, '{"principal": "trusted-principal"}')
