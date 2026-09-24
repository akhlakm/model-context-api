import json
import sys
from pathlib import Path
from unittest import TestCase

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
from demo.rpc import billing_rpc


class NinjaCompositionExampleTests(TestCase):
    def setUp(self):
        billing_rpc.calls.clear()
        public_mca.clear_remote_schema_cache()

    def test_public_discovery_exposes_only_explicit_public_operation(self):
        discovery = public_mca._get_mca(None, None)

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
        self.assertNotIn("billing/index.md", discovery["available_guides"])

        expected_routes = {
            "get_public_invoice": "GET invoices/{invoice_id}",
            "make_public_invoice": "POST invoices",
            "update_public_invoice": "PATCH invoices/{invoice_id}",
            "remove_public_invoice": "DELETE invoices/{invoice_id}",
        }
        details = public_mca._get_mca(None, ",".join(expected_routes))
        self.assertEqual(
            {
                name: schema["route"]
                for name, schema in details["operations"].items()
            },
            expected_routes,
        )

        nested_guide = public_mca._get_mca(
            "billing/invoices/legacy_format.md",
            None,
        )
        self.assertEqual(
            nested_guide["guides"]["billing/invoices/legacy_format.md"],
            "# Legacy invoice format\n\n"
            "Legacy clients may request invoice fields using the compatibility format.\n",
        )

    def test_public_discovery_composes_private_schemas_and_caches_them(self):
        details = public_mca._get_mca(
            None,
            "make_public_invoice,update_public_invoice",
        )
        repeated = public_mca._get_mca(
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
            create_schema["response_schema"]["type"],
            "object",
        )
        self.assertIn(
            "invoice_id",
            create_schema["response_schema"]["properties"],
        )
        self.assertNotIn("components", create_schema["request_schema"])
        self.assertNotIn("components", create_schema["response_schema"])

        update_schema = repeated["operations"]["update_public_invoice"]
        self.assertIn("path_params", update_schema["request_schema"]["properties"])
        self.assertEqual(
            update_schema["request_schema"]["properties"]["body"]["type"],
            "object",
        )
        self.assertNotIn("delegate_to", create_schema)
        self.assertEqual(
            [call["operation"] for call in billing_rpc.calls if call["method"] == "get_mca"],
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
