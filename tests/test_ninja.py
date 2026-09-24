import json
from pathlib import Path

from django.conf import settings
from django.test import RequestFactory

if not settings.configured:
    settings.configure(
        DEFAULT_CHARSET="utf-8",
        INSTALLED_APPS=[],
        SECRET_KEY="mca-test-key",
    )

import django

django.setup()

from unittest import TestCase

from ninja import NinjaAPI, Router

from mca.ninja import NinjaMCARouter


class FakeAPI:
    def __init__(self):
        self.calls = []

    def get(self, path, **options):
        return self._decorator("GET", path, options)

    def post(self, path, **options):
        return self._decorator("POST", path, options)

    def put(self, path, **options):
        return self._decorator("PUT", path, options)

    def patch(self, path, **options):
        return self._decorator("PATCH", path, options)

    def delete(self, path, **options):
        return self._decorator("DELETE", path, options)

    def _decorator(self, method, path, options):
        def decorator(endpoint):
            self.calls.append((method, path, options, endpoint))
            return endpoint

        return decorator

    def get_openapi_schema(self):
        return {
            "paths": {
                "/guided": {
                    "get": {
                        "operationId": "get_guided",
                        "description": "Read guided data.",
                        "responses": {"200": {"description": "OK"}},
                    },
                },
                "/documented": {
                    "get": {
                        "operationId": "get_documented",
                        "responses": {"200": {"description": "OK"}},
                    },
                },
            },
            "components": {"schemas": {}},
        }


class NinjaMCARouterPackageTests(TestCase):
    def test_register_all_generates_selected_operation_ids(self):
        api = FakeAPI()
        router = NinjaMCARouter(api, guides_dir=Path(__file__).resolve().parents[1])

        @router.register_all(
            "/engine",
            operation_id="engine",
            methods=("GET", "POST"),
        )
        def engine(request):
            return None

        self.assertIs(router.routes()[1].endpoint, engine)
        self.assertEqual(
            [
                (call[0], call[2]["operation_id"])
                for call in api.calls
                if call[2].get("include_in_schema") is not False
                ][1:],
            [("GET", "get_engine"), ("POST", "make_engine")],
        )
        self.assertEqual(
            [call[1] for call in api.calls if call[2].get("include_in_schema") is False],
            ["/engine/", "/engine/"],
        )

    def test_operation_schema_preserves_guide_metadata(self):
        api = FakeAPI()
        router = NinjaMCARouter(api, guides_dir=Path(__file__).resolve().parents[1])

        @router.register("/guided", guides=["workflow.md"])
        def get_guided():
            return []

        schema = router._route_schema(router.route("get_guided"))

        self.assertEqual(schema["route"], "GET guided")
        self.assertNotIn("operation", schema)
        self.assertEqual(schema["guides"], ["workflow.md"])

    def test_guides_can_be_disabled_and_docstrings_describe_operations(self):
        api = FakeAPI()
        router = NinjaMCARouter(api, help="Use operation discovery.")

        @router.register("/documented")
        def get_documented():
            """Read documented data."""
            return []

        @router.register("/guided", guides=["workflow.md"])
        def get_guided():
            """Read guided data."""
            return []

        discovery = router.discovery(None, None, lambda route: route.relative_route)
        documented = router._route_schema(router.route("get_documented"))
        guided = router._route_schema(router.route("get_guided"))

        self.assertEqual(discovery["help"], "Use operation discovery.")
        self.assertNotIn("index", discovery)
        self.assertNotIn("available_guides", discovery)
        self.assertEqual(documented["description"], "Read documented data.")
        self.assertNotIn("guides", guided)
        documented_call = next(call for call in api.calls if call[2].get("operation_id") == "get_documented")
        self.assertEqual(documented_call[2]["description"], "Read documented data.")

    def test_router_backed_registry_supports_schema_and_execution(self):
        def authenticate(request):
            return "allowed" if getattr(request, "_mca_allow_anonymous", False) else None

        api_router = Router(auth=authenticate)
        registry = NinjaMCARouter(api_router)

        @registry.register("/items/{item_id}", response=dict)
        def get_item(request, item_id: int):
            return {"item_id": item_id}

        root_api = NinjaAPI()
        root_api.add_router("/v1", api_router)
        schema = registry._route_schema(registry.route("get_item"))
        root_schema = root_api.get_openapi_schema(path_prefix="")
        request = RequestFactory().get("/items/7")
        denied_response = registry.execute_http(
            "get_item",
            request,
            path_params={"item_id": 7},
        )
        response = registry.execute_http(
            "get_item",
            request,
            path_params={"item_id": 7},
            allow_anonymous=True,
        )

        self.assertEqual(schema["route"], "GET items/{item_id}")
        self.assertIn("/v1/items/{item_id}", root_schema["paths"])
        self.assertEqual(denied_response.status_code, 401)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content), {"item_id": 7})
