"""MCP hosting for applications that publish MCA Ninja routers."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any
from urllib.parse import unquote, urlsplit

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context as MCPContext
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import Tool as MCPTool

from .base import MCAError
from .ninja import NinjaMCARouter

BODY_METHODS = {"POST", "PUT", "PATCH"}
MCP_TOOL_NAME = "mc_api"


@dataclass(frozen=True)
class MCPRegistration:
    """One MCA router registered with the shared MCP API tool."""

    api_base_path: str
    description: str
    registry: NinjaMCARouter


@dataclass(frozen=True)
class _DjangoResponse:
    status_code: int
    content: bytes
    headers: tuple[tuple[bytes, bytes], ...]

    @property
    def reason_phrase(self) -> str:
        try:
            return HTTPStatus(self.status_code).phrase
        except ValueError:
            return "Unknown HTTP response"


class _DynamicMCPServer(MCPServer):
    """MCP server whose shared tool description reflects current registrations."""

    def __init__(self, description_factory: Callable[[], str]):
        self._description_factory = description_factory
        super().__init__("MCA API")

    async def list_tools(self) -> list[MCPTool]:
        """Return tool metadata with the current MCA API description."""
        tools = await super().list_tools()
        for tool in tools:
            if tool.name == MCP_TOOL_NAME:
                tool.description = self._description_factory()
        return tools


class MCPHost:
    """ASGI host exposing registered MCA routers through one MCP API tool.

    The host forwards full API paths from the shared ``mc_api`` tool to the
    matching registered router and all other traffic to Django.
    """

    def __init__(self):
        """Create an uninitialized host with one MCP endpoint."""
        self._django_application: Any | None = None
        self._mcp_server: MCPServer | None = None
        self._mcp_application: Any | None = None
        self._registrations: tuple[MCPRegistration, ...] = ()
        self._mcp_path = self._normalize_path("/mcp")
        self._description_prefix = ""

    def configure(
        self,
        mount_path: str = "/mcp",
        description_prefix: str = "",
    ) -> None:
        """Configure the MCP endpoint and shared tool context."""
        if not isinstance(description_prefix, str):
            raise ValueError("MCP tool description prefix must be a string.")
        if self._django_application is not None or self._mcp_server is not None:
            raise RuntimeError("MCP host must be configured before initialization.")
        self._mcp_path = self._normalize_path(mount_path)
        self._description_prefix = description_prefix.strip()

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
        """Register one MCA router with the shared MCP API tool.

        ``api_base_path`` is the complete URL prefix where the router is
        mounted by Django and Ninja. The required description is included in
        the shared ``mc_api`` tool description. Registrations may be added
        before or after the host initializes.
        """
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
        )
        self._registrations += (registration,)
        return registration

    @staticmethod
    def _json_response(response: _DjangoResponse) -> Any:
        """Decode a successful Django response, treating empty/204 as ``None``."""
        if response.status_code == 204 or not response.content:
            return None
        return json.loads(response.content)

    @staticmethod
    def _tool_error(response: _DjangoResponse) -> ToolError:
        """Convert an HTTP error response into an MCP ``ToolError`` payload."""
        try:
            payload = json.loads(response.content)
        except (TypeError, ValueError, UnicodeDecodeError):
            location = next(
                (value.decode("latin1") for key, value in response.headers if key.lower() == b"location"),
                None,
            )
            payload = {
                "code": "mcp_endpoint_error",
                "detail": (
                    f"{response.reason_phrase}: {location}" if location else response.reason_phrase
                ),
            }
        if isinstance(payload, dict):
            payload = {**payload, "status": response.status_code}
        else:
            payload = {
                "code": "mcp_endpoint_error",
                "detail": payload,
                "status": response.status_code,
            }
        return ToolError(json.dumps(payload, ensure_ascii=False))

    @staticmethod
    def _transport_request(context: MCPContext | None) -> tuple[Mapping[str, str], Mapping[str, Any]]:
        """Return incoming MCP headers and ASGI metadata, when available."""
        if context is None:
            return {}, {}
        try:
            request = context.request_context.request
        except ValueError:
            return {}, {}
        if request is None:
            return {}, {}
        return request.headers, getattr(request, "scope", {})

    @staticmethod
    def _parse_route(route: str) -> tuple[str, str, str]:
        """Parse an MCP route argument into method, path, and raw query."""
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
        return method, path, parsed.query

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

    async def _call_django(
        self,
        method: str,
        path: str,
        query: str,
        body: Any,
        request_headers: Mapping[str, str],
        transport_scope: Mapping[str, Any],
    ) -> _DjangoResponse:
        """Send one API request through Django's normal ASGI handler."""
        self._initialize()
        request_body = (
            json.dumps(body, ensure_ascii=False).encode("utf-8")
            if body is not None else b""
        )
        excluded_headers = {
            "accept",
            "connection",
            "content-length",
            "content-type",
            "transfer-encoding",
            "mcp-session-id",
            "mcp-protocol-version",
            "last-event-id",
        }
        headers = [
            (name.lower().encode("latin1"), value.encode("latin1"))
            for name, value in request_headers.items()
            if name.lower() not in excluded_headers
        ]
        headers.append((b"accept", b"application/json"))
        if body is not None:
            headers.extend([
                (b"content-type", b"application/json"),
                (b"content-length", str(len(request_body)).encode("ascii")),
            ])
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": transport_scope.get("http_version", "1.1"),
            "method": method,
            "scheme": transport_scope.get("scheme", "http"),
            "path": unquote(path),
            "raw_path": path.encode("utf-8"),
            "root_path": transport_scope.get("_mca_root_path", transport_scope.get("root_path", "")),
            "query_string": query.encode("utf-8"),
            "headers": headers,
            "server": transport_scope.get("server", ("localhost", 80)),
            "client": transport_scope.get("client", ("127.0.0.1", 0)),
        }
        received = False
        disconnect = asyncio.Event()

        async def receive() -> dict[str, Any]:
            nonlocal received
            if not received:
                received = True
                return {"type": "http.request", "body": request_body, "more_body": False}
            await disconnect.wait()
            raise RuntimeError("Django requested another body message after completion.")

        status: int | None = None
        response_headers: tuple[tuple[bytes, bytes], ...] = ()
        chunks: list[bytes] = []

        async def send(message: dict[str, Any]) -> None:
            nonlocal status, response_headers
            if message["type"] == "http.response.start":
                status = message["status"]
                response_headers = tuple(message.get("headers", ()))
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))

        await self._django_application(scope, receive, send)
        if status is None:
            raise RuntimeError("Django did not return an HTTP response.")
        return _DjangoResponse(status, b"".join(chunks), response_headers)

    async def _call_route(
        self,
        route: str,
        body: Any,
        request_headers: Mapping[str, str] | None = None,
        transport_scope: Mapping[str, Any] | None = None,
    ) -> str:
        """Validate and resolve a full API route before execution."""
        method, path, query = self._parse_route(route)
        # ASGI passes a decoded path to Django's URL resolver. Check that same
        # path against the MCA allowlist before dispatching it.
        registration, relative_path = self._resolve_registration(unquote(path))
        if body is not None and method not in BODY_METHODS:
            raise MCAError(
                "invalid_body",
                f"HTTP {method} routes cannot receive a JSON body.",
                "body",
            )

        if registration.registry.resolve(method, relative_path) is None:
            raise MCAError(
                "unknown_route",
                f"No route matches {method} {path}.",
                "route",
                404,
            )

        response = await self._call_django(
            method, path, query, body, request_headers or {}, transport_scope or {},
        )
        if not 200 <= response.status_code < 300:
            raise self._tool_error(response)
        try:
            return json.dumps(self._json_response(response), ensure_ascii=False)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ToolError(
                json.dumps(
                    {
                        "code": "mcp_endpoint_error",
                        "detail": "The API returned a non-JSON success response.",
                        "status": response.status_code,
                    }
                )
            ) from exc

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
            "Use the exact mounted Django URL, such as route='GET /api/path/' "
            "for discovery, and pass "
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

        server = _DynamicMCPServer(self._tool_description)

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
                request_headers, transport_scope = self._transport_request(context)
                return await self._call_route(
                    route,
                    body,
                    request_headers,
                    transport_scope,
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
                mcp_scope["_mca_root_path"] = scope.get("root_path", "")
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
