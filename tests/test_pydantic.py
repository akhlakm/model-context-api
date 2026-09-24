from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from pydantic import BaseModel

from mca.models import ErrorOut, MCAResponseOut
from mca.pydantic import PydanticMCARouter


class ItemParams(BaseModel):
    item_id: int


class ItemOut(BaseModel):
    item_id: int


class FakeMCAClient:
    def __init__(self):
        self.discovery_calls = []
        self.calls = []

    def discover(self, *, guide=None, operation=None):
        self.discovery_calls.append((guide, operation))
        if guide is None and operation is None:
            return {
                "title": "Billing API",
                "version": 1.0,
                "help": "Billing help.",
                "available_guides": ["index.md", "invoices.md"],
                "available_operations": {
                    "get_invoice": "GET invoices/{invoice_id} - Read an invoice.",
                },
            }
        result = {}
        if guide is not None:
            result["guides"] = {
                name: f"# {name.removesuffix('.md')}"
                for name in guide.split(",")
            }
        if operation is not None:
            result["operations"] = {
                "get_invoice": {
                    "route": "GET invoices/{invoice_id}",
                    "description": "Read an invoice.",
                    "guides": ["invoices.md"],
                    "request_schema": None,
                    "response_schema": {"type": "object"},
                }
                for _ in operation.split(",")
            }
        return result

    def call(self, operation, *, params=None, data=None):
        self.calls.append((operation, params, data))
        return {"invoice_id": params["invoice_id"]}


