import json
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
from mcp.server.mcpserver.exceptions import ToolError
from ninja import Router

from mca.mcp import MCPHost
from mca.ninja import NinjaMCARouter


class AsyncMCPHostTests(IsolatedAsyncioTestCase):
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
        def authenticate(request):
            return request.headers.get("X-MCP-Token")

        registry = NinjaMCARouter(Router(auth=authenticate))

        @registry.register("/items", response=dict)
        def get_items(request):
            return {"principal": request.auth}

        unauthenticated_host = MCPHost()
        with self.assertRaises(ToolError):
            await unauthenticated_host._call_route(
                registry,
                "items",
                "GET /items",
                None,
                "/api/items",
            )

        def authenticated_context(app_label, method, path, path_params, query_params, body):
            return RequestFactory().generic(
                method,
                path,
                HTTP_X_MCP_TOKEN="trusted-principal",
            )

        host = MCPHost(request_context_factory=authenticated_context)
        result = await host._call_route(
            registry,
            "items",
            "GET /items",
            None,
            "/api/items",
        )
        self.assertEqual(json.loads(result), {"principal": "trusted-principal"})
