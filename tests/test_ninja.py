from django.conf import settings
from pathlib import Path

if not settings.configured:
    settings.configure(
        DEFAULT_CHARSET="utf-8",
        INSTALLED_APPS=[],
        SECRET_KEY="mca-test-key",
    )

import django

django.setup()

from unittest import TestCase

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
