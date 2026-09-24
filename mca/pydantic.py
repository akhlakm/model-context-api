"""Reusable Pydantic MCA route registration, discovery, and dispatch."""

from __future__ import annotations

import inspect
import re
from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any, Callable, TypeVar, get_type_hints

from pydantic import BaseModel, TypeAdapter, ValidationError

from .base import BaseMCARouter, MCAError, RegisteredRoute
from .composition import MCAClient
from .models import (APIRouteSchemaOut, DiscoveryParams, ErrorOut,
                     MCADiscoveryOut, MCAResponseOut)

F = TypeVar("F", bound=Callable[..., Any])


class DispatchValidationError(ValueError):
    """Raised when dispatch input or output does not match its route schema."""

    def __init__(self, source: str, errors: list[dict[str, Any]]):
        self.source = source
        self.errors = errors
        super().__init__(f"Invalid {source}.")


def _validate_for_dispatch(annotation: Any, value: Any, source: str) -> Any:
    if annotation is None:
        return value
    try:
        return TypeAdapter(annotation).validate_python(value)
    except ValidationError as exc:
        raise DispatchValidationError(source, exc.errors()) from exc


def _validation_error_response(exc: DispatchValidationError) -> ErrorOut:
    first_error = exc.errors[0] if exc.errors else {}
    location = first_error.get("loc", ())
    field = ".".join(str(part) for part in location) or exc.source
    code = "invalid_response" if exc.source == "response" else "invalid_request"
    return ErrorOut(
        code=code,
        detail=str(first_error.get("msg", "Validation failed.")),
        field=field,
    )


