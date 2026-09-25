import json
from pathlib import Path
from tempfile import TemporaryDirectory

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

from unittest import IsolatedAsyncioTestCase, TestCase

from ninja import NinjaAPI, Router

from mca.base import MCAError
from mca.ninja import NinjaMCARouter


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.openapi_calls = 0

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
        self.openapi_calls += 1
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
                "/invoices/{invoice_id}": {
                    "get": {
                        "operationId": "get_invoice",
                        "description": "Read a public invoice.",
                        "parameters": [
                            {
                                "name": "invoice_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "integer"},
                            },
                        ],
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {"type": "object"},
                                },
                            },
                        },
                        "responses": {
                            "200": {
                                "description": "OK",
                                "content": {
                                    "application/json": {"schema": {"type": "object"}},
                                },
                            },
                        },
                    },
                },
            },
            "components": {"schemas": {}},
        }


class FakeMCAClient:
    def __init__(self, *, fail=False):
        self.discovery_calls = []
        self.calls = []
        self.fail = fail

    def discover(self, *, guide=None, operation=None):
        self.discovery_calls.append((guide, operation))
        if self.fail:
            raise RuntimeError("connection refused")
        if guide is not None:
            return {
                "guides": {
                    name: f"# {name.removesuffix('.md')}"
                    for name in guide.split(",")
                }
            }
        return {
            "operations": {
                name: {
                    "route": "GET private/invoices/{invoice_id}",
                    "description": "Read a private invoice.",
                    "guides": ["invoices.md"],
                    "request_schema": None,
                    "response_schema": {"type": "object"},
                }
                for name in (operation or "").split(",")
            }
        }

    def call(self, operation, *, params=None, data=None):
        self.calls.append((operation, params, data))
        return {"invoice_id": params["invoice_id"]}


