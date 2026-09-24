"""Interfaces for composing a public MCA router with private MCA services."""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Any, Protocol

from pydantic import ValidationError

from .base import MCAError, RegisteredRoute
from .models import APIRouteSchemaOut, MCADiscoveryOut, MCAResponseOut


class MCAClient(Protocol):
    """Client boundary used by a router to reach a mounted MCA service."""

    def discover(
        self,
        *,
        guide: str | None = None,
        operation: str | None = None,
    ) -> Any:
        """Return a remote get_mca response as a model or JSON-like value."""

    def call(
        self,
        operation: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Any = None,
    ) -> Any:
        """Call a remote operation and return its JSON-compatible result."""


class MCACompositionMixin:
    """Shared explicit-composition behavior for MCA adapters."""

    _namespace_pattern = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")

    def __init__(self, *args: Any, **kwargs: Any):
        self._mounted_mcas: dict[str, MCAClient] = {}
        self._remote_schema_cache: dict[tuple[str, str], APIRouteSchemaOut] = {}
        super().__init__(*args, **kwargs)

    def mount(self, namespace: str, client: MCAClient) -> None:
        """Make a private MCA available for explicitly delegated routes."""
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
        if not callable(getattr(client, "discover", None)) or not callable(getattr(client, "call", None)):
            raise TypeError("MCA client must provide discover() and call() methods.")
        self._mounted_mcas[namespace] = client

    def clear_remote_schema_cache(
        self,
        namespace: str | None = None,
        operation: str | None = None,
    ) -> None:
        """Clear cached discovery schemas, optionally for one mounted operation."""
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
        target = options.get("delegate_to")
        if target is not None:
            self._delegate_target(target)

    def _delegate_target(self, target: Any) -> tuple[str, str]:
        if not isinstance(target, str):
            raise ValueError("delegate_to must use the form 'namespace.operation'.")
        namespace, separator, operation = target.partition(".")
        if not separator or not namespace or not operation:
            raise ValueError("delegate_to must use the form 'namespace.operation'.")
        if namespace not in self._mounted_mcas:
            raise ValueError(f"MCA namespace {namespace!r} is not mounted.")
        return namespace, operation

    def _route_metadata(self, endpoint: Any, options: Mapping[str, Any]) -> Mapping[str, Any]:
        self._validate_delegate_option(options)
        return super()._route_metadata(endpoint, options)

    def _validate_route_registration(
        self,
        path: str | None,
        method: str,
        operation: str,
    ) -> None:
        super()._validate_route_registration(path, method, operation)
        if any(
            operation == namespace or operation.startswith(f"{namespace}.")
            for namespace in self._mounted_mcas
        ):
            raise ValueError(f"MCA operation {operation!r} conflicts with a mounted namespace.")

    def _delegated_routes(self) -> tuple[RegisteredRoute, ...]:
        return tuple(
            route
            for route in self._routes.values()
            if route.operation != "get_mca"
            and route.meta("include_in_discovery", True)
            and route.meta("delegate_to") is not None
        )

    def _delegated_operations_by_namespace(self) -> dict[str, set[str]]:
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
        client = self._mounted_mcas[namespace]
        try:
            value = client.discover(guide=guide, operation=operation)
            model = MCAResponseOut if guide is None and operation is None else MCADiscoveryOut
            return model.model_validate(value).model_dump()
        except MCAError:
            raise
        except ValidationError as exc:
            raise MCAError(
                "invalid_upstream_response",
                f"Mounted MCA service '{namespace}' returned an invalid discovery response.",
                "service",
                502,
            ) from exc
        except Exception as exc:
            raise self._remote_error(namespace, "return discovery data", exc) from exc

    @staticmethod
    def _remote_error(namespace: str, action: str, exc: Exception) -> MCAError:
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
            remote_operations = result.get("operations")
            if not isinstance(remote_operations, Mapping):
                remote_operations = {}

            schemas: dict[tuple[str, str], APIRouteSchemaOut] = {}
            for operation in missing:
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

    @staticmethod
    def _is_generic_schema(schema: Any) -> bool:
        """Return whether a local schema carries no useful structural contract."""
        if not isinstance(schema, Mapping) or not schema:
            return True
        if "$ref" in schema:
            return False
        if schema.get("type") not in (None, "object"):
            return False
        return not any(
            key in schema
            for key in (
                "properties",
                "items",
                "enum",
                "const",
                "allOf",
                "anyOf",
                "oneOf",
            )
        )

    @staticmethod
    def _rewrite_component_refs(value: Any, names: Mapping[str, str]) -> Any:
        if isinstance(value, list):
            return [MCACompositionMixin._rewrite_component_refs(item, names) for item in value]
        if not isinstance(value, dict):
            return value
        rewritten = {
            key: MCACompositionMixin._rewrite_component_refs(item, names)
            for key, item in value.items()
        }
        reference = rewritten.get("$ref")
        prefix = "#/components/schemas/"
        if isinstance(reference, str) and reference.startswith(prefix):
            name = reference[len(prefix):]
            rewritten["$ref"] = f"{prefix}{names.get(name, name)}"
        return rewritten

    @classmethod
    def _merge_remote_fragment(
        cls,
        target_schema: dict[str, Any],
        remote_schema: Mapping[str, Any],
        fragment: Mapping[str, Any],
        public_operation: str,
    ) -> dict[str, Any]:
        """Copy a remote fragment and its components into a public schema."""
        remote_components = (
            remote_schema.get("components", {}).get("schemas", {})
            if isinstance(remote_schema.get("components", {}), Mapping)
            else {}
        )
        target_components = target_schema.setdefault("components", {}).setdefault("schemas", {})
        names: dict[str, str] = {}
        for name, component in remote_components.items():
            candidate = name
            if candidate in target_components and target_components[candidate] != component:
                candidate = f"{public_operation}__{name}"
                suffix = 2
                while candidate in target_components and target_components[candidate] != component:
                    candidate = f"{public_operation}__{name}_{suffix}"
                    suffix += 1
            names[name] = candidate

        for name, component in remote_components.items():
            candidate = names[name]
            if candidate not in target_components:
                target_components[candidate] = cls._rewrite_component_refs(
                    deepcopy(component),
                    names,
                )

        if not target_components:
            target_schema.pop("components", None)
        return cls._rewrite_component_refs(deepcopy(fragment), names)

    @classmethod
    def _materialize_schema(cls, schema: Any) -> Any:
        """Return a self-contained JSON Schema fragment without MCA components."""
        if not isinstance(schema, Mapping):
            return schema

        materialized = deepcopy(dict(schema))
        component_container = materialized.pop("components", {})
        components = (
            component_container.get("schemas", {})
            if isinstance(component_container, Mapping)
            else {}
        )
        if not isinstance(components, Mapping) or not components:
            return materialized

        resolving: set[str] = set()
        recursive = False

        def definitions_schema(value: Any) -> Any:
            if isinstance(value, list):
                return [definitions_schema(item) for item in value]
            if not isinstance(value, dict):
                return value
            reference = value.get("$ref")
            prefix = "#/components/schemas/"
            if isinstance(reference, str) and reference.startswith(prefix):
                name = reference[len(prefix):]
                if name in components:
                    rewritten: dict[str, Any] = {"$ref": f"#/$defs/{name}"}
                    rewritten.update(
                        {
                            key: definitions_schema(item)
                            for key, item in value.items()
                            if key != "$ref"
                        }
                    )
                    return rewritten
            return {key: definitions_schema(item) for key, item in value.items()}

        def expand(value: Any) -> Any:
            nonlocal recursive
            if isinstance(value, list):
                return [expand(item) for item in value]
            if not isinstance(value, dict):
                return value

            reference = value.get("$ref")
            prefix = "#/components/schemas/"
            if isinstance(reference, str) and reference.startswith(prefix):
                name = reference[len(prefix):]
                if name in components:
                    if name in resolving:
                        recursive = True
                        expanded: dict[str, Any] = {"$ref": f"#/$defs/{name}"}
                    else:
                        resolving.add(name)
                        expanded = expand(deepcopy(components[name]))
                        resolving.remove(name)
                    expanded.update(
                        {
                            key: expand(item)
                            for key, item in value.items()
                            if key != "$ref"
                        }
                    )
                    return expanded
            return {key: expand(item) for key, item in value.items()}

        materialized = expand(materialized)
        if recursive and isinstance(materialized, dict):
            materialized["$defs"] = {
                name: definitions_schema(component)
                for name, component in components.items()
            }
        return materialized

    def _compose_route_schema(
        self,
        route: RegisteredRoute,
        schema: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Overlay remote body/response schemas without exposing delegation metadata."""
        composed = deepcopy(dict(schema))
        target = route.meta("delegate_to")
        if target is None:
            for key in ("request_schema", "response_schema"):
                composed[key] = self._materialize_schema(composed.get(key))
            return composed

        namespace, operation = self._delegate_target(target)
        remote = self._remote_operation_schema(namespace, operation)

        remote_request = remote.request_schema or {}
        remote_body = (
            remote_request.get("properties", {}).get("body")
            if isinstance(remote_request.get("properties", {}), Mapping)
            else None
        )
        request_schema = composed.get("request_schema")
        if remote_body is not None and isinstance(request_schema, dict):
            request_properties = request_schema.setdefault("properties", {})
            local_body = request_properties.get("body")
            if local_body is not None and self._is_generic_schema(local_body):
                body_schema = self._merge_remote_fragment(
                    request_schema,
                    remote_request,
                    remote_body,
                    route.operation,
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

        remote_response = remote.response_schema
        if remote_response is not None:
            local_response = composed.get("response_schema")
            if local_response is None or self._is_generic_schema(local_response):
                response_container: dict[str, Any] = {}
                response_fragment = deepcopy(remote_response)
                response_fragment.pop("components", None)
                response_fragment = self._merge_remote_fragment(
                    response_container,
                    remote_response,
                    response_fragment,
                    route.operation,
                )
                if response_container.get("components"):
                    response_fragment["components"] = response_container["components"]
                composed["response_schema"] = response_fragment
        for key in ("request_schema", "response_schema"):
            composed[key] = self._materialize_schema(composed.get(key))
        return composed

    def _route_guides(self, route: RegisteredRoute) -> list[str] | None:
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

    def _exposed_remote_guides(self) -> set[str]:
        guides: set[str] = set()
        for route in self._delegated_routes():
            route_guides = self._route_guides(route) or []
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
