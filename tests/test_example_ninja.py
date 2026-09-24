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

    def test_public_discovery_exposes_only_explicit_public_operation(self):
        discovery = public_mca._get_mca(None, None)

        self.assertIn("get_public_invoice", discovery["available_operations"])
        self.assertNotIn("billing.get_invoice", discovery["available_operations"])
        self.assertIn("billing/invoices.md", discovery["available_guides"])
        self.assertNotIn("billing/index.md", discovery["available_guides"])

        details = public_mca._get_mca(None, "get_public_invoice")
        self.assertEqual(
            details["operations"]["get_public_invoice"]["route"],
            "GET invoices/{invoice_id}",
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
