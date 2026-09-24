"""Interfaces for composing a public MCA router with private MCA services."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import Any, Protocol

from pydantic import ValidationError

from .base import MCAError, RegisteredRoute
from .models import APIRouteSchemaOut, MCADiscoveryOut, MCAResponseOut
from .schema import (is_generic_schema, materialize_schema,
                     merge_remote_fragment, rewrite_component_refs)


class MCAClient(Protocol):
    """Client boundary used by a public router to reach a private MCA service.

    Implementations may use HTTP, JSON-RPC, a message bus, or an in-process
    adapter. Synchronous composition uses ``discover`` and ``call``; async
    handlers can use their ``adiscover`` and ``acall`` counterparts.
    """

    def discover(
        self,
        *,
        guide: str | None = None,
        operation: str | None = None,
    ) -> Any:
        """Return a remote ``get_mca`` response as a model or JSON-like value.

        ``guide`` and ``operation`` may contain comma-separated names when the
        transport supports batched discovery.

        Raises:
            MCAError: When the mounted service reports a discovery failure.
        """

    def call(
        self,
        operation: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Any = None,
    ) -> Any:
        """Call a remote operation and return its JSON-compatible result.

        ``params`` carries path/query-style values and ``data`` carries the
        operation body. The composition layer does not prescribe the RPC wire
        format.

        Raises:
            MCAError: When the mounted service reports an operation failure.
        """

    async def adiscover(
        self,
        *,
        guide: str | None = None,
        operation: str | None = None,
    ) -> Any:
        """Asynchronously return a remote ``get_mca`` response.

        This is the async counterpart to :meth:`discover`. The values use the
        same batched guide and operation format.

        Raises:
            MCAError: When the mounted service reports a discovery failure.
        """

    async def acall(
        self,
        operation: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Any = None,
    ) -> Any:
        """Asynchronously call a remote operation.

        This is the async counterpart to :meth:`call`; transports should
        await their underlying network or RPC request here.

        Raises:
            MCAError: When the mounted service reports an operation failure.
        """


class MCACompositionMixin:
    """Add explicit private-router composition to a concrete MCA adapter.

    A mounted client is never exposed as an independent public route. A local
    route opts into composition with ``delegate_to="namespace.operation"``;
    only that public route's discovery schema and guides are enriched.
    """

    _namespace_pattern = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")

    def __init__(self, *args: Any, **kwargs: Any):
        """Initialize mounted clients and the cache of remote operation schemas."""
        self._mounted_mcas: dict[str, MCAClient] = {}
        self._remote_schema_cache: dict[tuple[str, str], APIRouteSchemaOut] = {}
        super().__init__(*args, **kwargs)

    def mount(self, namespace: str, client: MCAClient) -> None:
        """Mount a private MCA for explicitly delegated public routes.

        ``namespace`` becomes the first segment of every ``delegate_to``
        target. Mounting alone does not expose or merge any private operation.

        The client must provide either the synchronous pair ``discover`` and
        ``call``, the asynchronous pair ``adiscover`` and ``acall``, or both.
        """
        if not isinstance(namespace, str) or self._namespace_pattern.fullmatch(namespace) is None:
            raise ValueError(
                "MCA mount namespace must start with a letter and contain only letters, numbers, '-' or '_'."
            )
        if namespace in self._mounted_mcas:
            raise ValueError(f"MCA namespace {namespace!r} is already mounted.")
        if any(
            route.operation == namespace or route.operation.startswith(f"{namespace}.")
            for route in self._routes.values()
        ):
            raise ValueError(f"MCA namespace {namespace!r} conflicts with a public operation.")
        sync_methods = (
            callable(getattr(client, "discover", None)),
            callable(getattr(client, "call", None)),
        )
        async_methods = (
            callable(getattr(client, "adiscover", None)),
            callable(getattr(client, "acall", None)),
        )
        if any(sync_methods) and not all(sync_methods):
            raise TypeError("MCA client must provide both discover() and call() methods.")
        if any(async_methods) and not all(async_methods):
            raise TypeError("MCA client must provide both adiscover() and acall() methods.")
        if not all(sync_methods) and not all(async_methods):
            raise TypeError(
                "MCA client must provide discover()/call() or adiscover()/acall() methods."
            )
        self._mounted_mcas[namespace] = client

    def clear_remote_schema_cache(
        self,
        namespace: str | None = None,
        operation: str | None = None,
    ) -> None:
        """Clear cached remote schemas globally or for one mounted operation.

        Use this after a private service deploys a schema change. Supplying an
        operation requires its namespace; omitting both clears the full cache.
        """
        if operation is not None and namespace is None:
            raise ValueError("A namespace is required when clearing one operation.")
        if namespace is None:
            self._remote_schema_cache.clear()
            return
        if namespace not in self._mounted_mcas:
            raise ValueError(f"MCA namespace {namespace!r} is not mounted.")
        if operation is None:
            for key in tuple(self._remote_schema_cache):
                if key[0] == namespace:
                    del self._remote_schema_cache[key]
            return
        self._remote_schema_cache.pop((namespace, operation), None)

    def _validate_delegate_option(self, options: Mapping[str, Any]) -> None:
        """Validate a route's optional private operation target."""
        target = options.get("delegate_to")
        if target is not None:
            self._delegate_target(target)

    def _delegate_target(self, target: Any) -> tuple[str, str]:
        """Parse and validate a ``namespace.operation`` delegation target."""
        if not isinstance(target, str):
            raise ValueError("delegate_to must use the form 'namespace.operation'.")
        namespace, separator, operation = target.partition(".")
        if not separator or not namespace or not operation:
            raise ValueError("delegate_to must use the form 'namespace.operation'.")
        if namespace not in self._mounted_mcas:
            raise ValueError(f"MCA namespace {namespace!r} is not mounted.")
        return namespace, operation

    def _route_metadata(self, endpoint: Any, options: Mapping[str, Any]) -> Mapping[str, Any]:
        """Validate composition options before the base router records metadata."""
        self._validate_delegate_option(options)
        return super()._route_metadata(endpoint, options)

    def _validate_route_registration(
        self,
        path: str | None,
        method: str,
        operation: str,
    ) -> None:
        """Prevent public operation names from colliding with mount namespaces."""
        super()._validate_route_registration(path, method, operation)
        if any(
            operation == namespace or operation.startswith(f"{namespace}.")
            for namespace in self._mounted_mcas
        ):
            raise ValueError(f"MCA operation {operation!r} conflicts with a mounted namespace.")

    def _delegated_routes(self) -> tuple[RegisteredRoute, ...]:
        """Return visible public routes that explicitly delegate to a private MCA."""
        return tuple(
            route
            for route in self._routes.values()
            if route.operation != "get_mca"
            and route.meta("include_in_discovery", True)
            and route.meta("delegate_to") is not None
        )

    def _delegated_operations_by_namespace(self) -> dict[str, set[str]]:
        """Group delegated private operation names by mounted namespace."""
        operations: dict[str, set[str]] = {}
        for route in self._delegated_routes():
            namespace, operation = self._delegate_target(route.meta("delegate_to"))
            operations.setdefault(namespace, set()).add(operation)
        return operations

    def _remote_discovery(
        self,
        namespace: str,
        *,
        guide: str | None = None,
        operation: str | None = None,
    ) -> dict[str, Any]:
        """Fetch and validate one mounted service's discovery response."""
        client = self._mounted_mcas[namespace]
        discover = getattr(client, "discover", None)
        if not callable(discover):
            raise MCAError(
                "sync_client_required",
                f"Mounted MCA service '{namespace}' does not provide discover().",
                "service",
                500,
            )
        try:
            return self._validate_remote_discovery(
                namespace,
                discover(guide=guide, operation=operation),
                guide=guide,
                operation=operation,
            )
        except MCAError:
            raise
        except Exception as exc:
            raise self._remote_error(namespace, "return discovery data") from exc

    def _validate_remote_discovery(
        self,
        namespace: str,
        value: Any,
        *,
        guide: str | None,
        operation: str | None,
    ) -> dict[str, Any]:
        """Validate and normalize a discovery response from a mounted service."""
        try:
            model = MCAResponseOut if guide is None and operation is None else MCADiscoveryOut
            return model.model_validate(value).model_dump()
        except ValidationError as exc:
            raise MCAError(
                "invalid_upstream_response",
                f"Mounted MCA service '{namespace}' returned an invalid discovery response.",
                "service",
                502,
            ) from exc

    async def _remote_discovery_async(
        self,
        namespace: str,
        *,
        guide: str | None = None,
        operation: str | None = None,
    ) -> dict[str, Any]:
        """Asynchronously fetch and validate one mounted discovery response."""
        client = self._mounted_mcas[namespace]
        discover = getattr(client, "adiscover", None)
        if not callable(discover):
            raise MCAError(
                "async_client_required",
                f"Mounted MCA service '{namespace}' does not provide adiscover().",
                "service",
                500,
            )
        try:
            value = await discover(guide=guide, operation=operation)
            return self._validate_remote_discovery(
                namespace,
                value,
                guide=guide,
                operation=operation,
            )
        except MCAError:
            raise
        except Exception as exc:
            raise self._remote_error(namespace, "return discovery data") from exc

    @staticmethod
    def _remote_error(namespace: str, action: str) -> MCAError:
        """Create the stable public error used when a mounted service fails."""
        return MCAError(
            "upstream_unavailable",
            f"Mounted MCA service '{namespace}' could not {action}.",
            "service",
            502,
        )

    def _remote_operation_schema(
        self,
        namespace: str,
        operation: str,
    ) -> APIRouteSchemaOut:
        """Return one cached remote schema, fetching missing schemas in batches."""
        cache_key = (namespace, operation)
        cached = self._remote_schema_cache.get(cache_key)
        if cached is not None:
            return cached
        operations_by_namespace = self._delegated_operations_by_namespace()
        operations_by_namespace.setdefault(namespace, set()).add(operation)
        self._prefetch_remote_schemas(operations_by_namespace)
        schema = self._remote_schema_cache.get(cache_key)
        if schema is None:
            raise MCAError(
                "unknown_operation",
                f"Mounted MCA service '{namespace}' has no operation '{operation}'.",
                "operation",
                404,
            )
        return schema

    def _prefetch_remote_schemas(
        self,
        operations_by_namespace: Mapping[str, set[str]] | None = None,
    ) -> None:
        """Fetch all missing delegated schemas in one request per service."""
        grouped = (
            self._delegated_operations_by_namespace()
            if operations_by_namespace is None
            else operations_by_namespace
        )
        for namespace in sorted(grouped):
            missing = sorted(
                operation
                for operation in grouped[namespace]
                if (namespace, operation) not in self._remote_schema_cache
            )
            if not missing:
                continue

            result = self._remote_discovery(namespace, operation=",".join(missing))
            self._cache_remote_schemas(namespace, missing, result)

    def _cache_remote_schemas(
        self,
        namespace: str,
        operations: list[str],
        result: Mapping[str, Any],
    ) -> None:
        """Validate and cache a batch of operation schemas."""
        remote_operations = result.get("operations")
        if not isinstance(remote_operations, Mapping):
            remote_operations = {}

        schemas: dict[tuple[str, str], APIRouteSchemaOut] = {}
        for operation in operations:
            if operation not in remote_operations:
                raise MCAError(
                    "unknown_operation",
                    f"Mounted MCA service '{namespace}' has no operation '{operation}'.",
                    "operation",
                    404,
                )
            try:
                schemas[(namespace, operation)] = APIRouteSchemaOut.model_validate(
                    remote_operations[operation]
                )
            except ValidationError as exc:
                raise MCAError(
                    "invalid_upstream_response",
                    f"Mounted MCA service '{namespace}' returned an invalid operation schema.",
                    "service",
                    502,
                ) from exc

        self._remote_schema_cache.update(schemas)

    async def _prefetch_remote_schemas_async(
        self,
        operations_by_namespace: Mapping[str, set[str]] | None = None,
    ) -> None:
        """Asynchronously fetch all missing delegated schemas per service."""
        grouped = (
            self._delegated_operations_by_namespace()
            if operations_by_namespace is None
            else operations_by_namespace
        )
        for namespace in sorted(grouped):
            missing = sorted(
                operation
                for operation in grouped[namespace]
                if (namespace, operation) not in self._remote_schema_cache
            )
            if missing:
                result = await self._remote_discovery_async(
                    namespace,
                    operation=",".join(missing),
                )
                self._cache_remote_schemas(namespace, missing, result)

    async def _remote_operation_schema_async(
        self,
        namespace: str,
        operation: str,
    ) -> APIRouteSchemaOut:
        """Return one async-fetched cached remote operation schema."""
        cache_key = (namespace, operation)
        cached = self._remote_schema_cache.get(cache_key)
        if cached is not None:
            return cached
        operations_by_namespace = self._delegated_operations_by_namespace()
        operations_by_namespace.setdefault(namespace, set()).add(operation)
        await self._prefetch_remote_schemas_async(operations_by_namespace)
        schema = self._remote_schema_cache.get(cache_key)
        if schema is None:
            raise MCAError(
                "unknown_operation",
                f"Mounted MCA service '{namespace}' has no operation '{operation}'.",
                "operation",
                404,
            )
        return schema

    def _composed_discovery(
        self,
        guide: str | None,
        operation_name: str | None,
        schema_factory: Callable[[RegisteredRoute], Any],
    ) -> dict[str, Any]:
        """Combine local discovery with only the remote guides and schemas exposed publicly.

        Root discovery batches schema requests per mounted service. Guide
        requests are grouped by namespace so one request can satisfy several
        nested private guide names.
        """
        if guide is None and operation_name is None:
            result = self.discovery(None, None, schema_factory)
            remote_guides = self._exposed_remote_guides()
            if remote_guides:
                result["available_guides"] = sorted(
                    set(result.get("available_guides") or []) | remote_guides
                )
            return result

        local_guides, mounted_guides = self._split_remote_guides(guide)
        result: dict[str, Any] = {}
        if local_guides or operation_name is not None:
            result.update(
                self.discovery(
                    ",".join(local_guides) if local_guides else None,
                    operation_name,
                    schema_factory,
                )
            )
        for namespace, guides in mounted_guides.items():
            remote = self._remote_discovery(namespace, guide=",".join(guides))
            if remote.get("guides") is not None:
                result.setdefault("guides", {}).update(
                    {
                        f"{namespace}/{name}": content
                        for name, content in remote["guides"].items()
                    }
                )
        return result

    async def _composed_discovery_async(
        self,
        guide: str | None,
        operation_name: str | None,
        schema_factory: Callable[[RegisteredRoute], Any],
    ) -> dict[str, Any]:
        """Asynchronously combine local discovery with mounted MCA data."""
        if guide is None and operation_name is None:
            result = await self.adiscovery(None, None, schema_factory)
            remote_guides = await self._exposed_remote_guides_async()
            if remote_guides:
                result["available_guides"] = sorted(
                    set(result.get("available_guides") or []) | remote_guides
                )
            return result

        local_guides, mounted_guides = await self._split_remote_guides_async(guide)
        result: dict[str, Any] = {}
        if local_guides or operation_name is not None:
            result.update(
                await self.adiscovery(
                    ",".join(local_guides) if local_guides else None,
                    operation_name,
                    schema_factory,
                )
            )
        for namespace, guides in mounted_guides.items():
            remote = await self._remote_discovery_async(
                namespace,
                guide=",".join(guides),
            )
            if remote.get("guides") is not None:
                result.setdefault("guides", {}).update(
                    {
                        f"{namespace}/{name}": content
                        for name, content in remote["guides"].items()
                    }
                )
        return result

    @staticmethod
    def _is_generic_schema(schema: Any) -> bool:
        """Return whether a local schema is too generic to improve a remote one."""
        return is_generic_schema(schema)

    @staticmethod
    def _rewrite_component_refs(value: Any, names: Mapping[str, str]) -> Any:
        """Rewrite component references after names are collision-resolved."""
        return rewrite_component_refs(value, names)

    @staticmethod
    def _merge_remote_fragment(
        target_schema: dict[str, Any],
        remote_schema: Mapping[str, Any],
        fragment: Mapping[str, Any],
        public_operation: str,
    ) -> dict[str, Any]:
        """Merge a remote schema fragment and its referenced components into a target."""
        return merge_remote_fragment(
            target_schema,
            remote_schema,
            fragment,
            public_operation,
        )

    @staticmethod
    def _materialize_schema(schema: Any) -> Any:
        """Inline component references for the public discovery payload."""
        return materialize_schema(schema)

    def _compose_remote_request(
        self,
        composed: dict[str, Any],
        remote_request: Mapping[str, Any],
        public_operation: str,
    ) -> None:
        """Replace a generic public body schema with the delegated body contract."""
        remote_properties = remote_request.get("properties", {})
        remote_body = (
            remote_properties.get("body")
            if isinstance(remote_properties, Mapping)
            else None
        )
        request_schema = composed.get("request_schema")
        if remote_body is None or not isinstance(request_schema, dict):
            return

        request_properties = request_schema.setdefault("properties", {})
        local_body = request_properties.get("body")
        if local_body is None or not self._is_generic_schema(local_body):
            return

        body_schema = self._merge_remote_fragment(
            request_schema,
            remote_request,
            remote_body,
            public_operation,
        )
        if (
            isinstance(local_body, Mapping)
            and local_body.get("description") is not None
            and "description" not in body_schema
        ):
            body_schema["description"] = local_body["description"]
        request_properties["body"] = body_schema

        required = request_schema.setdefault("required", [])
        if "body" in remote_request.get("required", []):
            if "body" not in required:
                required.append("body")
        else:
            request_schema["required"] = [name for name in required if name != "body"]

    def _compose_remote_response(
        self,
        composed: dict[str, Any],
        remote_response: Mapping[str, Any] | None,
        public_operation: str,
    ) -> None:
        """Replace a generic public response schema with the delegated response contract."""
        if remote_response is None:
            return
        local_response = composed.get("response_schema")
        if local_response is not None and not self._is_generic_schema(local_response):
            return

        response_container: dict[str, Any] = {}
        response_fragment = deepcopy(dict(remote_response))
        response_fragment.pop("components", None)
        response_fragment = self._merge_remote_fragment(
            response_container,
            remote_response,
            response_fragment,
            public_operation,
        )
        if response_container.get("components"):
            response_fragment["components"] = response_container["components"]
        composed["response_schema"] = response_fragment

    def _materialize_composed_schema(self, schema: dict[str, Any]) -> dict[str, Any]:
        """Make request and response schemas self-contained before serialization."""
        for key in ("request_schema", "response_schema"):
            schema[key] = self._materialize_schema(schema.get(key))
        return schema

    def _compose_route_schema(
        self,
        route: RegisteredRoute,
        schema: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Overlay remote body/response schemas without exposing delegation metadata."""
        composed = deepcopy(dict(schema))
        target = route.meta("delegate_to")
        if target is not None:
            namespace, operation = self._delegate_target(target)
            remote = self._remote_operation_schema(namespace, operation)
            self._compose_remote_request(
                composed,
                remote.request_schema or {},
                route.operation,
            )
            self._compose_remote_response(
                composed,
                remote.response_schema,
                route.operation,
            )
        return self._materialize_composed_schema(composed)

    async def _compose_route_schema_async(
        self,
        route: RegisteredRoute,
        schema: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Asynchronously overlay remote schemas onto one local route schema."""
        composed = deepcopy(dict(schema))
        target = route.meta("delegate_to")
        if target is not None:
            namespace, operation = self._delegate_target(target)
            remote = await self._remote_operation_schema_async(namespace, operation)
            self._compose_remote_request(
                composed,
                remote.request_schema or {},
                route.operation,
            )
            self._compose_remote_response(
                composed,
                remote.response_schema,
                route.operation,
            )
        return self._materialize_composed_schema(composed)

    def _route_guides(self, route: RegisteredRoute) -> list[str] | None:
        """Return local guides plus namespaced guides advertised by a delegate."""
        guides = list(route.meta("guides") or [])
        target = route.meta("delegate_to")
        if target is not None:
            namespace, operation = self._delegate_target(target)
            remote_schema = self._remote_operation_schema(namespace, operation)
            guides.extend(
                f"{namespace}/{name}"
                for name in remote_schema.guides or []
            )
        return list(dict.fromkeys(guides)) or None

    async def _route_guides_async(self, route: RegisteredRoute) -> list[str] | None:
        """Asynchronously return local and delegated guide names for a route."""
        guides = list(route.meta("guides") or [])
        target = route.meta("delegate_to")
        if target is not None:
            namespace, operation = self._delegate_target(target)
            remote_schema = await self._remote_operation_schema_async(namespace, operation)
            guides.extend(
                f"{namespace}/{name}"
                for name in remote_schema.guides or []
            )
        return list(dict.fromkeys(guides)) or None

    def _exposed_remote_guides(self) -> set[str]:
        """Return private guide paths reachable through visible delegated routes."""
        guides: set[str] = set()
        for route in self._delegated_routes():
            route_guides = self._route_guides(route) or []
            guides.update(
                guide
                for guide in route_guides
                if "/" in guide and guide.partition("/")[0] in self._mounted_mcas
            )
        return guides

    async def _exposed_remote_guides_async(self) -> set[str]:
        """Asynchronously return private guides reachable through public routes."""
        guides: set[str] = set()
        for route in self._delegated_routes():
            route_guides = await self._route_guides_async(route) or []
            guides.update(
                guide
                for guide in route_guides
                if "/" in guide and guide.partition("/")[0] in self._mounted_mcas
            )
        return guides

    def _split_remote_guides(
        self,
        value: str | None,
    ) -> tuple[list[str], dict[str, list[str]]]:
        """Separate local guide names from validated namespaced private guides."""
        names = [] if value is None else [item.strip() for item in value.split(",")]
        remote_names = {
            name
            for name in names
            if "/" in name and name.partition("/")[0] in self._mounted_mcas
        }
        exposed = self._exposed_remote_guides() if remote_names else set()
        local_names: list[str] = []
        mounted_names: dict[str, list[str]] = {}
        for name in names:
            namespace, separator, remote_name = name.partition("/")
            if separator and namespace in self._mounted_mcas:
                if name not in exposed:
                    raise MCAError(
                        "unknown_guides",
                        f"Unavailable guide(s): {name}.",
                        "guide",
                        404,
                    )
                mounted_names.setdefault(namespace, []).append(remote_name)
            else:
                local_names.append(name)
        return local_names, mounted_names

    async def _split_remote_guides_async(
        self,
        value: str | None,
    ) -> tuple[list[str], dict[str, list[str]]]:
        """Asynchronously separate local and validated private guide names."""
        names = [] if value is None else [item.strip() for item in value.split(",")]
        remote_names = {
            name
            for name in names
            if "/" in name and name.partition("/")[0] in self._mounted_mcas
        }
        exposed = await self._exposed_remote_guides_async() if remote_names else set()
        local_names: list[str] = []
        mounted_names: dict[str, list[str]] = {}
        for name in names:
            namespace, separator, remote_name = name.partition("/")
            if separator and namespace in self._mounted_mcas:
                if name not in exposed:
                    raise MCAError(
                        "unknown_guides",
                        f"Unavailable guide(s): {name}.",
                        "guide",
                        404,
                    )
                mounted_names.setdefault(namespace, []).append(remote_name)
            else:
                local_names.append(name)
        return local_names, mounted_names
