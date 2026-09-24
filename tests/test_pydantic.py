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
