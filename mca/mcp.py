"""MCP hosting for applications that publish MCA Ninja routers."""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

from django.http import HttpRequest
from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context as MCPContext
from mcp.server.mcpserver.exceptions import ToolError

from .base import MCAError
from .ninja import MCAExecutionError, NinjaMCARouter
from .request import build_request

BODY_METHODS = {"POST", "PUT", "PATCH"}
RequestContextFactory = Callable[
    [str, str, str, Mapping[str, Any], Mapping[str, Any], Any, Mapping[str, str]],
    HttpRequest | None | Awaitable[HttpRequest | None],
]


@dataclass(frozen=True)
class MCPRoute:
    """Mounted MCP application and its public Django URL."""

    app_label: str
    path: str
    server: MCPServer
    application: Any


class MCPHost:
    """ASGI host that exposes each Django app's Ninja MCA as an MCP tool.

    The host discovers application registries lazily after Django initializes,
    then forwards MCA-relative tool routes to the corresponding Streamable HTTP
    MCP application and all other traffic to Django.
    """

    def __init__(self, request_context_factory: RequestContextFactory | None = None):
        """Create an uninitialized host; Django and app routes load on first use.

        ``request_context_factory`` may return a request carrying the user,
        credentials, session, or headers that application authentication needs
        for one MCP call. Its final argument contains the incoming MCP
        transport headers. Without it, MCP transport headers are copied into a
        synthetic Django request automatically.
        """
        self._django_application: Any | None = None
        self._routes: tuple[MCPRoute, ...] = ()
        self.request_context_factory = request_context_factory

    @staticmethod
    def _json_response(response: Any) -> Any:
        """Decode a successful Django response, treating empty/204 as ``None``."""
        if response.status_code == 204 or not response.content:
            return None
        return json.loads(response.content)

    @staticmethod
    def _tool_error(response: Any) -> ToolError:
        """Convert an HTTP error response into an MCP ``ToolError`` payload."""
        try:
            payload = json.loads(response.content)
        except (TypeError, ValueError, UnicodeDecodeError):
            payload = {
                "code": "mcp_endpoint_error",
                "detail": response.reason_phrase,
            }
        if isinstance(payload, dict):
            payload = {"status": response.status_code, **payload}
        return ToolError(json.dumps(payload, ensure_ascii=False))

    @staticmethod
    def _request_headers(context: MCPContext | None) -> Mapping[str, str]:
        """Return headers from the incoming MCP transport request, if present."""
        if context is None:
            return {}
        try:
            request = context.request_context.request
        except ValueError:
            return {}
        if request is None:
            return {}
        return request.headers

    @staticmethod
    def _parse_route(route: str) -> tuple[str, str, dict[str, Any]]:
        """Parse an MCP route argument into method, path, and query values."""
        if not isinstance(route, str) or not route.strip():
            raise MCAError("invalid_route", "Route must be a non-empty HTTP method and path.", "route")

        value = route.strip()
        parts = value.split(None, 1)
        if len(parts) != 2:
            raise MCAError("invalid_route", "Route must use the form 'METHOD path'.", "route")

        method, target = parts[0].upper(), parts[1]
        if not method.isalpha() or any(character.isspace() for character in target):
            raise MCAError("invalid_route", "Route must use the form 'METHOD path'.", "route")

        try:
            parsed = urlsplit(target)
        except ValueError as exc:
            raise MCAError("invalid_route", "Route must be a valid HTTP method and path.", "route") from exc
        if parsed.scheme or parsed.netloc or parsed.fragment or not parsed.path:
            raise MCAError(
                "invalid_route",
                "Route must be an API-relative path without a scheme, host, or fragment.",
                "route",
            )

        path = parsed.path if parsed.path.startswith("/") else f"/{parsed.path}"
        values = parse_qs(parsed.query, keep_blank_values=True)
        query_params = {
            key: values[0] if len(values) == 1 else values
            for key, values in values.items()
        }
        return method, path, query_params

    async def _call_operation(
        self,
        registry: NinjaMCARouter,
        app_label: str,
        operation: str,
        method: str,
        path: str,
        path_params: dict[str, Any],
        query_params: dict[str, Any],
        body: Any,
        request_headers: Mapping[str, str] | None = None,
    ) -> str:
        """Invoke a resolved Ninja operation and serialize its JSON response."""
        source_request = None
        if self.request_context_factory is not None:
            source_request = self.request_context_factory(
                app_label,
                method,
                path,
                path_params,
                query_params,
                body,
                request_headers or {},
            )
            if inspect.isawaitable(source_request):
                source_request = await source_request
        elif request_headers:
            source_request = build_request(
                method,
                path,
                headers=request_headers,
            )

        response = await registry.execute_http_request_async(
            operation,
            source_request=source_request,
            path_params=path_params,
            query_params=query_params,
            body=body,
        )
        if response.status_code >= 400:
            raise self._tool_error(response)
        return json.dumps(self._json_response(response), ensure_ascii=False)

    async def _call_route(
        self,
        registry: NinjaMCARouter,
        app_label: str,
        route: str,
        body: Any,
        api_base_path: str,
        request_headers: Mapping[str, str] | None = None,
    ) -> str:
        """Validate and resolve an HTTP-style MCP route before execution."""
        method, path, query_params = self._parse_route(route)
        if path == api_base_path or path.startswith(f"{api_base_path}/"):
            raise MCAError(
                "invalid_route",
                f"Route must be relative to {api_base_path}; omit the API prefix.",
                "route",
            )
        if body is not None and method not in BODY_METHODS:
            raise MCAError(
                "invalid_body",
                f"HTTP {method} routes cannot receive a JSON body.",
                "body",
            )

        resolved = registry.resolve(method, path)
        if resolved is None:
            raise MCAError(
                "unknown_route",
                f"No route matches {method} {path}.",
                "route",
                404,
            )

        operation, path_params = resolved
        return await self._call_operation(
            registry,
            app_label,
            operation,
            method,
            path,
            path_params,
            query_params,
            body,
            request_headers,
        )

    def build_server(self, registry: NinjaMCARouter, app_label: str) -> MCPServer:
        """Build the MCP server and tool for one Django app's MCA registry.

        The resulting tool accepts an HTTP-style route relative to
        ``/api/{app_label}``; ``GET /`` is the discovery entry point.
        """
        server = MCPServer(f"{app_label} API")
        tool_name = f"{app_label}_api"
        rest_base_path = f"/api/{app_label}"

        @server.tool(
            name=tool_name,
            description=(
                f"Call the {app_label} API with an HTTP-style route relative to its API mount. "
                "Start with route='GET /' to discover operations, guides and schemas. "
                "Pass body for JSON request data. Results are JSON text; "
                "HTTP 204 responses return null. Direct REST access is also possible."
            ),
            structured_output=False,
        )
        async def call_api(
            route: str,
            body: Any = None,
            context: MCPContext | None = None,
        ) -> str:
            """Handle one MCP tool call using an MCA-relative HTTP route."""
            try:
                return await self._call_route(
                    registry,
                    app_label,
                    route,
                    body,
                    rest_base_path,
                    self._request_headers(context),
                )
            except MCAError as exc:
                raise ToolError(
                    json.dumps(
                        {
                            "code": exc.code,
                            "detail": exc.detail,
                            "field": exc.field,
                            "status": exc.status,
                        },
                        ensure_ascii=False,
                    )
                ) from exc
            except MCAExecutionError as exc:
                raise ToolError(
                    json.dumps(
                        {
                            "code": "execution_error",
                            "detail": exc.detail,
                            "field": "operation",
                            "status": 500,
                        },
                        ensure_ascii=False,
                    )
                ) from exc

        call_api.__name__ = tool_name
        return server

    def discover_routes(self) -> tuple[MCPRoute, ...]:
        """Discover Django apps that export a ``mca_registry`` Ninja router.

        Each discovered app receives a unique ``/api/{label}/mcp`` path and
        ``{label}_api`` tool name. Duplicate paths or names are rejected.
        """
        from importlib import import_module

        from django.apps import apps

        routes: list[MCPRoute] = []
        paths: set[str] = set()
        tool_names: set[str] = set()
        for app_config in apps.get_app_configs():
            module_name = f"{app_config.name}.api"
            try:
                module = import_module(module_name)
            except ModuleNotFoundError as exc:
                if exc.name == module_name:
                    continue
                raise

            registry = getattr(module, "mca_registry", None)
            if registry is None:
                continue
            if not isinstance(registry, NinjaMCARouter):
                raise RuntimeError(
                    f"MCA application '{app_config.label}' must expose a NinjaMCARouter "
                    "named 'mca_registry' from its api module."
                )

            app_label = app_config.label
            path = f"/api/{app_label}/mcp"
            tool_name = f"{app_label}_api"
            if path in paths:
                raise RuntimeError(f"MCA MCP path '{path}' is registered more than once.")
            if tool_name in tool_names:
                raise RuntimeError(f"MCA MCP tool '{tool_name}' is registered more than once.")

            server = self.build_server(registry, app_label)
            application = server.streamable_http_app(
                streamable_http_path="/",
                stateless_http=True,
            )
            routes.append(MCPRoute(app_label, path, server, application))
            paths.add(path)
            tool_names.add(tool_name)

        return tuple(routes)

    async def _dispatch(self, scope: dict[str, Any], receive: Any, send: Any):
        """Route MCP paths to MCP applications and delegate everything else to Django."""
        self._initialize()
        if scope.get("type") == "http":
            path = scope.get("path", "")
            normalized_path = path.rstrip("/") or "/"
            for route in self._routes:
                if normalized_path != route.path:
                    continue

                mcp_scope = dict(scope)
                mcp_scope["path"] = "/"
                mcp_scope["raw_path"] = b"/"
                mcp_scope["root_path"] = ""
                return await route.application(mcp_scope, receive, send)

        return await self._django_application(scope, receive, send)

    def _initialize(self) -> None:
        """Initialize Django and discover MCP routes exactly once."""
        if self._django_application is not None:
            return

        from django.core.asgi import get_asgi_application

        self._django_application = get_asgi_application()
        self._routes = self.discover_routes()

    @asynccontextmanager
    async def _mcp_lifespan(self):
        """Run every discovered MCP session manager during ASGI lifespan."""
        self._initialize()
        async with AsyncExitStack() as stack:
            for route in self._routes:
                await stack.enter_async_context(route.server.session_manager.run())
            yield

    async def _handle_lifespan(self, receive: Any, send: Any):
        """Translate ASGI lifespan messages into startup/shutdown completion events."""
        startup_complete = False
        try:
            message = await receive()
            if message.get("type") != "lifespan.startup":
                raise RuntimeError("Expected lifespan.startup message.")

            async with self._mcp_lifespan():
                await send({"type": "lifespan.startup.complete"})
                startup_complete = True
                message = await receive()
                if message.get("type") != "lifespan.shutdown":
                    raise RuntimeError("Expected lifespan.shutdown message.")

            await send({"type": "lifespan.shutdown.complete"})
        except Exception as exc:
            event_type = (
                "lifespan.shutdown.failed"
                if startup_complete
                else "lifespan.startup.failed"
            )
            await send({"type": event_type, "message": str(exc)})

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any):
        """Serve HTTP and lifespan ASGI scopes through the composed host."""
        if scope.get("type") == "lifespan":
            return await self._handle_lifespan(receive, send)
        return await self._dispatch(scope, receive, send)
