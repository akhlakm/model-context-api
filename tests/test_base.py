from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from mca.base import BaseMCARouter, MCAError, RegisteredRoute


class FakeMCARouter(BaseMCARouter):
    def __init__(self, guides_dir=None, **options):
        self.transport_routes = []
        self.transport_calls = []
        super().__init__(guides_dir=guides_dir, **options)

    def _discovery_endpoints(self):
        def get_mca():
            return self.discovery(None, None, lambda route: route.relative_route)

        return (("/", "get_mca", get_mca, {}),)

    def _register_transport_route(
        self,
        route,
        endpoint,
        options,
        *,
        path=None,
        operation_id=None,
        include_in_schema=None,
    ):
        self.transport_calls.append(
            {
                "method": route.method,
                "path": path or route.path,
                "operation_id": operation_id or route.operation,
                "include_in_schema": include_in_schema,
            }
        )
        if path is None:
            self.transport_routes.append(route)
        return endpoint

    def _dispatch_registered(self, route, params, data):
        return route.operation, params, data

    def _dispatch_error(self, error):
        return error


class BaseMCARouterTests(TestCase):
    def test_core_router_owns_guides_registration_and_path_resolution(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "index.md").write_text("# MCA", encoding="utf-8")
            (root / "workflow.md").write_text("# Workflow", encoding="utf-8")
            router = FakeMCARouter(root)

            @router.register("/items/{item_id}")
            def get_item():
                return None

            self.assertEqual(router.guide_catalog.available(), ["index.md", "workflow.md"])
            self.assertEqual(router.guide_catalog.read("workflow.md")["workflow.md"], "# Workflow")
            self.assertEqual(router.route("get_mca").discovery_route, "GET .")
            discovery = router.discovery(None, None, lambda route: route.relative_route)
            self.assertEqual(
                discovery["available_operations"]["get_item"],
                "GET items/{item_id} - Get item",
            )
            self.assertEqual(
                router.public_routes(),
                (
                    ("get_mca", "GET /"),
                    ("get_item", "GET /items/{item_id}"),
                ),
            )
            self.assertEqual(
                router.dispatch("/items/7", method="GET"),
                ("get_item", {"item_id": "7"}, None),
            )
            self.assertEqual(
                router.dispatch("/items/7/", method="GET"),
                ("get_item", {"item_id": "7"}, None),
            )

    def test_missing_guides_use_the_shared_error(self):
        with TemporaryDirectory() as directory:
            router = FakeMCARouter(directory)

            with self.assertRaises(MCAError) as context:
                router.guide_catalog.read("missing.md")

            self.assertEqual(context.exception.code, "unknown_guides")
            self.assertEqual(context.exception.status, 404)

    def test_guides_can_be_disabled_and_help_can_be_overridden(self):
        router = FakeMCARouter(None, help="Use operation discovery.")

        @router.register("/guided", guides=["workflow.md"])
        def get_guided():
            """Read guided data."""
            return None

        discovery = router.discovery(None, None, lambda route: route.relative_route)

        self.assertEqual(discovery["help"], "Use operation discovery.")
        self.assertNotIn("index", discovery)
        self.assertNotIn("available_guides", discovery)
        self.assertEqual(
            discovery["available_operations"]["get_guided"],
            "GET guided - Read guided data.",
        )
        with self.assertRaises(MCAError) as context:
            router.guide_catalog.read("workflow.md")
        self.assertEqual(context.exception.code, "unknown_guides")
        self.assertEqual(context.exception.status, 404)

    def test_register_and_register_all_use_endpoint_docstrings(self):
        with TemporaryDirectory() as directory:
            router = FakeMCARouter(directory)

            @router.register("/documented")
            def get_documented():
                """Read documented data."""
                return None

            @router.register_all("/shared", operation_id="shared", methods=("GET", "POST"))
            def shared():
                """Use the shared operation implementation."""
                return None

            @router.register("/overridden", description="Explicit operation description.")
            def get_overridden():
                """This docstring should not win."""
                return None

            self.assertEqual(router.route("get_documented").description, "Read documented data.")
            self.assertEqual(router.route("get_shared").description, "Use the shared operation implementation.")
            self.assertEqual(router.route("make_shared").description, "Use the shared operation implementation.")
            self.assertEqual(router.route("get_overridden").description, "Explicit operation description.")

    def test_routes_can_be_hidden_from_discovery(self):
        with TemporaryDirectory() as directory:
            Path(directory, "index.md").write_text("# MCA", encoding="utf-8")
            router = FakeMCARouter(directory)

            @router.register("/hidden", include_in_discovery=False)
            def get_hidden():
                return None

            discovery = router.discovery(None, None, lambda route: route.relative_route)

            self.assertNotIn("get_hidden", discovery["available_operations"])
            self.assertIn(("get_hidden", "GET /hidden"), router.public_routes())

    def test_transport_adapter_receives_normalized_route_metadata(self):
        with TemporaryDirectory() as directory:
            router = FakeMCARouter(directory)

            @router.register_all("/items", operation_id="items", methods=("GET", "POST"))
            def items():
                return None

            self.assertEqual(
                [(route.method, route.operation) for route in router.transport_routes[1:]],
                [("GET", "get_items"), ("POST", "make_items")],
            )
            self.assertTrue(all(isinstance(route, RegisteredRoute) for route in router.transport_routes))
            item_calls = [
                call
                for call in router.transport_calls
                if call["operation_id"] in {
                    "get_items",
                    "get_items__slash_variant",
                    "make_items",
                    "make_items__slash_variant",
                }
            ]
            self.assertEqual(
                [(call["method"], call["path"]) for call in item_calls],
                [
                    ("GET", "/items"),
                    ("GET", "/items/"),
                    ("POST", "/items"),
                    ("POST", "/items/"),
                ],
            )
            self.assertTrue(all(call["include_in_schema"] is False for call in item_calls[1::2]))
