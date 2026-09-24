"""Reusable Pydantic MCA route registration, discovery, and dispatch."""

from __future__ import annotations

import inspect
import re
from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any, Callable, NoReturn, TypeVar, get_type_hints

from pydantic import BaseModel, TypeAdapter, ValidationError

from .base import BaseMCARouter, MCAError, RegisteredRoute
from .composition import MCACompositionMixin
from .models import (APIRouteSchemaOut, DiscoveryParams, MCADiscoveryOut,
                     MCAResponseOut)
from .schema import attach_components, build_request_schema

F = TypeVar("F", bound=Callable[..., Any])


class DispatchValidationError(ValueError):
    """Raised when dispatch input or output does not match its route schema."""

    def __init__(self, source: str, errors: list[dict[str, Any]]):
        """Store the validation source and Pydantic's structured errors."""
        self.source = source
        self.errors = errors
        super().__init__(f"Invalid {source}.")


def _validate_for_dispatch(annotation: Any, value: Any, source: str) -> Any:
    """Validate one dispatch value with its route annotation when present."""
    if annotation is None:
        return value
    try:
        return TypeAdapter(annotation).validate_python(value)
    except ValidationError as exc:
        raise DispatchValidationError(source, exc.errors()) from exc


def _validation_error(exc: DispatchValidationError) -> MCAError:
    """Convert a validation failure into a transport-neutral MCA error."""
    first_error = exc.errors[0] if exc.errors else {}
    location = first_error.get("loc", ())
    field = ".".join(str(part) for part in location) or exc.source
    code = "invalid_response" if exc.source == "response" else "invalid_request"
    status = 500 if code == "invalid_response" else 422
    return MCAError(
        code=code,
        detail=str(first_error.get("msg", "Validation failed.")),
        field=field,
        status=status,
    )


