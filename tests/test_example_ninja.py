import json
import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, TestCase

from django.conf import settings

if not settings.configured:
    settings.configure(
        DEFAULT_CHARSET="utf-8",
        INSTALLED_APPS=[],
        SECRET_KEY="mca-example-test-key",
        ROOT_URLCONF="demo.urls",
        ALLOWED_HOSTS=["*"],
        MIDDLEWARE=[],
    )

import django

django.setup()

from django.test import Client, RequestFactory, override_settings

EXAMPLE_ROOT = Path(__file__).resolve().parents[1] / "examples" / "ninja"
if str(EXAMPLE_ROOT) not in sys.path:
    sys.path.insert(0, str(EXAMPLE_ROOT))

from demo.api import public_mca
from demo.asgi import application as demo_application
from demo.rpc import billing_rpc

from mca.mcp import mcp_host


class NinjaCompositionExampleTests(TestCase):
    def setUp(self):
        billing_rpc.calls.clear()
        public_mca.clear_remote_schema_cache()

    def test_public_discovery_exposes_only_explicit_public_operation(self):
        discovery = public_mca._get_context(None, None)

        self.assertEqual(
            discovery["help"],
            "# Public Invoice API\n\n"
            "Start with the public operation discovery document, then read the invoice\n"
            "guide before requesting an invoice.\n",
        )
        self.assertEqual(
            discovery["usage"],
            "Discover public operations and guides before requesting an invoice.",
        )
        self.assertEqual(
            set(discovery["available_operations"]),
            {
                "get_public_invoice",
                "make_public_invoice",
                "update_public_invoice",
                "remove_public_invoice",
            },
        )
        self.assertNotIn("billing.get_invoice", discovery["available_operations"])
        self.assertIn("billing/invoices.md", discovery["available_guides"])
        self.assertIn(
            "billing/invoices/legacy_format.md",
            discovery["available_guides"],
        )
        self.assertNotIn("index.md", discovery["available_guides"])
        self.assertNotIn("billing/index.md", discovery["available_guides"])

        index_guide = public_mca._get_context("index.md", None)
        self.assertEqual(
            index_guide["guides"]["index.md"],
            "# Public Invoice API\n\n"
            "Start with the public operation discovery document, then read the invoice\n"
            "guide before requesting an invoice.\n",
        )

        expected_routes = {
            "get_public_invoice": "GET invoices/{invoice_id}",
            "make_public_invoice": "POST invoices",
            "update_public_invoice": "PATCH invoices/{invoice_id}",
            "remove_public_invoice": "DELETE invoices/{invoice_id}",
        }
        details = public_mca._get_context(None, ",".join(expected_routes))
        self.assertEqual(
            {
                name: schema["route"]
                for name, schema in details["operations"].items()
            },
            expected_routes,
        )

        nested_guide = public_mca._get_context(
            "billing/invoices/legacy_format.md",
            None,
        )
        self.assertEqual(
            nested_guide["guides"]["billing/invoices/legacy_format.md"],
            "# Legacy invoice format\n\n"
            "Legacy clients may request invoice fields using the compatibility format.\n",
        )

    def test_mcp_endpoint_is_registered_explicitly(self):
        self.assertIs(demo_application, mcp_host)
        self.assertEqual(
            [(route.path, route.tool_name) for route in demo_application._routes],
            [("/api/demo/mcp", "demo_api")],
        )

    def test_public_discovery_composes_private_schemas_and_caches_them(self):
        details = public_mca._get_context(
            None,
            "make_public_invoice,update_public_invoice",
        )
        repeated = public_mca._get_context(
            None,
            "make_public_invoice,update_public_invoice",
        )

        create_schema = details["operations"]["make_public_invoice"]
        self.assertEqual(
            create_schema["request_schema"]["properties"]["body"]["type"],
            "object",
        )
        self.assertEqual(
            create_schema["request_schema"]["properties"]["body"]["required"],
            ["customer", "total"],
        )
        self.assertEqual(
            create_schema["request_schema"]["properties"]["body"]["description"],
            "Fields required to create a new invoice.",
        )
        self.assertEqual(
            create_schema["request_schema"]["properties"]["body"]["properties"]["customer"][
                "description"
            ],
            "Name of the customer billed by the invoice.",
        )
        self.assertEqual(
            create_schema["request_schema"]["properties"]["body"]["properties"]["total"][
                "description"
            ],
            "Total amount of the invoice in the example currency.",
        )
        self.assertEqual(
            create_schema["response_schema"]["type"],
            "object",
        )
        self.assertEqual(
            create_schema["response_schema"]["description"],
            "Invoice returned by the billing service.",
        )
        self.assertIn(
            "invoice_id",
            create_schema["response_schema"]["properties"],
        )
        self.assertEqual(
            create_schema["response_schema"]["properties"]["invoice_id"]["description"],
            "Unique identifier of the invoice.",
        )
        self.assertEqual(
            create_schema["response_schema"]["properties"]["status"]["description"],
            "Current lifecycle status of the invoice.",
        )
        self.assertNotIn("components", create_schema["request_schema"])
        self.assertNotIn("components", create_schema["response_schema"])

        update_schema = repeated["operations"]["update_public_invoice"]
        self.assertIn("path_params", update_schema["request_schema"]["properties"])
        self.assertEqual(
            update_schema["request_schema"]["properties"]["path_params"]["properties"][
                "invoice_id"
            ]["description"],
            "Unique identifier of the invoice.",
        )
        self.assertEqual(
            update_schema["request_schema"]["properties"]["body"]["type"],
            "object",
        )
        self.assertEqual(
            update_schema["request_schema"]["properties"]["body"]["properties"]["status"][
                "description"
            ],
            "Replacement invoice status, when changing its state.",
        )
        self.assertNotIn("delegate_to", create_schema)
        self.assertEqual(
            [call["operation"] for call in billing_rpc.calls if call["method"] == "get_context"],
            ["get_invoice,make_invoice,remove_invoice,update_invoice"],
        )

    def test_public_handler_authenticates_checks_acl_and_uses_rpc_client(self):
        request = RequestFactory().get(
            "/api/invoices/7",
            HTTP_X_DEMO_TOKEN="demo-token",
        )
        response = public_mca.execute_http(
            "get_public_invoice",
            request,
            path_params={"invoice_id": 7},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)["invoice_id"], 7)
        self.assertEqual(
            billing_rpc.calls[-1],
            {"method": "get_invoice", "params": {"invoice_id": 7}, "data": None},
        )

    def test_acl_failure_prevents_private_rpc_call(self):
        request = RequestFactory().get(
            "/api/invoices/7",
            HTTP_X_DEMO_TOKEN="limited-token",
        )
        response = public_mca.execute_http(
            "get_public_invoice",
            request,
            path_params={"invoice_id": 7},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(billing_rpc.calls, [])

    def test_public_django_url_reaches_only_the_public_router(self):
        with override_settings(ROOT_URLCONF="demo.urls"):
            response = Client().get(
                "/api/invoices/7",
                HTTP_X_DEMO_TOKEN="demo-token",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "open")
        self.assertEqual(billing_rpc.calls[-1]["method"], "get_invoice")

    def test_public_django_discovery_uses_async_private_rpc(self):
        with override_settings(ROOT_URLCONF="demo.urls"):
            response = Client().get("/api/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("make_public_invoice", response.json()["available_operations"])
        self.assertEqual(
            [call["method"] for call in billing_rpc.calls],
            ["get_context"],
        )

    def test_public_write_operations_delegate_with_params_and_body(self):
        with override_settings(ROOT_URLCONF="demo.urls"):
            client = Client()
            created = client.post(
                "/api/invoices",
                data=json.dumps({"customer": "New Customer", "total": 42.5}),
                content_type="application/json",
                HTTP_X_DEMO_TOKEN="demo-token",
            )
            updated = client.patch(
                "/api/invoices/7",
                data=json.dumps({"status": "paid"}),
                content_type="application/json",
                HTTP_X_DEMO_TOKEN="demo-token",
            )
            deleted = client.delete(
                "/api/invoices/7",
                HTTP_X_DEMO_TOKEN="demo-token",
            )

        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["invoice_id"], 8)
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["status"], "paid")
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json(), {"invoice_id": 7, "status": "deleted"})
        self.assertEqual(
            [call["method"] for call in billing_rpc.calls],
            ["make_invoice", "update_invoice", "remove_invoice"],
        )
        self.assertEqual(
            billing_rpc.calls[0]["data"],
            {"customer": "New Customer", "total": 42.5},
        )
        self.assertEqual(billing_rpc.calls[1]["params"], {"invoice_id": 7})
        self.assertEqual(billing_rpc.calls[1]["data"], {"status": "paid"})
        self.assertEqual(billing_rpc.calls[2]["params"], {"invoice_id": 7})

    def test_write_acl_failure_prevents_private_rpc_call(self):
        with override_settings(ROOT_URLCONF="demo.urls"):
            response = Client().post(
                "/api/invoices",
                data=json.dumps({"customer": "Denied", "total": 1}),
                content_type="application/json",
                HTTP_X_DEMO_TOKEN="limited-token",
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(billing_rpc.calls, [])

    def test_invalid_private_payload_returns_unprocessable_entity(self):
        with override_settings(ROOT_URLCONF="demo.urls"):
            response = Client().post(
                "/api/invoices",
                data=json.dumps({"customer": "Missing Total"}),
                content_type="application/json",
                HTTP_X_DEMO_TOKEN="demo-token",
            )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json(),
            {
                "code": "invalid_request",
                "detail": "Field required",
                "field": "total",
            },
        )


class NinjaCompositionExampleAsyncTests(IsolatedAsyncioTestCase):
    async def test_async_rpc_methods_match_sync_client_behavior(self):
        billing_rpc.calls.clear()

        discovery = await billing_rpc.adiscover(operation="make_invoice")
        result = await billing_rpc.acall(
            "make_invoice",
            data={"customer": "Async Customer", "total": 18.5},
        )

        self.assertIn("make_invoice", discovery["operations"])
        self.assertEqual(result["customer"], "Async Customer")
        self.assertEqual(
            billing_rpc.calls,
            [
                {"method": "get_context", "guide": None, "operation": "make_invoice"},
                {
                    "method": "make_invoice",
                    "params": None,
                    "data": {"customer": "Async Customer", "total": 18.5},
                },
            ],
        )
