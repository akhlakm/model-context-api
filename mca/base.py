"""Framework-neutral MCA route registration and discovery."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import unquote

F = TypeVar("F", bound=Callable[..., Any])

METHOD_PREFIXES = {
    "get_": "GET",
    "make_": "POST",
    "set_": "PUT",
    "update_": "PATCH",
    "remove_": "DELETE",
}
_DISCOVERY_OPTIONS = {"delegate_to", "guides", "include_in_discovery"}


class MCAError(Exception):
    def __init__(self, code: str, detail: str, field: str | None = None, status: int = 400):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.field = field
        self.status = status


class GuideCatalog:
    def __init__(self, guides_dir: str | Path | None = None):
        self.root = Path(guides_dir) if guides_dir is not None else None

    @property
    def enabled(self) -> bool:
        return self.root is not None

    def available(self) -> list[str]:
        return (
            sorted(
                path.relative_to(self.root).as_posix()
                for path in self.root.rglob("*.md")
                if path.is_file()
            )
            if self.root is not None and self.root.is_dir()
            else []
        )

    def read(self, names: str) -> dict[str, str]:
        requested, available = [item.strip() for item in names.split(",")], set(self.available())
        missing = [item for item in requested if not item or item not in available]
        if missing:
            raise MCAError(
                "unknown_guides",
                f"Unavailable guide(s): {', '.join(missing)}.",
                "guide",
                404,
            )
        assert self.root is not None
        return {name: self.root.joinpath(name).read_text(encoding="utf-8") for name in requested}


@dataclass(frozen=True, slots=True)
class RegisteredRoute:
    method: str
    path: str | None
    operation: str
    endpoint: Callable[..., Any]
    description: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def relative_route(self) -> str:
        if self.path is None:
            return self.operation
        return f"{self.method} {self.path}"

    @property
    def discovery_route(self) -> str:
        if self.path is None:
            return self.operation
        return f"{self.method} {self.path.lstrip('/') or '.'}"

    def meta(self, name: str, default: Any = None) -> Any:
        return self.metadata.get(name, default)


class BaseMCARouter:
    def __init__(
        self,
        *,
        guides_dir: str | Path | None = None,
        mca_path: str = "/",
        title: str = "Model Context API",
        version: float = 1.0,
        help: str | None = None,
    ):
        self.guide_catalog = GuideCatalog(guides_dir)
        self.mca_path = mca_path
        self.title = title
        self.version = version
        self.help = help
        self._routes: dict[str, RegisteredRoute] = {}
        for path, operation_id, endpoint, options in self._discovery_endpoints():
            self._register_endpoint(path, operation_id, endpoint, options)

    def _discovery_endpoints(self) -> Iterable[tuple[str, str, F, Mapping[str, Any]]]:
        raise NotImplementedError

    def _register_transport_route(
        self,
        route: RegisteredRoute,
        endpoint: F,
        options: Mapping[str, Any],
        *,
        path: str | None = None,
        operation_id: str | None = None,
        include_in_schema: bool | None = None,
    ) -> F:
        raise NotImplementedError

    def _route_metadata(self, endpoint: F, options: Mapping[str, Any]) -> Mapping[str, Any]:
        return {}

    @staticmethod
    def _transport_options(options: Mapping[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in options.items() if key not in _DISCOVERY_OPTIONS}

    def _route(
        self,
        path: str | None,
        method: str,
        operation: str,
        endpoint: F,
        options: Mapping[str, Any],
    ) -> F:
        metadata = dict(self._route_metadata(endpoint, options))
        if self.guide_catalog.enabled and options.get("guides") is not None:
            metadata["guides"] = options["guides"]
        if options.get("include_in_discovery") is False:
            metadata["include_in_discovery"] = False
        if options.get("delegate_to") is not None:
            metadata["delegate_to"] = options["delegate_to"]
        docstring = inspect.getdoc(endpoint)
        description = options.get("description") or docstring or operation.replace("_", " ").capitalize()
        route = RegisteredRoute(method, path, operation, endpoint, description, metadata)
        transport_options = self._transport_options(options)
        if options.get("description") or docstring:
            transport_options["description"] = description
        registered_endpoint = self._register_transport_route(route, endpoint, transport_options)
        self._register_route_variant(route, registered_endpoint, transport_options)
        self._routes[operation] = replace(route, endpoint=registered_endpoint)
        return registered_endpoint

    def _register_endpoint(
        self,
        path: str | None,
        operation: str,
        endpoint: F,
        options: Mapping[str, Any],
    ) -> F:
        self._validate_route_path(path)
        method = self._method_for_endpoint(endpoint)
        self._validate_route_registration(path, method, operation)
        return self._route(path, method, operation, endpoint, options)

    @staticmethod
    def _validate_route_path(path: str | None) -> None:
        if path is None:
            raise ValueError("MCA route path must be specified.")

    def _register_route_variant(
        self,
        route: RegisteredRoute,
        endpoint: F,
        options: Mapping[str, Any],
    ) -> None:
        if route.path is None:
            return
        canonical_path = route.path.rstrip("/") or "/"
        if canonical_path == "/":
            return
        variant_path = canonical_path[:-1] if route.path.endswith("/") else f"{canonical_path}/"
        variant_options = dict(options)
        variant_options["include_in_schema"] = False
        self._register_transport_route(
            route,
            endpoint,
            variant_options,
            path=variant_path,
            operation_id=f"{route.operation}__slash_variant",
            include_in_schema=False,
        )

    @staticmethod
    def _method_for_endpoint(endpoint: Callable[..., Any]) -> str:
        method = next(
            (method for prefix, method in METHOD_PREFIXES.items() if endpoint.__name__.startswith(prefix)),
            None,
        )
        if method is None:
            prefixes = ", ".join(f"{prefix[:-1]}_" for prefix in METHOD_PREFIXES.values())
            raise ValueError(
                f"MCA endpoint {endpoint.__name__!r} must start with one of: {prefixes}."
            )
        return method

    def register(self, path: str, *, operation_id: str | None = None, **options: Any) -> Callable[[F], F]:
        def decorator(endpoint: F) -> F:
            return self._register_endpoint(path, operation_id or endpoint.__name__, endpoint, options)
        return decorator

    def register_all(
        self,
        path: str,
        *,
        operation_id: str | None = None,
        methods: Iterable[str] | None = None,
        **options: Any,
    ) -> Callable[[F], F]:
        def decorator(endpoint: F) -> F:
            self._validate_route_path(path)
            base_operation = operation_id or endpoint.__name__
            selected_methods = tuple(
                method.upper()
                for method in (methods if methods is not None else METHOD_PREFIXES.values())
            )
            method_prefixes = {method: prefix for prefix, method in METHOD_PREFIXES.items()}
            unknown_methods = [method for method in selected_methods if method not in method_prefixes]
            if unknown_methods:
                supported_methods = ", ".join(method_prefixes)
                raise ValueError(f"MCA methods must be selected from: {supported_methods}.")
            if len(set(selected_methods)) != len(selected_methods):
                raise ValueError("MCA methods cannot contain duplicates.")
            if not selected_methods:
                raise ValueError("MCA register_all requires at least one method.")

            registrations = [
                (method, f"{method_prefixes[method]}{base_operation}")
                for method in selected_methods
            ]
            for method, operation in registrations:
                self._validate_route_registration(path, method, operation)

            for method, operation in registrations:
                self._route(path, method, operation, endpoint, options)
            return endpoint

        return decorator

    def _validate_route_registration(self, path: str | None, method: str, operation: str) -> None:
        if operation in self._routes:
            raise ValueError(f"MCA operation {operation!r} is already registered.")
        if path is not None and any(
            route.method == method and route.path == path
            for route in self._routes.values()
        ):
            raise ValueError(f"MCA route {method} {path!r} is already registered.")

    def routes(self) -> tuple[RegisteredRoute, ...]:
        return tuple(self._routes.values())

    def route(self, operation: str) -> RegisteredRoute:
        route = self._routes.get(operation)
        if route is None:
            raise MCAError(
                "unknown_endpoint",
                f"No endpoint is registered for operation '{operation}'.",
                "endpoint",
                404,
            )
        return route

    def public_routes(self) -> tuple[tuple[str, str], ...]:
        return tuple((route.operation, route.relative_route) for route in self._routes.values())

    def resolve(self, method: str, route_path: str) -> tuple[str, dict[str, str]] | None:
        """Resolve an HTTP method and route path to an operation."""
        parts = lambda value: (value.rstrip("/") or "/").strip("/").split("/") if value.strip("/") else []
        path_parts = parts(route_path)
        routes = sorted(
            (route for route in self._routes.values() if route.path is not None),
            key=lambda route: route.path.count("{"),
        )

        for route in routes:
            if route.method != method.upper():
                continue
            route_parts = parts(route.path)
            if len(route_parts) != len(path_parts):
                continue

            path_params: dict[str, str] = {}
            for route_part, path_part in zip(route_parts, path_parts):
                if route_part.startswith("{") and route_part.endswith("}"):
                    name = route_part[1:-1].split(":")[-1]
                    path_params[name] = unquote(path_part)
                elif route_part != path_part:
                    break
            else:
                return route.operation, path_params

        return None

    def dispatch(
        self,
        operation: str | None = None,
        params: dict[str, Any] | None = None,
        data: Any = None,
        *,
        method: str = "GET",
    ) -> Any:
        if operation is None:
            return self._error("invalid_request", "An operation or route path is required.", "operation")

        if operation.startswith("/"):
            route_path = operation
            resolved = self.resolve(method, route_path)
            if resolved is None:
                return self._error(
                    "unknown_route",
                    f"No route matches {method.upper()} {route_path}.",
                    "path",
                    404,
                )
            operation, path_params = resolved
            params = {**(params or {}), **path_params}

        try:
            route = self.route(operation)
            return self._dispatch_registered(route, params, data)
        except MCAError as exc:
            return self._dispatch_error(exc)

    def _dispatch_registered(
        self,
        route: RegisteredRoute,
        params: dict[str, Any] | None,
        data: Any,
    ) -> Any:
        raise NotImplementedError

    def _dispatch_error(self, error: MCAError) -> Any:
        raise error

    def _error(self, code: str, detail: str, field: str | None = None, status: int = 400) -> Any:
        return self._dispatch_error(MCAError(code, detail, field, status))

    def discovery(
        self,
        guide: str | None,
        operation_name: str | None,
        schema_factory: Callable[[RegisteredRoute], Any],
    ) -> dict[str, Any]:
        if guide is None and operation_name is None:
            result = {
                "title": self.title,
                "version": self.version,
                "help": self.help if self.help is not None else self._default_help(),
                "available_operations": dict(
                    sorted(
                        (
                            route.operation,
                            f"{route.discovery_route} - {route.description}",
                        )
                        for route in self._routes.values()
                        if route.operation != "get_mca"
                        and route.meta("include_in_discovery", True)
                    )
                ),
            }
            if self.guide_catalog.enabled:
                result["index"] = self.guide_catalog.read("index.md")["index.md"]
                result["available_guides"] = self.guide_catalog.available()
            return result

        result: dict[str, Any] = {}
        if guide is not None:
            result["guides"] = self.guide_catalog.read(guide)
        if operation_name is not None:
            names = [item.strip() for item in operation_name.split(",")]
            route_map = dict(self._routes)
            missing = [name for name in names if not name or name not in route_map]
            if missing:
                raise MCAError(
                    "unknown_operation",
                    f"Unavailable operation(s): {', '.join(missing)}.",
                    "operation",
                    404,
                )
            result["operations"] = {
                name: schema_factory(route_map[name])
                for name in names
            }
        return result

    def _default_help(self) -> str:
        if self.guide_catalog.enabled:
            return (
                "Use GET /?guide={names} and/or GET /?operation={names} with "
                "comma-separated names to read available guides and operation schemas."
            )
        return "Use GET /?operation={names} with comma-separated names to read operation schemas."