class PydanticMCARouter(MCACompositionMixin, BaseMCARouter):
    """Framework-independent MCA router for typed Python callables.

    Operations may have an HTTP-style path for discovery and route dispatch,
    or omit the path entirely when they are callable only by operation name.
    Pydantic annotations validate parameters, request bodies, and responses.
    """

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
        """Register a typed operation, optionally with an HTTP-style path."""
        return super().register(path, operation_id=operation_id, **options)

    def register_all(
        self,
        path: str | None = None,
        *,
        operation_id: str | None = None,
        methods: Iterable[str] | None = None,
        **options: Any,
    ) -> Callable[[F], F]:
        """Register several method-prefixed operations from one callable."""
        return super().register_all(
            path,
            operation_id=operation_id,
            methods=methods,
            **options,
        )

    def _discovery_endpoints(self) -> Iterable[tuple[str, str, F, Mapping[str, Any]]]:
        """Define the Pydantic discovery operation used by the base router."""
        def get_mca(params: DiscoveryParams) -> MCAResponseOut | MCADiscoveryOut:
            """Return the discovery response selected by the query parameters."""
            return self._get_mca(params)

        return (
            (
                self.mca_path,
                "get_mca",
                get_mca,
                {"description": "Discover engine guides and read operation schemas."},
            ),
        )

    def _register_transport_route(
        self,
        route: RegisteredRoute,
        endpoint: F,
        options: Mapping[str, Any],
        **_: Any,
    ) -> F:
        """Keep Pydantic registration in-process rather than adding transport routes."""
        return endpoint

    def _route_metadata(self, endpoint: F, options: Mapping[str, Any]) -> Mapping[str, Any]:
        """Capture endpoint annotations and whether params/body are required."""
        metadata = dict(super()._route_metadata(endpoint, options))
        parameters = inspect.signature(endpoint).parameters
        type_hints = get_type_hints(endpoint)
        params_parameter = parameters.get("params")
        body_parameter = parameters.get("data")
        metadata.update({
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
        })
        return metadata

    @staticmethod
    def _schema_parts(annotation: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        """Convert an annotation to a schema fragment and its named components."""
        if annotation is None or annotation is Any:
            return {}, {}
        schema = TypeAdapter(annotation).json_schema(
            ref_template="#/components/schemas/{model}"
        )
        components = schema.pop("$defs", {})
        return schema, components

    @staticmethod
    def _is_model_type(annotation: Any) -> bool:
        """Return whether an annotation is a concrete Pydantic model class."""
        return isinstance(annotation, type) and issubclass(annotation, BaseModel)

    @classmethod
    def _referenced_schema(
        cls,
        annotation: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Represent model classes as component references and inline other types."""
        schema, components = cls._schema_parts(annotation)
        if cls._is_model_type(annotation):
            name = annotation.__name__
            components[name] = schema
            return {"$ref": f"#/components/schemas/{name}"}, components
        return schema, components

    @staticmethod
    def _path_parameter_names(route: str | None) -> set[str]:
        """Extract parameter names captured by an HTTP route template."""
        if route is None:
            return set()
        return {
            parameter.split(":")[-1]
            for parameter in re.findall(r"\{([^}]+)\}", route)
        }

    def _parameter_sections(
        self,
        route: RegisteredRoute,
    ) -> tuple[dict[str, dict[str, Any]], set[str], dict[str, Any]]:
        """Split the typed ``params`` model into path and query sections."""
        components: dict[str, Any] = {}
        params_schema, params_components = self._schema_parts(route.meta("params_type"))
        components.update(params_components)
        params_properties = params_schema.get("properties", {})
        params_required = set(params_schema.get("required", []))
        path_names = self._path_parameter_names(route.path)

        path_properties: dict[str, Any] = {}
        query_properties: dict[str, Any] = {}
        path_required: list[str] = []
        query_required: list[str] = []
        for name, property_schema in params_properties.items():
            # Path fields are required by the route; query fields retain the
            # requiredness declared by the Pydantic parameter model.
            property_schema = deepcopy(property_schema)
            property_schema.setdefault("description", f"{name} request parameter.")
            if name in path_names:
                path_properties[name] = property_schema
                path_required.append(name)
            else:
                query_properties[name] = property_schema
                if name in params_required:
                    query_required.append(name)

        sections: dict[str, dict[str, Any]] = {}
        if path_properties:
            sections["path_params"] = {
                "description": "Values captured from the selected operation route.",
                "properties": path_properties,
                "required": path_required,
            }
        if query_properties:
            sections["query_params"] = {
                "description": "Values supplied as operation query parameters.",
                "properties": query_properties,
                "required": query_required,
            }
        required_sections: set[str] = set()
        if path_properties:
            required_sections.add("path_params")
        if query_required:
            required_sections.add("query_params")
        return sections, required_sections, components

    def _request_schema(self, route: RegisteredRoute) -> dict[str, Any] | None:
        """Build the logical request envelope for one typed operation."""
        sections, required_sections, components = self._parameter_sections(route)
        body_schema = None
        body_type = route.meta("body_type")
        if body_type is not None and body_type is not Any:
            body_schema, body_components = self._referenced_schema(body_type)
            components.update(body_components)
        return build_request_schema(
            sections,
            required_sections,
            body_schema=body_schema,
            body_required=bool(route.meta("body_required")),
            components=components,
        )

    def _response_schema(self, route: RegisteredRoute) -> dict[str, Any] | None:
        """Build the successful response schema for one typed operation."""
        response_type = route.meta("response_type")
        if response_type is None or response_type is Any:
            return None
        response_schema, response_components = self._referenced_schema(response_type)
        return attach_components(response_schema, response_components)

    def _route_schema(self, route: RegisteredRoute) -> APIRouteSchemaOut:
        """Build and compose the discovery schema for one operation."""
        local_schema = APIRouteSchemaOut(
            route=route.discovery_route,
            description=route.description,
            guides=self._route_guides(route),
            request_schema=self._request_schema(route),
            response_schema=self._response_schema(route),
        )

        return APIRouteSchemaOut.model_validate(
            self._compose_route_schema(route, local_schema.model_dump())
        )

    async def _aroute_schema(self, route: RegisteredRoute) -> APIRouteSchemaOut:
        """Build and asynchronously compose one operation discovery schema."""
        local_schema = APIRouteSchemaOut(
            route=route.discovery_route,
            description=route.description,
            guides=await self._route_guides_async(route),
            request_schema=self._request_schema(route),
            response_schema=self._response_schema(route),
        )
        composed = await self._compose_route_schema_async(
            route,
            local_schema.model_dump(),
        )
        return APIRouteSchemaOut.model_validate(composed)

    def _get_mca(self, params: DiscoveryParams) -> MCAResponseOut | MCADiscoveryOut:
        """Serve root, guide, or operation discovery through the typed adapter."""
        result = self._composed_discovery(
            params.guide,
            params.operation_name,
            self._route_schema,
        )
        response_model = (
            MCAResponseOut
            if params.guide is None and params.operation_name is None
            else MCADiscoveryOut
        )
        return response_model(**result)

    async def _aget_mca(self, params: DiscoveryParams) -> MCAResponseOut | MCADiscoveryOut:
        """Serve discovery asynchronously, including mounted MCA calls."""
        result = await self._composed_discovery_async(
            params.guide,
            params.operation_name,
            self._aroute_schema,
        )
        response_model = (
            MCAResponseOut
            if params.guide is None and params.operation_name is None
            else MCADiscoveryOut
        )
        return response_model(**result)

    def _dispatch_arguments(
        self,
        route: RegisteredRoute,
        params: dict[str, Any] | None,
        data: Any,
    ) -> tuple[dict[str, Any], Any]:
        """Validate dispatch input and return endpoint keyword arguments."""
        params_type = route.meta("params_type")
        body_type = route.meta("body_type")
        validated_params = _validate_for_dispatch(
            params_type,
            {} if params is None else params,
            "params",
        )
        validated_body = _validate_for_dispatch(body_type, data, "data")

        kwargs: dict[str, Any] = {}
        if params_type is not None:
            kwargs["params"] = validated_params
        if body_type is not None:
            kwargs["data"] = validated_body
        return kwargs, validated_params

    def _dispatch_registered(
        self,
        route: RegisteredRoute,
        params: dict[str, Any] | None,
        data: Any,
    ) -> Any:
        """Validate inputs, invoke the callable, and validate its response."""
        kwargs, _ = self._dispatch_arguments(route, params, data)
        result = route.endpoint(**kwargs)
        if inspect.isawaitable(result):
            raise MCAError(
                "async_operation",
                "Use adispatch() for asynchronous operations.",
                "operation",
                500,
            )
        return _validate_for_dispatch(route.meta("response_type"), result, "response")

    async def _adispatch_registered(
        self,
        route: RegisteredRoute,
        params: dict[str, Any] | None,
        data: Any,
    ) -> Any:
        """Validate, await, and validate one typed async operation."""
        kwargs, validated_params = self._dispatch_arguments(route, params, data)
        if route.operation == "get_mca":
            result = await self._aget_mca(validated_params)
        else:
            result = route.endpoint(**kwargs)
            if inspect.isawaitable(result):
                result = await result
        return _validate_for_dispatch(route.meta("response_type"), result, "response")

    def _dispatch_error(self, error: MCAError) -> NoReturn:
        """Normalize and raise an MCA error for the caller or RPC transport."""
        code = "unknown_operation" if error.code == "unknown_endpoint" else error.code
        field = "operation" if error.code == "unknown_endpoint" else error.field
        raise MCAError(code, error.detail, field, error.status) from error

    def dispatch(
        self,
        operation: str | None = None,
        params: dict[str, Any] | None = None,
        data: Any = None,
        *,
        method: str = "GET",
    ) -> Any:
        """Dispatch an operation and return a typed result or raise ``MCAError``.

        Request parameters and body values are validated from annotations before
        invocation, and the endpoint result is validated against its return
        annotation. Validation and endpoint failures carry stable codes and
        HTTP-compatible statuses in the raised exception.
        """
        try:
            return super().dispatch(
                operation,
                params,
                data,
                method=method,
            )
        except DispatchValidationError as exc:
            raise _validation_error(exc) from exc
        except MCAError:
            raise
        except Exception as exc:
            raise MCAError(
                "internal_error",
                "The endpoint could not complete the operation.",
                "operation",
                500,
            ) from exc

    async def adispatch(
        self,
        operation: str | None = None,
        params: dict[str, Any] | None = None,
        data: Any = None,
        *,
        method: str = "GET",
    ) -> Any:
        """Asynchronously dispatch a typed operation or discovery request."""
        try:
            return await super().adispatch(
                operation,
                params,
                data,
                method=method,
            )
        except DispatchValidationError as exc:
            raise _validation_error(exc) from exc
        except MCAError:
            raise
        except Exception as exc:
            raise MCAError(
                "internal_error",
                "The endpoint could not complete the operation.",
                "operation",
                500,
            ) from exc