class NinjaMCARouterPackageTests(TestCase):
    def test_routes_are_required_for_ninja_registration(self):
        api = FakeAPI()
        router = NinjaMCARouter(api)

        with self.assertRaises(TypeError):
            router.register()

        with self.assertRaisesRegex(ValueError, "route path must be specified"):
            @router.register(None)
            def get_missing_route():
                return None

        with self.assertRaisesRegex(ValueError, "route path must be specified"):
            @router.register_all(None, methods=("GET",))
            def missing_route_all():
                return None

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
        router = NinjaMCARouter(api, usage="Use operation discovery.")

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

        self.assertEqual(discovery["usage"], "Use operation discovery.")
        self.assertNotIn("help", discovery)
        self.assertNotIn("available_guides", discovery)
        self.assertEqual(documented["description"], "Read documented data.")
        self.assertNotIn("guides", guided)
        documented_call = next(call for call in api.calls if call[2].get("operation_id") == "get_documented")
        self.assertEqual(documented_call[2]["description"], "Read documented data.")

    def test_discovery_omits_missing_index(self):
        with TemporaryDirectory() as directory:
            Path(directory, "workflow.md").write_text("# Workflow", encoding="utf-8")
            router = NinjaMCARouter(FakeAPI(), guides_dir=directory)

            discovery = router._get_context(None, None)

            self.assertNotIn("help", discovery)
            self.assertIn("usage", discovery)
            self.assertEqual(discovery["available_guides"], ["workflow.md"])

    def test_route_schema_reads_openapi_document_once(self):
        api = FakeAPI()
        router = NinjaMCARouter(api)

        @router.register("/invoices/{invoice_id}", response=dict)
        def get_invoice(request, invoice_id: int):
            return {"invoice_id": invoice_id}

        api.openapi_calls = 0
        router._route_schema(router.route("get_invoice"))

        self.assertEqual(api.openapi_calls, 1)

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

    def test_private_mounts_require_explicit_public_routes(self):
        api = FakeAPI()
        router = NinjaMCARouter(api)
        client = FakeMCAClient()
        router.mount("billing", client)

        @router.register(
            "/invoices/{invoice_id}",
            response=dict,
            delegate_to="billing.get_invoice",
        )
        def get_invoice(request, invoice_id: int):
            return {"invoice_id": invoice_id}

        registered_paths = [call[1] for call in api.calls]
        self.assertEqual(registered_paths.count("/invoices/{invoice_id}"), 1)
        self.assertNotIn("/private/invoices/{invoice_id}", registered_paths)
        self.assertTrue(all("delegate_to" not in call[2] for call in api.calls))

        schema = router._route_schema(router.route("get_invoice"))
        self.assertEqual(schema["route"], "GET invoices/{invoice_id}")
        self.assertEqual(schema["description"], "Read a public invoice.")
        self.assertEqual(schema["guides"], ["billing/invoices.md"])
        self.assertEqual(
            schema["request_schema"]["properties"]["path_params"]["properties"]["invoice_id"]["type"],
            "integer",
        )

        root = router._get_context(None, None)
        self.assertEqual(set(root["available_operations"]), {"get_invoice"})
        self.assertEqual(root["available_guides"], ["billing/invoices.md"])
        self.assertNotIn("billing.get_invoice", root["available_operations"])

        details = router._get_context("billing/invoices.md", None)
        self.assertEqual(details["guides"], {"billing/invoices.md": "# invoices"})
        operation_details = router._get_context(None, "get_invoice")
        self.assertIn("get_invoice", operation_details["operations"])
        with self.assertRaisesRegex(MCAError, "Unavailable operation"):
            router._get_context(None, "billing.get_invoice")

    def test_delegated_schema_composes_remote_body_and_response_and_caches(self):
        class SchemaClient(FakeMCAClient):
            def discover(self, *, guide=None, operation=None):
                if operation == "get_invoice":
                    self.discovery_calls.append((guide, operation))
                    return {
                        "operations": {
                            "get_invoice": {
                                "route": "GET private/invoices/{invoice_id}",
                                "description": "Read a private invoice.",
                                "request_schema": {
                                    "type": "object",
                                    "properties": {
                                        "body": {
                                            "$ref": "#/components/schemas/PrivateFilter",
                                        },
                                    },
                                    "required": ["body"],
                                    "components": {
                                        "schemas": {
                                            "PrivateFilter": {
                                                "type": "object",
                                                "properties": {
                                                    "include_history": {"type": "boolean"},
                                                },
                                            },
                                        },
                                    },
                                },
                                "response_schema": {
                                    "$ref": "#/components/schemas/PrivateInvoice",
                                    "components": {
                                        "schemas": {
                                            "PrivateInvoice": {
                                                "type": "object",
                                                "properties": {
                                                    "invoice_id": {"type": "integer"},
                                                },
                                            },
                                        },
                                    },
                                },
                            },
                        },
                    }
                return super().discover(guide=guide, operation=operation)

        api = FakeAPI()
        router = NinjaMCARouter(api)
        client = SchemaClient()
        router.mount("billing", client)

        @router.register(
            "/invoices/{invoice_id}",
            response=dict,
            delegate_to="billing.get_invoice",
        )
        def get_invoice(request, invoice_id: int):
            return {}

        schema = router._route_schema(router.route("get_invoice"))
        repeated = router._route_schema(router.route("get_invoice"))
        request_schema = schema["request_schema"]
        response_schema = schema["response_schema"]

        self.assertIn("path_params", request_schema["properties"])
        self.assertEqual(
            request_schema["properties"]["body"]["type"],
            "object",
        )
        self.assertEqual(
            response_schema["type"],
            "object",
        )
        self.assertIn("include_history", request_schema["properties"]["body"]["properties"])
        self.assertIn("invoice_id", response_schema["properties"])
        self.assertNotIn("components", request_schema)
        self.assertNotIn("components", response_schema)
        self.assertNotIn("#/components/", repr(schema))
        self.assertEqual(schema, repeated)
        self.assertEqual(client.discovery_calls.count((None, "get_invoice")), 1)

        router.clear_remote_schema_cache("billing", "get_invoice")
        router._route_schema(router.route("get_invoice"))
        self.assertEqual(client.discovery_calls.count((None, "get_invoice")), 2)

    def test_schema_materializer_uses_defs_for_recursive_components(self):
        router = NinjaMCARouter(FakeAPI())

        schema = router._materialize_schema(
            {
                "$ref": "#/components/schemas/Node",
                "components": {
                    "schemas": {
                        "Node": {
                            "type": "object",
                            "properties": {
                                "value": {"type": "integer"},
                                "child": {"$ref": "#/components/schemas/Node"},
                            },
                        },
                    },
                },
            }
        )

        self.assertEqual(schema["type"], "object")
        self.assertEqual(schema["properties"]["value"]["type"], "integer")
        self.assertEqual(
            schema["properties"]["child"]["$ref"],
            "#/$defs/Node",
        )
        self.assertIn("Node", schema["$defs"])
        self.assertNotIn("components", schema)

    def test_public_handler_can_authenticate_then_call_private_client(self):
        events = []

        def authenticate(request):
            events.append("auth")
            return "principal"

        api_router = Router(auth=authenticate)
        registry = NinjaMCARouter(api_router)
        client = FakeMCAClient()
        registry.mount("billing", client)

        @registry.register(
            "/invoices/{invoice_id}",
            response=dict,
            delegate_to="billing.get_invoice",
        )
        def get_invoice(request, invoice_id: int):
            events.append(("handler", request.auth))
            return client.call("get_invoice", params={"invoice_id": invoice_id})

        response = registry.execute_http(
            "get_invoice",
            RequestFactory().get("/invoices/7"),
            path_params={"invoice_id": 7},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content), {"invoice_id": 7})
        discovery = registry._get_context(None, None)
        self.assertIn("get_invoice", discovery["available_operations"])
        self.assertEqual(events, ["auth", ("handler", "principal")])
        self.assertEqual(client.calls, [("get_invoice", {"invoice_id": 7}, None)])

    def test_endpoint_mca_errors_become_structured_http_responses(self):
        registry = NinjaMCARouter(Router())

        @registry.register("/invalid", response=dict)
        def get_invalid(request):
            raise MCAError("invalid_request", "Missing required field.", "data", 422)

        response = registry.execute_http(
            "get_invalid",
            RequestFactory().get("/invalid"),
            allow_anonymous=True,
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            json.loads(response.content),
            {
                "code": "invalid_request",
                "detail": "Missing required field.",
                "field": "data",
            },
        )

    def test_only_guides_attached_to_exposed_routes_are_published(self):
        class Client(FakeMCAClient):
            def discover(self, *, guide=None, operation=None):
                if operation == "get_hidden":
                    return {
                        "operations": {
                            "get_hidden": {
                                "route": "GET hidden",
                                "description": "Hidden.",
                                "guides": ["hidden.md"],
                            }
                        }
                    }
                return super().discover(guide=guide, operation=operation)

        api = FakeAPI()
        router = NinjaMCARouter(api)
        router.mount("billing", Client())

        @router.register(
            "/invoices/{invoice_id}",
            response=dict,
            delegate_to="billing.get_invoice",
        )
        def get_invoice(request, invoice_id: int):
            return {"invoice_id": invoice_id}

        @router.register(
            "/hidden",
            response=dict,
            delegate_to="billing.get_hidden",
            include_in_discovery=False,
        )
        def get_hidden(request):
            return {}

        root = router._get_context(None, None)
        self.assertNotIn("get_hidden", root["available_operations"])
        self.assertEqual(root["available_guides"], ["billing/invoices.md"])

    def test_private_discovery_failures_are_reported_as_upstream_errors(self):
        api = FakeAPI()
        router = NinjaMCARouter(api)
        router.mount("billing", FakeMCAClient(fail=True))

        @router.register(
            "/invoices/{invoice_id}",
            response=dict,
            delegate_to="billing.get_invoice",
        )
        def get_invoice(request, invoice_id: int):
            return {"invoice_id": invoice_id}

        with self.assertRaises(MCAError) as context:
            router._get_context(None, None)
        self.assertEqual(context.exception.code, "upstream_unavailable")
        self.assertEqual(context.exception.status, 502)


