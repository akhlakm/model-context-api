"""MCP hosting for applications that publish MCA Ninja routers."""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
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
MCP_TOOL_NAME = "mc_api"
RequestContextFactory = Callable[
    [str, str, Mapping[str, Any], Mapping[str, Any], Any, Mapping[str, str]],
    HttpRequest | None | Awaitable[HttpRequest | None],
]
MCPAuthCallback = Callable[[HttpRequest], Any]


@dataclass(frozen=True)
class MCPRegistration:
    """One MCA router registered with the shared MCP API tool."""

    api_base_path: str
    description: str
    registry: NinjaMCARouter
    auth: MCPAuthCallback | None = None


class MCPHost:
    """ASGI host exposing registered MCA routers through one MCP API tool.

    The host forwards full API paths from the shared ``mc_api`` tool to the
    matching registered router and all other traffic to Django.
    """

    def __init__(
        self,
        request_context_factory: RequestContextFactory | None = None,
    ):
        """Create an uninitialized host with one MCP endpoint.

        ``request_context_factory`` may return a request carrying the user,
        credentials, session, or headers that application authentication needs
        for one MCP call. Its final argument contains the incoming MCP
        transport headers. Without it, MCP transport headers are copied into a
        synthetic Django request automatically.
        """
        self._django_application: Any | None = None
        self._mcp_server: MCPServer | None = None
        self._mcp_application: Any | None = None
        self._registrations: tuple[MCPRegistration, ...] = ()
        self.request_context_factory = request_context_factory
        self._mcp_auth: MCPAuthCallback | None = None
        self._mcp_path = self._normalize_path("/mcp")
        self._description_prefix = ""

    def configure(
        self,
        mount_path: str = "/mcp",
        description_prefix: str = "",
        *,
        auth: MCPAuthCallback | None = None,
    ) -> None:
        """Configure the MCP endpoint, optional shared auth, and tool context.

        ``auth`` is passed to each registered Ninja operation only during MCP
        execution. It may be synchronous or asynchronous and receives the
        final synthetic Django request used by the operation.
        """
        if not isinstance(description_prefix, str):
            raise ValueError("MCP tool description prefix must be a string.")
        if auth is not None and not callable(auth):
            raise TypeError("MCP auth must be callable or None.")
        if self._django_application is not None or self._mcp_server is not None:
            raise RuntimeError("MCP host must be configured before initialization.")
        self._mcp_path = self._normalize_path(mount_path)
        self._description_prefix = description_prefix.strip()
        self._mcp_auth = auth
        self._registrations = tuple(
            replace(registration, auth=auth) for registration in self._registrations
        )

    @staticmethod
    def _normalize_path(path: str) -> str:
        """Normalize a configured URL path without a trailing slash."""
        if not isinstance(path, str) or not path.strip():
            raise ValueError("Configured MCP and API paths must be non-empty strings.")
        value = path.strip()
        if any(character.isspace() for character in value):
            raise ValueError("Configured MCP and API paths cannot contain whitespace.")
        parsed = urlsplit(value)
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("Configured MCP and API paths must be path-only values.")
        return f"/{parsed.path.lstrip('/')}".rstrip("/") or "/"

    @staticmethod
    def _paths_overlap(first: str, second: str) -> bool:
        """Return whether two normalized API prefixes can match one request."""
        return (
            first == second
            or first == "/"
            or second == "/"
            or first.startswith(f"{second}/")
            or second.startswith(f"{first}/")
        )

    def register(
        self,
        registry: NinjaMCARouter,
        *,
        api_base_path: str,
        description: str,
    ) -> MCPRegistration:
        """Register one MCA router before the host starts serving requests.

        ``api_base_path`` is the complete URL prefix where the router is
        mounted by Django and Ninja. The required description is included in
        the shared ``mc_api`` tool description.
        """
        if self._django_application is not None or self._mcp_server is not None:
            raise RuntimeError("MCA APIs must be registered before host initialization.")
        if not isinstance(registry, NinjaMCARouter):
            raise TypeError("registry must be a NinjaMCARouter.")
        if not isinstance(description, str) or not description.strip():
            raise ValueError("MCA API registration requires a non-empty description.")

        base_path = self._normalize_path(api_base_path)
        if any(self._paths_overlap(base_path, item.api_base_path) for item in self._registrations):
            raise RuntimeError(f"MCA API path '{base_path}' overlaps a registered API path.")

        registration = MCPRegistration(
            base_path,
            description.strip(),
            registry,
            self._mcp_auth,
        )
        self._registrations += (registration,)
        return registration

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
                "Route must be a full API path without a scheme, host, or fragment.",
                "route",
            )

        path = parsed.path if parsed.path.startswith("/") else f"/{parsed.path}"
        values = parse_qs(parsed.query, keep_blank_values=True)
        query_params = {
            key: values[0] if len(values) == 1 else values
            for key, values in values.items()
        }
        return method, path, query_params

    def _resolve_registration(self, path: str) -> tuple[MCPRegistration, str]:
        """Resolve a full API path to a registration and relative route."""
        for registration in self._registrations:
            base_path = registration.api_base_path
            if base_path == "/":
                matches = True
            else:
                matches = path == base_path or path.startswith(f"{base_path}/")
            if not matches:
                continue

            relative_path = path if base_path == "/" else path[len(base_path) :] or "/"
            return registration, relative_path

        registered_paths = ", ".join(item.api_base_path for item in self._registrations) or "none"
        raise MCAError(
            "unknown_api",
            f"No registered API matches {path}. Registered API paths: {registered_paths}.",
            "route",
            404,
        )

    async def _call_operation(
        self,
        registry: NinjaMCARouter,
        operation: str,
        method: str,
        path: str,
        path_params: dict[str, Any],
        query_params: dict[str, Any],
        body: Any,
        request_headers: Mapping[str, str] | None = None,
        auth: MCPAuthCallback | None = None,
    ) -> str:
        """Invoke a resolved Ninja operation and serialize its JSON response."""
        source_request = None
        if self.request_context_factory is not None:
            source_request = self.request_context_factory(
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
            auth=auth,
        )
        if response.status_code >= 400:
            raise self._tool_error(response)
        return json.dumps(self._json_response(response), ensure_ascii=False)

    async def _call_route(
        self,
        route: str,
        body: Any,
        request_headers: Mapping[str, str] | None = None,
    ) -> str:
        """Validate and resolve a full API route before execution."""
        method, path, query_params = self._parse_route(route)
        registration, relative_path = self._resolve_registration(path)
        if body is not None and method not in BODY_METHODS:
            raise MCAError(
                "invalid_body",
                f"HTTP {method} routes cannot receive a JSON body.",
                "body",
            )

        resolved = registration.registry.resolve(method, relative_path)
        if resolved is None:
            raise MCAError(
                "unknown_route",
                f"No route matches {method} {path}.",
                "route",
                404,
            )

        operation, path_params = resolved
        return await self._call_operation(
            registration.registry,
            operation,
            method,
            relative_path,
            path_params,
            query_params,
            body,
            request_headers,
            registration.auth,
        )

    def _tool_description(self) -> str:
        """Build the shared tool description from registered API metadata."""
        if self._registrations:
            api_list = "\n".join(
                f"- {item.api_base_path}: {item.description}" for item in self._registrations
            )
        else:
            api_list = "- No APIs are registered."
        generated_description = (
            "Call registered MCA APIs with a full HTTP-style route. "
            "Use route='GET /api/path' for discovery or an operation, and pass "
            "body for JSON request data. Results are JSON text; HTTP 204 responses "
            "return null. Registered API paths:\n"
            f"{api_list}"
        )
        if not self._description_prefix:
            return generated_description
        return f"{self._description_prefix}\n\n{generated_description}"

    def build_server(self) -> MCPServer:
        """Build one MCP server exposing all registered APIs through ``mc_api``."""
        if self._mcp_server is not None:
            return self._mcp_server

        server = MCPServer("MCA API")

        @server.tool(
            name=MCP_TOOL_NAME,
            description=self._tool_description(),
            structured_output=False,
        )
        async def call_api(
            route: str,
            body: Any = None,
            context: MCPContext | None = None,
        ) -> str:
            """Handle one MCP tool call using a full API route."""
            try:
                return await self._call_route(
                    route,
                    body,
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

        call_api.__name__ = MCP_TOOL_NAME
        self._mcp_server = server
        return server

    async def _dispatch(self, scope: dict[str, Any], receive: Any, send: Any):
        """Route the one MCP path to MCP and delegate everything else to Django."""
        self._initialize()
        if scope.get("type") == "http":
            path = scope.get("path", "")
            normalized_path = path.rstrip("/") or "/"
            if normalized_path == self._mcp_path:
                mcp_scope = dict(scope)
                mcp_scope["path"] = "/"
                mcp_scope["raw_path"] = b"/"
                mcp_scope["root_path"] = ""
                return await self._mcp_application(mcp_scope, receive, send)

        return await self._django_application(scope, receive, send)

    def _initialize(self) -> None:
        """Initialize Django and the shared MCP application exactly once."""
        if self._django_application is not None:
            return

        from django.core.asgi import get_asgi_application

        self._django_application = get_asgi_application()
        self.build_server()
        self._mcp_application = self._mcp_server.streamable_http_app(
            streamable_http_path="/",
            stateless_http=True,
        )

    @asynccontextmanager
    async def _mcp_lifespan(self):
        """Run the shared MCP session manager during ASGI lifespan."""
        self._initialize()
        assert self._mcp_server is not None
        async with self._mcp_server.session_manager.run():
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


# Shared application host for app-local MCP router registration.
mcp_host = MCPHost()
