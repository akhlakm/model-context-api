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
from pydantic import ValidationError

from .base import BaseMCARouter, MCAError, RegisteredRoute
from .composition import MCAClient
from .models import (APIRouteSchemaOut, ErrorOut, MCADiscoveryOut,
                     MCAResponseOut)

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


class NinjaMCARouter(BaseMCARouter):
    _namespace_pattern = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")

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
        self._mounted_mcas: dict[str, MCAClient] = {}
        super().__init__(
            guides_dir=guides_dir,
            mca_path=mca_path,
            title=title,
            version=version,
            help=help,
        )

    def mount(self, namespace: str, client: MCAClient) -> None:
        """Make a private MCA available for explicit delegated routes."""
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

    def _delegate_target(self, target: Any) -> tuple[str, str]:
        if not isinstance(target, str):
            raise ValueError("delegate_to must use the form 'namespace.operation'.")
        namespace, separator, operation = target.partition(".")
        if not separator or not namespace or not operation:
            raise ValueError("delegate_to must use the form 'namespace.operation'.")
        if namespace not in self._mounted_mcas:
            raise ValueError(f"MCA namespace {namespace!r} is not mounted.")
        return namespace, operation

    def _route_metadata(self, endpoint: F, options: Mapping[str, Any]) -> Mapping[str, Any]:
        target = options.get("delegate_to")
        if target is not None:
            self._delegate_target(target)
        return {}

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

    def _register_transport_route(self, route: RegisteredRoute, endpoint: F, options: Mapping[str, Any], **variant: Any) -> F:
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
        from django.contrib.auth.models import AnonymousUser
        from django.test import RequestFactory

        route = self.route(operation)
        path_values = dict(path_params or {})
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

        return self.execute_http(
            operation,
            request,
            path_values,
            allow_anonymous=allow_anonymous,
        )

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
        if guide is None and operation_name is None:
            result = self.discovery(None, None, self._route_schema)
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
                    self._route_schema,
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

    def _delegated_routes(self) -> tuple[RegisteredRoute, ...]:
        return tuple(
            route
            for route in self._routes.values()
            if route.operation != "get_mca"
            and route.meta("include_in_discovery", True)
            and route.meta("delegate_to") is not None
        )

    def _remote_operation_schema(
        self,
        namespace: str,
        operation: str,
    ) -> APIRouteSchemaOut:
        result = self._remote_discovery(namespace, operation=operation)
        operations = result.get("operations") or {}
        if operation not in operations:
            raise MCAError(
                "unknown_operation",
                f"Mounted MCA service '{namespace}' has no operation '{operation}'.",
                "operation",
                404,
            )
        try:
            return APIRouteSchemaOut.model_validate(operations[operation])
        except ValidationError as exc:
            raise MCAError(
                "invalid_upstream_response",
                f"Mounted MCA service '{namespace}' returned an invalid operation schema.",
                "service",
                502,
            ) from exc

    def _exposed_remote_guides(self) -> set[str]:
        guides: set[str] = set()
        for route in self._delegated_routes():
            namespace, operation = self._delegate_target(route.meta("delegate_to"))
            remote_schema = self._remote_operation_schema(namespace, operation)
            guides.update(
                f"{namespace}/{name}"
                for name in remote_schema.guides or []
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
            return MCADiscoveryOut.model_validate(value).model_dump()
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

    def _openapi_operation(self, name: str) -> dict[str, Any] | None:
        schema = self._openapi_schema()
        for path_data in schema.get("paths", {}).values():
            for operation in path_data.values():
                if isinstance(operation, dict) and operation.get("operationId") == name:
                    return operation
        return None

    def _route_schema(self, route: RegisteredRoute) -> dict[str, Any]:
        name = route.operation
        discovery_route = route.discovery_route
        operation = self._openapi_operation(name)
        if operation is None:
            raise MCAError(
                "unknown_operation",
                f"No schema is available for operation '{name}'.",
                "operation",
                404,
            )

        openapi_schema = self._openapi_schema()
        components = openapi_schema.get("components", {}).get("schemas", {})
        path_properties: dict[str, Any] = {}
        path_required: list[str] = []
        query_properties: dict[str, Any] = {}
        query_required: list[str] = []
        route_path = route.path
        template_path_parameters = re.findall(r"\{([^}]+)\}", route_path)
        openapi_path_parameters = [
            parameter["name"]
            for parameter in operation.get("parameters", [])
            if parameter.get("in") == "path"
        ]
        path_parameter_names = dict(zip(openapi_path_parameters, template_path_parameters))
        for parameter in operation.get("parameters", []):
            parameter_name = path_parameter_names.get(parameter["name"], parameter["name"])
            parameter_schema = deepcopy(parameter.get("schema", {}))
            parameter_schema["description"] = parameter.get("description") or f"{parameter_name} request parameter."
            if parameter.get("in") == "path":
                properties = path_properties
                required = path_required
            else:
                properties = query_properties
                required = query_required
            properties[parameter_name] = parameter_schema
            if parameter.get("required"):
                required.append(parameter_name)

        body_schema = (
            operation.get("requestBody", {})
            .get("content", {})
            .get("application/json", {})
            .get("schema")
        )
        request_properties: dict[str, Any] = {}
        request_required: list[str] = []
        if path_properties:
            request_properties["path_params"] = {
                "description": "Values captured from the operation route.",
                "type": "object",
                "properties": path_properties,
                "required": path_required,
            }
            if path_required:
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
        if body_schema is not None:
            request_properties["body"] = {
                "description": "JSON request body.",
                **deepcopy(body_schema),
            }
            if operation.get("requestBody", {}).get("required"):
                request_required.append("body")

        request_schema = None
        if request_properties:
            request_shape = {
                "type": "object",
                "properties": request_properties,
                "required": request_required,
            }
            request_schema = {**request_shape}
            request_components = self._referenced_components(request_shape, components)
            if request_components:
                request_schema["components"] = {"schemas": request_components}

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
        if response_schema is not None:
            response_schema = deepcopy(response_schema)
            response_components = self._referenced_components(response_schema, components)
            if response_components:
                response_schema["components"] = {"schemas": response_components}

        schema = {
            "route": discovery_route,
            "description": operation.get("description") or operation.get("summary") or route.description,
            "request_schema": request_schema,
            "response_schema": response_schema,
        }
        guides = list(route.meta("guides") or [])
        target = route.meta("delegate_to")
        if target is not None:
            namespace, remote_operation = self._delegate_target(target)
            remote_schema = self._remote_operation_schema(namespace, remote_operation)
            guides.extend(
                f"{namespace}/{name}"
                for name in remote_schema.guides or []
            )
        if guides:
            schema["guides"] = list(dict.fromkeys(guides))
        return schema

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
