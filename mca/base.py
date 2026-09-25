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
    """Structured error returned when an MCA operation cannot be completed.

    ``code`` is stable for clients, ``detail`` is human-readable, ``field``
    identifies related input when available, and ``status`` is the suggested
    HTTP status for transport adapters.
    """

    def __init__(self, code: str, detail: str, field: str | None = None, status: int = 400):
        """Create an MCA error with a stable code and HTTP-compatible status."""
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.field = field
        self.status = status


class GuideCatalog:
    """Discover and read Markdown guides stored below one directory."""

    def __init__(self, guides_dir: str | Path | None = None):
        """Create a catalog; pass ``None`` to disable guide support."""
        self.root = Path(guides_dir) if guides_dir is not None else None

    @property
    def enabled(self) -> bool:
        """Whether this catalog has a configured guide directory."""
        return self.root is not None

    def available(self) -> list[str]:
        """Return sorted guide paths relative to the catalog root."""
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
        """Read comma-separated guide paths or raise ``unknown_guides``.

        Names are relative paths using ``/`` separators, so nested guides such
        as ``invoices/legacy_format.md`` work without special handling.
        """
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
    """Framework-neutral description of one registered MCA operation."""

    method: str
    path: str | None
    operation: str
    endpoint: Callable[..., Any]
    description: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def relative_route(self) -> str:
        """Return the route in transport form, or the operation name if route-less."""
        if self.path is None:
            return self.operation
        return f"{self.method} {self.path}"

    @property
    def discovery_route(self) -> str:
        """Return the normalized route representation exposed by discovery."""
        if self.path is None:
            return self.operation
        return f"{self.method} {self.path.lstrip('/') or '.'}"

    def meta(self, name: str, default: Any = None) -> Any:
        """Read adapter-specific metadata without exposing the backing mapping."""
        return self.metadata.get(name, default)