class PydanticMCARouter(BaseMCARouter):
    _namespace_pattern = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")

    def __init__(self, *args: Any, **kwargs: Any):
        self._mounted_mcas: dict[str, MCAClient] = {}
        super().__init__(*args, **kwargs)

    def mount(self, namespace: str, client: MCAClient) -> None:
        """Mount a remote MCA under a stable public namespace."""
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
            raise ValueError(f"MCA namespace {namespace!r} conflicts with a local operation.")
        if not callable(getattr(client, "discover", None)) or not callable(getattr(client, "call", None)):
            raise TypeError("MCA client must provide discover() and call() methods.")
        self._mounted_mcas[namespace] = client

    def _validate_route_registration(
        self,
        path: str | None,
        method: str,
        operation: str,
    ) -> None:
        super()._validate_route_registration(path, method, operation)
        if any(operation == namespace or operation.startswith(f"{namespace}.") for namespace in self._mounted_mcas):
            raise ValueError(f"MCA operation {operation!r} conflicts with a mounted namespace.")

    @staticmethod
    def _validate_route_path(path: str | None) -> None:
        """Pydantic operations may be registered without an HTTP route."""

    def register(
        self,
        path: str | None = None,
        *,
        operation_id: str | None = None,
        **options: Any,
    ) -> Callable[[F], F]:
        return super().register(path, operation_id=operation_id, **options)

    def register_all(
        self,
        path: str | None = None,
        *,
        operation_id: str | None = None,
        methods: Iterable[str] | None = None,
        **options: Any,
    ) -> Callable[[F], F]:
        return super().register_all(
            path,
            operation_id=operation_id,
            methods=methods,
            **options,
        )

    def _discovery_endpoints(self) -> Iterable[tuple[str, str, F, Mapping[str, Any]]]:
        def get_mca(params: DiscoveryParams) -> MCAResponseOut | MCADiscoveryOut:
            return self._get_mca(params)

        return (
            (
                self.mca_path,
                "get_mca",
                get_mca,
                {"description": "Discover engine guides and read operation schemas."},
            ),
        )

    def _register_transport_route(self, route: RegisteredRoute, endpoint: F, options: Mapping[str, Any], **_: Any) -> F:
        return endpoint

    def _route_metadata(self, endpoint: F, options: Mapping[str, Any]) -> Mapping[str, Any]:
        parameters = inspect.signature(endpoint).parameters
        type_hints = get_type_hints(endpoint)
        params_parameter = parameters.get("params")
        body_parameter = parameters.get("data")
        return {
            "params_type": type_hints.get("params") if "params" in parameters else None,
            "body_type": type_hints.get("data") if "data" in parameters else None,
            "response_type": type_hints.get("return"),
            "params_required": (
                params_parameter is not None
                and params_parameter.default is inspect.Parameter.empty
            ),
            "body_required": (
                body_parameter is not None
                and body_parameter.default is inspect.Parameter.empty
            ),
        }

    @staticmethod
    def _schema_parts(annotation: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        if annotation is None or annotation is Any:
            return {}, {}
        schema = TypeAdapter(annotation).json_schema(
            ref_template="#/components/schemas/{model}"
        )
        components = schema.pop("$defs", {})
        return schema, components

    @staticmethod
    def _is_model_type(annotation: Any) -> bool:
        return isinstance(annotation, type) and issubclass(annotation, BaseModel)

    @classmethod
    def _referenced_schema(
        cls,
        annotation: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        schema, components = cls._schema_parts(annotation)
        if cls._is_model_type(annotation):
            name = annotation.__name__
            components[name] = schema
            return {"$ref": f"#/components/schemas/{name}"}, components
        return schema, components

    @staticmethod
    def _path_parameter_names(route: str | None) -> set[str]:
        if route is None:
            return set()
        return {
            parameter.split(":")[-1]
            for parameter in re.findall(r"\{([^}]+)\}", route)
        }

    def _route_schema(self, route: RegisteredRoute) -> APIRouteSchemaOut:
        request_properties: dict[str, Any] = {}
        request_required: list[str] = []
        components: dict[str, Any] = {}

        params_type = route.meta("params_type")
        body_type = route.meta("body_type")
        response_type = route.meta("response_type")
        params_schema, params_components = self._schema_parts(params_type)
        components.update(params_components)
        params_properties = params_schema.get("properties", {})
        params_required = set(params_schema.get("required", []))
        path_names = self._path_parameter_names(route.path)

        path_properties: dict[str, Any] = {}
        path_required: list[str] = []
        query_properties: dict[str, Any] = {}
        query_required: list[str] = []
        for name, property_schema in params_properties.items():
            property_schema = deepcopy(property_schema)
            property_schema.setdefault("description", f"{name} request parameter.")
            if name in path_names:
                path_properties[name] = property_schema
                path_required.append(name)
            else:
                query_properties[name] = property_schema
                if name in params_required:
                    query_required.append(name)

        if path_properties:
            request_properties["path_params"] = {
                "description": "Values captured from the selected operation route.",
                "type": "object",
                "properties": path_properties,
                "required": path_required,
            }
            request_required.append("path_params")
        if query_properties:
            request_properties["query_params"] = {
                "description": "Values supplied as operation query parameters.",
                "type": "object",
                "properties": query_properties,
                "required": query_required,
            }
            if query_required:
                request_required.append("query_params")

        body_required = bool(route.meta("body_required"))
        if body_type is not None and body_type is not Any:
            body_schema, body_components = self._referenced_schema(body_type)
            components.update(body_components)
            request_properties["body"] = {
                "description": "JSON request body.",
                **body_schema,
            }
            if body_required:
                request_required.append("body")

        request_schema = None
        if request_properties:
            request_schema = {
                "type": "object",
                "properties": request_properties,
                "required": request_required,
            }
            if components:
                request_schema["components"] = {"schemas": components}

        response_schema = None
        if response_type is not None and response_type is not Any:
            response_schema, response_components = self._referenced_schema(response_type)
            if response_components:
                response_schema["components"] = {"schemas": response_components}

        return APIRouteSchemaOut(
            route=route.discovery_route,
            description=route.description,
            guides=route.meta("guides"),
            request_schema=request_schema,
            response_schema=response_schema,
        )

    @staticmethod
    def _query_names(value: str | None) -> list[str]:
        return [] if value is None else [item.strip() for item in value.split(",")]

    def _split_query_names(
        self,
        value: str | None,
        separator: str,
    ) -> tuple[list[str], dict[str, list[str]]]:
        local_names: list[str] = []
        mounted_names: dict[str, list[str]] = {}
        for name in self._query_names(value):
            namespace, delimiter, remote_name = name.partition(separator)
            if delimiter and namespace in self._mounted_mcas:
                mounted_names.setdefault(namespace, []).append(remote_name)
            else:
                local_names.append(name)
        return local_names, mounted_names

    @staticmethod
    def _remote_error(namespace: str, action: str, exc: Exception) -> MCAError:
        return MCAError(
            "upstream_unavailable",
            f"Mounted MCA service '{namespace}' could not {action}.",
            "service",
            502,
        )

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
    def _namespaced_operation_description(name: str, description: Any) -> str:
        text = str(description)
        _, separator, detail = text.partition(" - ")
        return f"{name} - {detail if separator else text}"

    @staticmethod
    def _namespaced_operation_schema(
        namespace: str,
        name: str,
        value: Any,
    ) -> dict[str, Any]:
        schema = APIRouteSchemaOut.model_validate(value).model_dump()
        schema["route"] = f"{namespace}.{name}"
        if schema.get("guides") is not None:
            schema["guides"] = [f"{namespace}/{guide}" for guide in schema["guides"]]
        return schema

    def _merge_mounted_root(self, result: dict[str, Any]) -> dict[str, Any]:
        operations = dict(result.get("available_operations", {}))
        guides = list(result.get("available_guides") or [])
        for namespace in self._mounted_mcas:
            remote = self._remote_discovery(namespace)
            for name, description in remote.get("available_operations", {}).items():
                public_name = f"{namespace}.{name}"
                operations[public_name] = self._namespaced_operation_description(public_name, description)
            guides.extend(
                f"{namespace}/{name}"
                for name in remote.get("available_guides", []) or []
            )
        result["available_operations"] = dict(sorted(operations.items()))
        if guides:
            result["available_guides"] = sorted(set(guides))
        return result

    def _merge_mounted_details(
        self,
        result: dict[str, Any],
        mounted_guides: Mapping[str, list[str]],
        mounted_operations: Mapping[str, list[str]],
    ) -> dict[str, Any]:
        for namespace in self._mounted_mcas:
            guides = mounted_guides.get(namespace, [])
            operations = mounted_operations.get(namespace, [])
            if not guides and not operations:
                continue
            remote = self._remote_discovery(
                namespace,
                guide=",".join(guides) if guides else None,
                operation=",".join(operations) if operations else None,
            )
            if remote.get("guides") is not None:
                result.setdefault("guides", {}).update(
                    {
                        f"{namespace}/{name}": content
                        for name, content in remote["guides"].items()
                    }
                )
            if remote.get("operations") is not None:
                result.setdefault("operations", {}).update(
                    {
                        f"{namespace}.{name}": self._namespaced_operation_schema(
                            namespace,
                            name,
                            schema,
                        )
                        for name, schema in remote["operations"].items()
                    }
                )
        return result

    def _get_mca(self, params: DiscoveryParams) -> MCAResponseOut | MCADiscoveryOut:
        if params.guide is None and params.operation_name is None:
            return MCAResponseOut(**self._merge_mounted_root(
                self.discovery(None, None, self._route_schema)
            ))

        local_guides, mounted_guides = self._split_query_names(params.guide, "/")
        local_operations, mounted_operations = self._split_query_names(params.operation_name, ".")
        result: dict[str, Any] = {}
        if local_guides or local_operations:
            result.update(
                self.discovery(
                    ",".join(local_guides) if local_guides else None,
                    ",".join(local_operations) if local_operations else None,
                    self._route_schema,
                )
            )
        result = self._merge_mounted_details(result, mounted_guides, mounted_operations)
        return MCADiscoveryOut(**result)

    def _mounted_operation(self, operation: str) -> tuple[str, str] | None:
        namespace, separator, remote_operation = operation.partition(".")
        if separator and namespace in self._mounted_mcas:
            return namespace, remote_operation
        return None

    def _dispatch_mounted(
        self,
        namespace: str,
        operation: str,
        params: dict[str, Any] | None,
        data: Any,
    ) -> Any:
        if not operation:
            raise MCAError(
                "unknown_operation",
                "A mounted operation name is required.",
                "operation",
                404,
            )
        try:
            return self._mounted_mcas[namespace].call(
                operation,
                params=params,
                data=data,
            )
        except MCAError:
            raise
        except Exception as exc:
            raise self._remote_error(namespace, "complete the operation", exc) from exc

    def _dispatch_registered(
        self,
        route: RegisteredRoute,
        params: dict[str, Any] | None,
        data: Any,
    ) -> Any:
        params_type = route.meta("params_type")
        body_type = route.meta("body_type")
        validated_params = _validate_for_dispatch(params_type, {} if params is None else params, "params")
        validated_body = _validate_for_dispatch(body_type, data, "data")

        kwargs: dict[str, Any] = {}
        if params_type is not None:
            kwargs["params"] = validated_params
        if body_type is not None:
            kwargs["data"] = validated_body

        result = route.endpoint(**kwargs)
        return _validate_for_dispatch(route.meta("response_type"), result, "response")

    def _dispatch_error(self, error: MCAError) -> ErrorOut:
        code = "unknown_operation" if error.code == "unknown_endpoint" else error.code
        field = "operation" if error.code == "unknown_endpoint" else error.field
        return ErrorOut(code=code, detail=error.detail, field=field)

    def dispatch(
        self,
        operation: str | None = None,
        params: dict[str, Any] | None = None,
        data: Any = None,
        *,
        method: str = "GET",
    ) -> Any:
        try:
            if operation is not None and not operation.startswith("/"):
                mounted_operation = self._mounted_operation(operation)
                if mounted_operation is not None:
                    namespace, remote_operation = mounted_operation
                    return self._dispatch_mounted(namespace, remote_operation, params, data)
            return super().dispatch(
                operation,
                params,
                data,
                method=method,
            )
        except DispatchValidationError as exc:
            return _validation_error_response(exc)
        except MCAError as exc:
            return self._dispatch_error(exc)
        except Exception:
            return ErrorOut(
                code="internal_error",
                detail="The endpoint could not complete the operation.",
                field="operation",
            )