class PydanticMCARouterPackageTests(TestCase):
    def setUp(self):
        self.guides_dir = TemporaryDirectory()
        Path(self.guides_dir.name, "index.md").write_text("# MCA", encoding="utf-8")
        Path(self.guides_dir.name, "workflow.md").write_text("# Workflow", encoding="utf-8")
        self.router = PydanticMCARouter(guides_dir=self.guides_dir.name)

        @self.router.register("/items/{item_id}")
        def get_item(params: ItemParams) -> ItemOut:
            return ItemOut(item_id=params.item_id)

    def tearDown(self):
        self.guides_dir.cleanup()

    def test_discovery_and_dispatch_are_framework_independent(self):
        discovery = self.router.dispatch("get_mca")
        result = self.router.dispatch("/items/7", method="GET")

        self.assertIsInstance(discovery, MCAResponseOut)
        self.assertEqual(discovery.title, "Model Context API")
        self.assertEqual(discovery.version, 1.0)
        self.assertIn("index.md", discovery.available_guides)
        details = self.router.dispatch("get_mca", params={"operation": "get_item"})
        self.assertEqual(details.operations["get_item"].route, "GET items/{item_id}")
        self.assertNotIn("guides", details.model_dump())
        self.assertNotIn("operation", details.operations["get_item"].model_dump())
        self.assertEqual(result.item_id, 7)

    def test_operations_can_be_registered_by_name_without_a_route(self):
        router = PydanticMCARouter()

        @router.register()
        def get_status() -> ItemOut:
            return ItemOut(item_id=1)

        route = router.route("get_status")
        details = router.dispatch("get_mca", params={"operation": "get_status"})

        self.assertIsNone(route.path)
        self.assertEqual(route.discovery_route, "get_status")
        self.assertEqual(details.operations["get_status"].route, "get_status")
        self.assertEqual(router.dispatch("get_status").item_id, 1)
        self.assertIsNone(router.resolve("GET", "/status"))

    def test_register_all_can_create_name_only_operations(self):
        router = PydanticMCARouter()

        @router.register_all(methods=("GET", "POST"))
        def item() -> ItemOut:
            return ItemOut(item_id=1)

        self.assertEqual(
            [(route.method, route.operation, route.path) for route in router.routes()[1:]],
            [("GET", "get_item", None), ("POST", "make_item", None)],
        )

    def test_discovery_metadata_can_be_configured(self):
        router = PydanticMCARouter(
            guides_dir=self.guides_dir.name,
            title="Example API",
            version=2.5,
        )

        discovery = router.dispatch("get_mca")

        self.assertEqual(discovery.title, "Example API")
        self.assertEqual(discovery.version, 2.5)

    def test_guides_can_be_disabled_and_help_can_be_overridden(self):
        router = PydanticMCARouter(help="Use operation discovery.")

        @router.register("/guided", guides=["workflow.md"])
        def get_guided() -> ItemOut:
            """Read guided data."""
            return ItemOut(item_id=1)

        discovery = router.dispatch("get_mca")
        details = router.dispatch("get_mca", params={"operation": "get_guided"})
        guide_error = router.dispatch("get_mca", params={"guide": "workflow.md"})

        self.assertEqual(discovery.help, "Use operation discovery.")
        self.assertNotIn("index", discovery.model_dump())
        self.assertNotIn("available_guides", discovery.model_dump())
        self.assertNotIn("guides", details.operations["get_guided"].model_dump())
        self.assertIsInstance(guide_error, ErrorOut)
        self.assertEqual(guide_error.code, "unknown_guides")

    def test_operation_schema_can_list_relevant_guides(self):
        @self.router.register("/guided", guides=["workflow.md"])
        def get_guided() -> ItemOut:
            return ItemOut(item_id=1)

        details = self.router.dispatch("get_mca", params={"operation": "get_guided"})

        self.assertEqual(details.operations["get_guided"].model_dump()["guides"], ["workflow.md"])

    def test_discovery_omits_unrequested_content(self):
        details = self.router.dispatch("get_mca", params={"guide": "index.md"})

        self.assertEqual(set(details.model_dump()), {"guides"})

    def test_invalid_dispatch_returns_package_error_model(self):
        result = self.router.dispatch("/items/not-an-int", method="GET")

        self.assertIsInstance(result, ErrorOut)
        self.assertEqual(result.code, "invalid_request")
        self.assertEqual(result.field, "item_id")

    def test_mount_requires_explicit_public_routes(self):
        client = FakeMCAClient()
        self.router.mount("billing", client)

        discovery = self.router.dispatch("get_mca")
        self.assertNotIn("billing.get_invoice", discovery.available_operations)
        self.assertNotIn("billing/index.md", discovery.available_guides)

        @self.router.register(
            "/invoices/{item_id}",
            delegate_to="billing.get_invoice",
        )
        def get_public_invoice(params: ItemParams) -> ItemOut:
            result = client.call(
                "get_invoice",
                params={"invoice_id": params.item_id},
            )
            return ItemOut(item_id=result["invoice_id"])

        discovery = self.router.dispatch("get_mca")
        details = self.router.dispatch(
            "get_mca",
            params={"operation": "get_public_invoice"},
        )
        result = self.router.dispatch(
            "get_public_invoice",
            params={"item_id": 7},
        )
        mounted_result = self.router.dispatch(
            "billing.get_invoice",
            params={"invoice_id": 7},
        )

        self.assertEqual(discovery.title, "Model Context API")
        self.assertEqual(discovery.index, "# MCA")
        self.assertIn("billing/invoices.md", discovery.available_guides)
        self.assertEqual(
            discovery.available_operations["get_public_invoice"],
            "GET invoices/{item_id} - Get public invoice",
        )
        self.assertEqual(details.operations["get_public_invoice"].route, "GET invoices/{item_id}")
        self.assertEqual(
            details.operations["get_public_invoice"].guides,
            ["billing/invoices.md"],
        )
        self.assertEqual(result, ItemOut(item_id=7))
        self.assertIsInstance(mounted_result, ErrorOut)
        self.assertEqual(mounted_result.code, "unknown_operation")
        self.assertEqual(client.calls, [("get_invoice", {"invoice_id": 7}, None)])

        operation_result = self.router.dispatch(
            "get_mca",
            params={"operation": "billing.get_invoice"},
        )
        self.assertIsInstance(operation_result, ErrorOut)
        self.assertEqual(operation_result.code, "unknown_operation")

        guide_result = self.router.dispatch(
            "get_mca",
            params={"guide": "billing/index.md"},
        )
        self.assertIsInstance(guide_result, ErrorOut)
        self.assertEqual(guide_result.code, "unknown_guides")

    def test_explicit_composition_merges_mixed_guides_and_public_operation_schemas(self):
        client = FakeMCAClient()
        self.router.mount("billing", client)

        @self.router.register(
            "/invoices/{item_id}",
            delegate_to="billing.get_invoice",
        )
        def get_public_invoice(params: ItemParams) -> ItemOut:
            return ItemOut(**client.call(
                "get_invoice",
                params=params.model_dump(),
            ))

        details = self.router.dispatch(
            "get_mca",
            params={
                "guide": "workflow.md,billing/invoices.md",
                "operation": "get_item,get_public_invoice",
            },
        )

        self.assertEqual(
            set(details.guides),
            {"workflow.md", "billing/invoices.md"},
        )
        self.assertIn("get_item", details.operations)
        self.assertIn("get_public_invoice", details.operations)
        self.assertEqual(
            details.operations["get_public_invoice"].route,
            "GET invoices/{item_id}",
        )
        self.assertEqual(
            details.operations["get_public_invoice"].guides,
            ["billing/invoices.md"],
        )

    def test_mount_validates_namespaces_and_remote_failures(self):
        client = FakeMCAClient()
        self.router.mount("billing", client)

        with self.assertRaises(ValueError):
            self.router.mount("billing", client)
        with self.assertRaises(ValueError):
            self.router.mount("billing.private", client)

        conflicting_router = PydanticMCARouter()
        conflicting_router.mount("billing", client)
        with self.assertRaises(ValueError):
            @conflicting_router.register(operation_id="billing.get_invoice")
            def get_conflicting_operation():
                return None

        with self.assertRaises(ValueError):
            @self.router.register(
                "/bad",
                delegate_to="billing",
            )
            def get_bad_target():
                return None

        with self.assertRaises(ValueError):
            @self.router.register(
                "/unmounted",
                delegate_to="private.get_status",
            )
            def get_unmounted_target():
                return None

        failing_router = PydanticMCARouter()

        class FailingClient(FakeMCAClient):
            def discover(self, **kwargs):
                raise RuntimeError("connection refused")

        failing_router.mount("private", FailingClient())
        @failing_router.register(delegate_to="private.get_status")
        def get_status() -> ItemOut:
            return ItemOut(item_id=1)

        result = failing_router.dispatch("get_mca")

        self.assertIsInstance(result, ErrorOut)
        self.assertEqual(result.code, "upstream_unavailable")
        self.assertEqual(result.field, "service")