class BaseMCARouter:
    """Shared registry, route-resolution, dispatch, guide, and discovery behavior.

    Concrete adapters implement transport registration and operation dispatch;
    all adapters otherwise share the same operation naming and discovery rules.
    """

    def __init__(
        self,
        *,
        guides_dir: str | Path | None = None,
        mca_path: str = "/",
        title: str = "Model Context API",
        version: float = 1.0,
        usage: str | None = None,
    ):
        """Initialize a router and register its adapter-specific discovery endpoint.

        ``guides_dir`` enables Markdown discovery, ``mca_path`` controls the
        discovery route, and ``title``, ``version``, and ``usage`` populate the
        root discovery document.
        """
        self.guide_catalog = GuideCatalog(guides_dir)
        self.mca_path = mca_path
        self.title = title
        self.version = version
        self.usage = usage
        self._routes: dict[str, RegisteredRoute] = {}
        for path, operation_id, endpoint, options in self._discovery_endpoints():
            self._register_endpoint(path, operation_id, endpoint, options)

    def _discovery_endpoints(self) -> Iterable[tuple[str, str, F, Mapping[str, Any]]]:
        """Return transport endpoints that every adapter needs at construction time."""
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
        """Register one operation with the concrete transport adapter."""
        raise NotImplementedError

    def _route_metadata(self, endpoint: F, options: Mapping[str, Any]) -> Mapping[str, Any]:
        """Return adapter-specific metadata stored on a registered route."""
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
        """Create the shared route record and register its transport representation."""
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
        """Validate an endpoint name/path and register the inferred HTTP method."""
        self._validate_route_path(path)
        method = self._method_for_endpoint(endpoint)
        self._validate_route_registration(path, method, operation)
        return self._route(path, method, operation, endpoint, options)

    @staticmethod
    def _validate_route_path(path: str | None) -> None:
        """Reject missing paths; adapters may override this for route-less operations."""
        if path is None:
            raise ValueError("MCA route path must be specified.")

    def _register_route_variant(
        self,
        route: RegisteredRoute,
        endpoint: F,
        options: Mapping[str, Any],
    ) -> None:
        """Register the opposite trailing-slash form without duplicating discovery."""
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
        """Infer an HTTP method from the endpoint's required name prefix."""
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
        """Decorate and register one endpoint.

        The endpoint name determines the HTTP method unless ``operation_id``
        supplies the published operation name. Supported discovery options
        include ``description``, ``guides``, ``include_in_discovery``, and
        ``delegate_to``.

        The decorator returns the original endpoint, allowing it to be used
        normally by application code after registration.
    """
        def decorator(endpoint: F) -> F:
            """Register the decorated endpoint using the enclosing options."""
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
        """Decorate one function as several operations selected by HTTP method.

        ``methods`` accepts ``GET``, ``POST``, ``PUT``, ``PATCH``, and
        ``DELETE``. Each generated operation receives the corresponding
        ``get_``, ``make_``, ``set_``, ``update_``, or ``remove_`` prefix.

        The same callable and registration options are shared by every
        generated operation.
    """
        def decorator(endpoint: F) -> F:
            """Register the decorated endpoint once for each selected method."""
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
        """Reject duplicate operation names and transport method/path pairs."""
        if operation in self._routes:
            raise ValueError(f"MCA operation {operation!r} is already registered.")
        if path is not None and any(
            route.method == method and route.path == path
            for route in self._routes.values()
        ):
            raise ValueError(f"MCA route {method} {path!r} is already registered.")

    def routes(self) -> tuple[RegisteredRoute, ...]:
        """Return registered routes in registration order."""
        return tuple(self._routes.values())

    def route(self, operation: str) -> RegisteredRoute:
        """Return a route by operation name or raise a structured not-found error."""
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
        """Return ``(operation, route)`` pairs for all registered operations."""
        return tuple((route.operation, route.relative_route) for route in self._routes.values())

    def resolve(self, method: str, route_path: str) -> tuple[str, dict[str, str]] | None:
        """Resolve an HTTP method/path to an operation and decoded path values.

        Static routes are checked after routes with fewer path parameters so a
        specific route wins over a parameterized route when both match.
        """
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
        """Dispatch by operation name or resolve an HTTP-style route path first.

        Concrete adapters validate inputs, invoke the endpoint, and format
        errors through ``_dispatch_registered`` and ``_dispatch_error``.

        ``operation`` may be a registered name or an HTTP-style path beginning
        with ``/``. Path parameters resolved from the latter are merged into
        ``params`` before adapter validation.
        """
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

    async def adispatch(
        self,
        operation: str | None = None,
        params: dict[str, Any] | None = None,
        data: Any = None,
        *,
        method: str = "GET",
    ) -> Any:
        """Asynchronously dispatch an operation or HTTP-style route path.

        This mirrors :meth:`dispatch` while allowing concrete adapters to
        await asynchronous endpoints and remote composition clients.
        """
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
            return await self._adispatch_registered(route, params, data)
        except MCAError as exc:
            return self._dispatch_error(exc)

    def _dispatch_registered(
        self,
        route: RegisteredRoute,
        params: dict[str, Any] | None,
        data: Any,
    ) -> Any:
        """Dispatch one resolved route; implemented by concrete adapters."""
        raise NotImplementedError

    async def _adispatch_registered(
        self,
        route: RegisteredRoute,
        params: dict[str, Any] | None,
        data: Any,
    ) -> Any:
        """Dispatch one route asynchronously, awaiting awaitable results."""
        result = self._dispatch_registered(route, params, data)
        if inspect.isawaitable(result):
            return await result
        return result

    def _dispatch_error(self, error: MCAError) -> Any:
        """Convert or re-raise an MCA error according to the adapter contract."""
        raise error

    def _error(self, code: str, detail: str, field: str | None = None, status: int = 400) -> Any:
        """Create an adapter-formatted error response."""
        return self._dispatch_error(MCAError(code, detail, field, status))

    def discovery(
        self,
        guide: str | None,
        operation_name: str | None,
        schema_factory: Callable[[RegisteredRoute], Any],
    ) -> dict[str, Any]:
        """Return root discovery, guide content, or operation schemas.

        ``schema_factory`` is supplied by the adapter because Ninja and
        Pydantic derive their operation schemas from different sources.

        Passing neither query value returns root metadata. Either query value
        returns only the requested guide content and/or operation schemas.
        """
        if guide is None and operation_name is None:
            result = {
                "title": self.title,
                "version": self.version,
                "usage": self.usage if self.usage is not None else self._default_usage(),
                "available_operations": dict(
                    sorted(
                        (
                            route.operation,
                            f"{route.discovery_route} - {route.description}",
                        )
                        for route in self._routes.values()
                        if route.operation != "get_context"
                        and route.meta("include_in_discovery", True)
                    )
                ),
            }
            if self.guide_catalog.enabled:
                available_guides = self.guide_catalog.available()
                if "index.md" in available_guides:
                    result["help"] = self.guide_catalog.read("index.md")["index.md"]
                result["available_guides"] = available_guides
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

    async def adiscovery(
        self,
        guide: str | None,
        operation_name: str | None,
        schema_factory: Callable[[RegisteredRoute], Any],
    ) -> dict[str, Any]:
        """Asynchronously return discovery data using an async schema factory.

        The guide and operation semantics match :meth:`discovery`; operation
        schema factories may return awaitables for composed remote schemas.
        """
        if guide is None and operation_name is None:
            result = {
                "title": self.title,
                "version": self.version,
                "usage": self.usage if self.usage is not None else self._default_usage(),
                "available_operations": dict(
                    sorted(
                        (
                            route.operation,
                            f"{route.discovery_route} - {route.description}",
                        )
                        for route in self._routes.values()
                        if route.operation != "get_context"
                        and route.meta("include_in_discovery", True)
                    )
                ),
            }
            if self.guide_catalog.enabled:
                available_guides = self.guide_catalog.available()
                if "index.md" in available_guides:
                    result["help"] = self.guide_catalog.read("index.md")["index.md"]
                result["available_guides"] = available_guides
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
            operations: dict[str, Any] = {}
            for name in names:
                schema = schema_factory(route_map[name])
                operations[name] = await schema if inspect.isawaitable(schema) else schema
            result["operations"] = operations
        return result

    def _default_usage(self) -> str:
        """Return default client usage instructions based on guide availability."""
        if self.guide_catalog.enabled:
            return (
                "Use GET /?guide={names} and/or GET /?operation={names} with "
                "comma-separated names to read available guides and operation schemas."
            )
        return "Use GET /?operation={names} with comma-separated names to read operation schemas."
