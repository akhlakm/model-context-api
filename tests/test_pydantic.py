from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import IsolatedAsyncioTestCase, TestCase

from pydantic import BaseModel

from mca.base import MCAError
from mca.models import MCAResponseOut
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
        discovery = self.router.dispatch("get_context")
        result = self.router.dispatch("/items/7", method="GET")

        self.assertIsInstance(discovery, MCAResponseOut)
        self.assertEqual(discovery.title, "Model Context API")
        self.assertEqual(discovery.version, 1.0)
        self.assertIn("index.md", discovery.available_guides)
        details = self.router.dispatch("get_context", params={"operation": "get_item"})
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
        details = router.dispatch("get_context", params={"operation": "get_status"})

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

        discovery = router.dispatch("get_context")

        self.assertEqual(discovery.title, "Example API")
        self.assertEqual(discovery.version, 2.5)

    def test_guides_can_be_disabled_and_help_can_be_overridden(self):
        router = PydanticMCARouter(help="Use operation discovery.")

        @router.register("/guided", guides=["workflow.md"])
        def get_guided() -> ItemOut:
            """Read guided data."""
            return ItemOut(item_id=1)

        discovery = router.dispatch("get_context")
        details = router.dispatch("get_context", params={"operation": "get_guided"})
        self.assertEqual(discovery.help, "Use operation discovery.")
        self.assertNotIn("index", discovery.model_dump())
        self.assertNotIn("available_guides", discovery.model_dump())
        self.assertNotIn("guides", details.operations["get_guided"].model_dump())
        with self.assertRaises(MCAError) as context:
            router.dispatch("get_context", params={"guide": "workflow.md"})
        self.assertEqual(context.exception.code, "unknown_guides")
        self.assertEqual(context.exception.status, 404)

    def test_discovery_omits_missing_index(self):
        with TemporaryDirectory() as directory:
            Path(directory, "workflow.md").write_text("# Workflow", encoding="utf-8")
            router = PydanticMCARouter(guides_dir=directory)

            discovery = router.dispatch("get_context")
            payload = discovery.model_dump()

            self.assertNotIn("index", payload)
            self.assertEqual(payload["available_guides"], ["workflow.md"])

    def test_operation_schema_can_list_relevant_guides(self):
        @self.router.register("/guided", guides=["workflow.md"])
        def get_guided() -> ItemOut:
            return ItemOut(item_id=1)

        details = self.router.dispatch("get_context", params={"operation": "get_guided"})

        self.assertEqual(details.operations["get_guided"].model_dump()["guides"], ["workflow.md"])

    def test_discovery_omits_unrequested_content(self):
        details = self.router.dispatch("get_context", params={"guide": "index.md"})

        self.assertEqual(set(details.model_dump()), {"guides"})

    def test_invalid_dispatch_raises_structured_error(self):
        with self.assertRaises(MCAError) as context:
            self.router.dispatch("/items/not-an-int", method="GET")

        self.assertEqual(context.exception.code, "invalid_request")
        self.assertEqual(context.exception.field, "item_id")
        self.assertEqual(context.exception.status, 422)

    def test_invalid_response_raises_server_error(self):
        router = PydanticMCARouter()

        @router.register()
        def get_invalid_response() -> ItemOut:
            return {"wrong": "shape"}

        with self.assertRaises(MCAError) as context:
            router.dispatch("get_invalid_response")

        self.assertEqual(context.exception.code, "invalid_response")
        self.assertEqual(context.exception.status, 500)

    def test_unexpected_endpoint_failure_raises_internal_error(self):
        router = PydanticMCARouter()

        @router.register()
        def get_broken() -> ItemOut:
            raise RuntimeError("database unavailable")

        with self.assertRaises(MCAError) as context:
            router.dispatch("get_broken")

        self.assertEqual(context.exception.code, "internal_error")
        self.assertEqual(context.exception.status, 500)

    def test_mount_requires_explicit_public_routes(self):
        client = FakeMCAClient()
        self.router.mount("billing", client)

        discovery = self.router.dispatch("get_context")
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

        discovery = self.router.dispatch("get_context")
        details = self.router.dispatch(
            "get_context",
            params={"operation": "get_public_invoice"},
        )
        result = self.router.dispatch(
            "get_public_invoice",
            params={"item_id": 7},
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
        with self.assertRaises(MCAError) as context:
            self.router.dispatch(
                "billing.get_invoice",
                params={"invoice_id": 7},
            )
        self.assertEqual(context.exception.code, "unknown_operation")
        self.assertEqual(context.exception.status, 404)
        self.assertEqual(client.calls, [("get_invoice", {"invoice_id": 7}, None)])

        with self.assertRaises(MCAError) as context:
            self.router.dispatch(
                "get_context",
                params={"operation": "billing.get_invoice"},
            )
        self.assertEqual(context.exception.code, "unknown_operation")
        self.assertEqual(context.exception.status, 404)

        with self.assertRaises(MCAError) as context:
            self.router.dispatch(
                "get_context",
                params={"guide": "billing/index.md"},
            )
        self.assertEqual(context.exception.code, "unknown_guides")
        self.assertEqual(context.exception.status, 404)

    def test_delegated_schema_composes_remote_body_and_response_for_generic_types(self):
        class SchemaClient(FakeMCAClient):
            def discover(self, *, guide=None, operation=None):
                if operation == "make_invoice":
                    self.discovery_calls.append((guide, operation))
                    return {
                        "operations": {
                            "make_invoice": {
                                "route": "POST private/invoices",
                                "description": "Create an invoice.",
                                "request_schema": {
                                    "type": "object",
                                    "properties": {
                                        "body": {
                                            "$ref": "#/components/schemas/PrivateInvoiceCreate",
                                        },
                                    },
                                    "required": ["body"],
                                    "components": {
                                        "schemas": {
                                            "PrivateInvoiceCreate": {
                                                "type": "object",
                                                "properties": {
                                                    "customer": {"type": "string"},
                                                    "total": {"type": "number"},
                                                },
                                                "required": ["customer", "total"],
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

        router = PydanticMCARouter()
        client = SchemaClient()
        router.mount("billing", client)

        @router.register(
            "/invoices",
            delegate_to="billing.make_invoice",
        )
        def make_invoice(data: dict[str, Any]) -> dict[str, Any]:
            return client.call("make_invoice", data=data)

        @router.register(
            "/typed-invoices",
            delegate_to="billing.make_invoice",
        )
        def make_typed_invoice(data: ItemOut) -> ItemOut:
            return ItemOut(**client.call("make_invoice", data=data.model_dump()))

        details = router.dispatch(
            "get_context",
            params={"operation": "make_invoice"},
        )
        schema = details.operations["make_invoice"].model_dump()

        self.assertEqual(
            schema["request_schema"]["properties"]["body"]["type"],
            "object",
        )
        self.assertEqual(
            schema["response_schema"]["type"],
            "object",
        )
        self.assertIn("customer", schema["request_schema"]["properties"]["body"]["properties"])
        self.assertIn("invoice_id", schema["response_schema"]["properties"])
        self.assertNotIn("components", schema["request_schema"])
        self.assertNotIn("components", schema["response_schema"])
        self.assertEqual(client.discovery_calls.count((None, "make_invoice")), 1)

        typed_details = router.dispatch(
            "get_context",
            params={"operation": "make_typed_invoice"},
        )
        typed_schema = typed_details.operations["make_typed_invoice"].model_dump()
        self.assertEqual(
            typed_schema["request_schema"]["properties"]["body"]["type"],
            "object",
        )
        self.assertEqual(
            typed_schema["response_schema"]["type"],
            "object",
        )
        self.assertIn("item_id", typed_schema["response_schema"]["properties"])

    def test_delegated_schema_discovery_batches_per_namespace(self):
        class BatchClient:
            def __init__(self):
                self.discovery_calls = []

            def discover(self, *, guide=None, operation=None):
                self.discovery_calls.append((guide, operation))
                return {
                    "operations": {
                        name: {
                            "route": f"GET private/{name}",
                            "description": f"Read {name}.",
                            "request_schema": None,
                            "response_schema": {
                                "type": "object",
                                "properties": {"operation": {"const": name}},
                            },
                        }
                        for name in (operation or "").split(",")
                    }
                }

            def call(self, operation, *, params=None, data=None):
                return {"operation": operation}

        router = PydanticMCARouter()
        billing = BatchClient()
        inventory = BatchClient()
        router.mount("billing", billing)
        router.mount("inventory", inventory)

        @router.register(
            "/first",
            operation_id="get_public_first",
            delegate_to="billing.get_first",
        )
        def get_public_first() -> dict[str, Any]:
            return {}

        @router.register(
            "/second",
            operation_id="get_public_second",
            delegate_to="billing.get_second",
        )
        def get_public_second() -> dict[str, Any]:
            return {}

        @router.register(
            "/shared",
            operation_id="get_public_shared",
            delegate_to="billing.get_first",
        )
        def get_public_shared() -> dict[str, Any]:
            return {}

        @router.register(
            "/other",
            operation_id="get_public_other",
            delegate_to="inventory.get_other",
        )
        def get_public_other() -> dict[str, Any]:
            return {}

        router.dispatch("get_context")
        router.dispatch("get_context")

        self.assertEqual(billing.discovery_calls, [(None, "get_first,get_second")])
        self.assertEqual(inventory.discovery_calls, [(None, "get_other")])

        router.clear_remote_schema_cache()
        router.dispatch("get_context", params={"operation": "get_public_first"})

        self.assertEqual(
            billing.discovery_calls,
            [(None, "get_first,get_second"), (None, "get_first,get_second")],
        )
        self.assertEqual(
            inventory.discovery_calls,
            [(None, "get_other"), (None, "get_other")],
        )

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
            "get_context",
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

        class PartialClient:
            def discover(self, **kwargs):
                return {}

        self.router.mount("partial", PartialClient())

        with self.assertRaisesRegex(TypeError, r"discover\(\) or adiscover\(\)"):
            self.router.mount("missing", object())

        failing_router = PydanticMCARouter()

        class FailingClient(FakeMCAClient):
            def discover(self, **kwargs):
                raise RuntimeError("connection refused")

        failing_router.mount("private", FailingClient())
        @failing_router.register(delegate_to="private.get_status")
        def get_status() -> ItemOut:
            return ItemOut(item_id=1)

        with self.assertRaises(MCAError) as context:
            failing_router.dispatch("get_context")

        self.assertEqual(context.exception.code, "upstream_unavailable")
        self.assertEqual(context.exception.field, "service")
        self.assertEqual(context.exception.status, 502)


class AsyncPydanticMCARouterTests(IsolatedAsyncioTestCase):
    async def test_sync_discovery_rejects_async_only_client(self):
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

        router = PydanticMCARouter()
        router.mount("billing", AsyncOnlyClient())

        @router.register(
            "/invoices/{invoice_id}",
            delegate_to="billing.get_invoice",
        )
        def get_public_invoice() -> dict[str, Any]:
            return {}

        with self.assertRaises(MCAError) as context:
            router.dispatch("get_context", params={"operation": "get_public_invoice"})

        self.assertEqual(context.exception.code, "sync_client_required")
        self.assertEqual(context.exception.status, 500)

    async def test_async_discovery_uses_adiscover_and_composes_schema(self):
        class AsyncClient:
            def __init__(self):
                self.sync_calls = []
                self.async_calls = []

            def discover(self, **kwargs):
                self.sync_calls.append(kwargs)
                raise AssertionError("sync discovery should not be used")

            async def adiscover(self, *, guide=None, operation=None):
                self.async_calls.append((guide, operation))
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
                            "route": f"GET private/{name}",
                            "description": f"Read {name}.",
                            "guides": ["invoices.md"],
                            "request_schema": None,
                            "response_schema": {
                                "type": "object",
                                "properties": {"invoice_id": {"type": "integer"}},
                            },
                        }
                        for name in (operation or "").split(",")
                    }
                }

            def call(self, operation, *, params=None, data=None):
                return {}

            async def acall(self, operation, *, params=None, data=None):
                return {}

        router = PydanticMCARouter()
        client = AsyncClient()
        router.mount("billing", client)

        @router.register(
            "/invoices/{invoice_id}",
            delegate_to="billing.get_invoice",
        )
        def get_public_invoice() -> dict[str, Any]:
            return {}

        details = await router.adispatch(
            "get_context",
            params={"operation": "get_public_invoice"},
        )

        self.assertIn("get_public_invoice", details.operations)
        self.assertEqual(
            details.operations["get_public_invoice"].response_schema["type"],
            "object",
        )
        self.assertEqual(client.sync_calls, [])
        self.assertEqual(client.async_calls, [(None, "get_invoice")])

        guide_details = await router.adispatch(
            "get_context",
            params={"guide": "billing/invoices.md"},
        )
        self.assertEqual(
            guide_details.guides,
            {"billing/invoices.md": "# invoices"},
        )
        self.assertEqual(
            client.async_calls,
            [(None, "get_invoice"), ("invoices.md", None)],
        )

    async def test_async_dispatch_awaits_registered_endpoint(self):
        router = PydanticMCARouter()

        @router.register()
        async def get_async_item() -> ItemOut:
            return ItemOut(item_id=7)

        result = await router.adispatch("get_async_item")

        self.assertEqual(result.item_id, 7)