class AsyncNinjaMCARouterTests(IsolatedAsyncioTestCase):
    async def test_async_only_client_can_mount_and_discover(self):
        class AsyncOnlyClient:
            async def adiscover(self, *, guide=None, operation=None):
                return {
                    "operations": {
                        "get_invoice": {
                            "route": "GET private/invoices/{invoice_id}",
                            "description": "Read an invoice.",
                            "guides": [],
                            "request_schema": None,
                            "response_schema": {"type": "object"},
                        }
                    }
                }

        registry = NinjaMCARouter(Router())
        registry.mount("billing", AsyncOnlyClient())

        @registry.register(
            "/invoices/{invoice_id}",
            response=dict,
            delegate_to="billing.get_invoice",
        )
        def get_invoice(request, invoice_id: int):
            return {"invoice_id": invoice_id}

        response = await registry.execute_http_async(
            "get_context",
            RequestFactory().get("/"),
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("get_invoice", json.loads(response.content)["available_operations"])

    async def test_async_discovery_awaits_mounted_client(self):
        class AsyncClient:
            def __init__(self):
                self.sync_calls = []
                self.async_calls = []

            def discover(self, **kwargs):
                self.sync_calls.append(kwargs)
                raise AssertionError("sync discovery should not be used")

            async def adiscover(self, *, guide=None, operation=None):
                self.async_calls.append((guide, operation))
                return {
                    "operations": {
                        name: {
                            "route": f"GET private/{name}",
                            "description": f"Read {name}.",
                            "guides": ["invoices.md"],
                            "request_schema": None,
                            "response_schema": {"type": "object"},
                        }
                        for name in (operation or "").split(",")
                    }
                }

            def call(self, operation, *, params=None, data=None):
                return {}

            async def acall(self, operation, *, params=None, data=None):
                return {}

        api = Router()
        registry = NinjaMCARouter(api)
        client = AsyncClient()
        registry.mount("billing", client)

        @registry.register(
            "/invoices/{invoice_id}",
            response=dict,
            delegate_to="billing.get_invoice",
        )
        def get_invoice(request, invoice_id: int):
            return {"invoice_id": invoice_id}

        response = await registry.execute_http_async(
            "get_context",
            RequestFactory().get("/"),
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("get_invoice", json.loads(response.content)["available_operations"])
        self.assertEqual(client.sync_calls, [])
        self.assertEqual(client.async_calls, [(None, "get_invoice")])

    async def test_async_discovery_requires_async_mounted_client(self):
        registry = NinjaMCARouter(Router())
        client = FakeMCAClient()
        registry.mount("billing", client)

        @registry.register(
            "/invoices/{invoice_id}",
            response=dict,
            delegate_to="billing.get_invoice",
        )
        def get_invoice(request, invoice_id: int):
            return {"invoice_id": invoice_id}

        response = await registry.execute_http_async(
            "get_context",
            RequestFactory().get("/"),
        )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            json.loads(response.content),
            {
                "code": "async_client_required",
                "detail": "Mounted MCA service 'billing' does not provide adiscover().",
                "field": "service",
            },
        )
