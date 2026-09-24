"""Reusable Model Context API support for Django Ninja APIs."""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Iterable, Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, TypeVar
from urllib.parse import quote, urlencode

from django.http import HttpRequest
from django.http.response import HttpResponseBase
from ninja import NinjaAPI, Query, Router, Status

from .base import BaseMCARouter, MCAError, RegisteredRoute
from .composition import MCACompositionMixin
from .models import ErrorOut, MCADiscoveryOut, MCAResponseOut
from .schema import attach_components, build_request_schema

F = TypeVar("F", bound=Callable[..., Any])

_PATH_PARAMETER = re.compile(r"\{([^}]+)\}")


def _request_path(path_template: str, path_params: Mapping[str, Any]) -> str:
    return _PATH_PARAMETER.sub(
        lambda match: quote(
            str(path_params.get(match.group(1), match.group(0))),
            safe="",
        ),
        path_template,
    )


class MCAExecutionError(Exception):
    def __init__(self, operation: str, detail: str, response: HttpResponseBase | None = None):
        super().__init__(detail)
        self.operation = operation
        self.detail = detail
        self.response = response


class NinjaMCARouter(MCACompositionMixin, BaseMCARouter):
    def __init__(
        self,
        api: Any,
        *,
        guides_dir: str | Path | None = None,
        mca_path: str = "/",
        title: str = "Model Context API",
        version: float = 1.0,
        help: str | None = None,
        error_responses: Mapping[int, Any] | None = None,
    ):
        self.api = api
        self._bound_api: NinjaAPI | None = None
        self.error_responses = error_responses or {}
        super().__init__(
            guides_dir=guides_dir,
            mca_path=mca_path,
            title=title,
            version=version,
            help=help,
        )

    def _discovery_endpoints(self) -> Iterable[tuple[str, str, F, Mapping[str, Any]]]:
        response = {
            400: ErrorOut,
            404: ErrorOut,
            502: ErrorOut,
            **self.error_responses,
            200: MCAResponseOut | MCADiscoveryOut,
        }

        def get_mca(
            request: HttpRequest,
            guide: str | None = Query(None, description="Comma-separated names of Markdown guides to read."),
            operation_name: str | None = Query(
                None,
                alias="operation",
                description="Comma-separated API operation names whose schemas should be read.",
            ),
        ):
            try:
                return self._get_mca(guide, operation_name)
            except MCAError as exc:
                return Status(
                    exc.status,
                    ErrorOut(code=exc.code, detail=exc.detail, field=exc.field),
                )

        return (
            (
                self.mca_path,
                "get_mca",
                get_mca,
                {"response": response, "description": "Discover API guides and read route schemas."},
            ),
        )

    def _register_transport_route(
        self,
        route: RegisteredRoute,
        endpoint: F,
        options: Mapping[str, Any],
        **variant: Any,
    ) -> F:
        api_register = getattr(self.api, route.method.lower())
        route_options = dict(options)
        route_options.update({key: variant[key] for key in ("include_in_schema",) if key in variant})
        return api_register(
            variant.get("path", route.path),
            operation_id=variant.get("operation_id", route.operation),
            **route_options,
        )(endpoint)

    def execute_http(
        self,
        operation: str,
        request: HttpRequest,
        path_params: Mapping[str, Any] | None = None,
        *,
        allow_anonymous: bool = False,
    ) -> HttpResponseBase:
        route = self.route(operation)
        ninja_operation = self._ninja_operation(route.operation)
        if inspect.iscoroutinefunction(ninja_operation.view_func):
            raise MCAExecutionError(
                operation,
                "Asynchronous endpoints require an asynchronous executor.",
            )

        if allow_anonymous:
            request._mca_allow_anonymous = True
            request._dont_enforce_csrf_checks = True

        return ninja_operation.run(request, **dict(path_params or {}))

    def execute_http_request(
        self,
        operation: str,
        source_request: HttpRequest | None = None,
        path_params: Mapping[str, Any] | None = None,
        query_params: Mapping[str, Any] | None = None,
        body: Any = None,
        *,
        allow_anonymous: bool = False,
    ) -> HttpResponseBase:
        route = self.route(operation)
        path_values = dict(path_params or {})
        request = self._build_execution_request(
            route,
            source_request,
            path_values,
            query_params,
            body,
        )
        return self.execute_http(
            operation,
            request,
            path_values,
            allow_anonymous=allow_anonymous,
        )

    @staticmethod
    def _copy_request_context(
        request: HttpRequest,
        source_request: HttpRequest,
    ) -> None:
        request.user = source_request.user
        request.COOKIES = source_request.COOKIES.copy()
        if hasattr(source_request, "session"):
            request.session = source_request.session
        if getattr(source_request, "_dont_enforce_csrf_checks", False):
            request._dont_enforce_csrf_checks = True
        request.META.update(
            {
                key: value
                for key, value in source_request.META.items()
                if key not in {
                    "CONTENT_LENGTH",
                    "CONTENT_TYPE",
                    "PATH_INFO",
                    "QUERY_STRING",
                    "RAW_URI",
                    "REQUEST_URI",
                }
            }
        )

    def _build_execution_request(
        self,
        route: RegisteredRoute,
        source_request: HttpRequest | None,
        path_values: Mapping[str, Any],
        query_params: Mapping[str, Any] | None,
        body: Any,
    ) -> HttpRequest:
        from django.contrib.auth.models import AnonymousUser
        from django.test import RequestFactory

        query_string = urlencode(dict(query_params or {}), doseq=True)
        path = _request_path(route.path, path_values)
        if query_string:
            path = f"{path}?{query_string}"

        request_factory = RequestFactory()
        if body is None:
            request = request_factory.generic(route.method, path)
        else:
            request = request_factory.generic(
                route.method,
                path,
                data=json.dumps(body).encode("utf-8"),
                content_type="application/json",
            )

        if source_request is None:
            request.user = AnonymousUser()
        else:
            self._copy_request_context(request, source_request)
        return request

    def _ninja_operation(self, operation: str) -> Any:
        api = self._operation_api()
        for bound_router in api._get_bound_routers():
            for path_view in bound_router.path_operations.values():
                for ninja_operation in path_view.operations:
                    if ninja_operation.operation_id == operation:
                        return ninja_operation
        raise MCAExecutionError(
            operation,
            f"Ninja has no bound operation for '{operation}'.",
        )

    def _get_mca(self, guide: str | None, operation_name: str | None):
        return self._composed_discovery(guide, operation_name, self._route_schema)

    def _operation_api(self) -> Any:
        if not isinstance(self.api, Router):
            return self.api
        if self._bound_api is None:
            self._bound_api = NinjaAPI(default_router=self.api)
        return self._bound_api

    def _openapi_schema(self) -> dict[str, Any]:
        api = self._operation_api()
        if isinstance(self.api, Router):
            return api.get_openapi_schema(path_prefix="")
        return api.get_openapi_schema()

    @staticmethod
    def _find_openapi_operation(
        schema: Mapping[str, Any],
        name: str,
    ) -> dict[str, Any] | None:
        for path_data in schema.get("paths", {}).values():
            for operation in path_data.values():
                if isinstance(operation, dict) and operation.get("operationId") == name:
                    return operation
        return None

    def _openapi_operation(self, name: str) -> dict[str, Any] | None:
        return self._find_openapi_operation(self._openapi_schema(), name)

    @staticmethod
    def _openapi_parameter_sections(
        operation: Mapping[str, Any],
        route_path: str,
    ) -> tuple[dict[str, dict[str, Any]], set[str]]:
        path_properties: dict[str, Any] = {}
        path_required: list[str] = []
        query_properties: dict[str, Any] = {}
        query_required: list[str] = []
        template_parameters = re.findall(r"\{([^}]+)\}", route_path)
        openapi_path_parameters = [
            parameter["name"]
            for parameter in operation.get("parameters", [])
            if parameter.get("in") == "path"
        ]
        path_parameter_names = dict(zip(openapi_path_parameters, template_parameters))
        for parameter in operation.get("parameters", []):
            parameter_name = path_parameter_names.get(parameter["name"], parameter["name"])
            parameter_schema = deepcopy(parameter.get("schema", {}))
            parameter_schema["description"] = parameter.get("description") or (
                f"{parameter_name} request parameter."
            )
            if parameter.get("in") == "path":
                properties, required = path_properties, path_required
            else:
                properties, required = query_properties, query_required
            properties[parameter_name] = parameter_schema
            if parameter.get("required"):
                required.append(parameter_name)

        sections: dict[str, dict[str, Any]] = {}
        if path_properties:
            sections["path_params"] = {
                "description": "Values captured from the operation route.",
                "properties": path_properties,
                "required": path_required,
            }
        if query_properties:
            sections["query_params"] = {
                "description": "Values supplied as operation query parameters.",
                "properties": query_properties,
                "required": query_required,
            }
        required_sections = {
            name
            for name, section in sections.items()
            if section["required"]
        }
        return sections, required_sections

    def _openapi_request_schema(
        self,
        operation: Mapping[str, Any],
        route_path: str,
        components: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        sections, required_sections = self._openapi_parameter_sections(operation, route_path)
        body_schema = (
            operation.get("requestBody", {})
            .get("content", {})
            .get("application/json", {})
            .get("schema")
        )
        request_schema = build_request_schema(
            sections,
            required_sections,
            body_schema=body_schema,
            body_required=bool(operation.get("requestBody", {}).get("required")),
        )
        if request_schema is None:
            return None
        request_components = self._referenced_components(request_schema, components)
        return attach_components(request_schema, request_components)

    def _openapi_response_schema(
        self,
        operation: Mapping[str, Any],
        components: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        response_schema = None
        for status in (200, 201):
            response_schema = (
                operation.get("responses", {})
                .get(status, {})
                .get("content", {})
                .get("application/json", {})
                .get("schema")
            )
            if response_schema is not None:
                break
        if response_schema is None:
            return None
        response_schema = deepcopy(response_schema)
        response_components = self._referenced_components(response_schema, components)
        return attach_components(response_schema, response_components)

    def _route_schema(self, route: RegisteredRoute) -> dict[str, Any]:
        name = route.operation
        discovery_route = route.discovery_route
        openapi_schema = self._openapi_schema()
        operation = self._find_openapi_operation(openapi_schema, name)
        if operation is None:
            raise MCAError(
                "unknown_operation",
                f"No schema is available for operation '{name}'.",
                "operation",
                404,
            )

        components = openapi_schema.get("components", {}).get("schemas", {})
        request_schema = self._openapi_request_schema(operation, route.path, components)
        response_schema = self._openapi_response_schema(operation, components)

        schema = {
            "route": discovery_route,
            "description": operation.get("description") or operation.get("summary") or route.description,
            "request_schema": request_schema,
            "response_schema": response_schema,
        }
        guides = self._route_guides(route)
        if guides:
            schema["guides"] = guides
        return self._compose_route_schema(route, schema)

    @staticmethod
    def _referenced_components(
        schema: Any,
        components: Mapping[str, Any],
    ) -> dict[str, Any]:
        pending = list(NinjaMCARouter._component_references(schema))
        selected: dict[str, Any] = {}
        while pending:
            name = pending.pop()
            if name in selected or name not in components:
                continue
            component = deepcopy(components[name])
            selected[name] = component
            pending.extend(NinjaMCARouter._component_references(component))
        return selected

    @staticmethod
    def _component_references(value: Any):
        if isinstance(value, dict):
            reference = value.get("$ref")
            prefix = "#/components/schemas/"
            if isinstance(reference, str) and reference.startswith(prefix):
                yield reference.removeprefix(prefix)
            for child in value.values():
                yield from NinjaMCARouter._component_references(child)
        elif isinstance(value, list):
            for child in value:
                yield from NinjaMCARouter._component_references(child)
