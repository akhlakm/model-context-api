# MCP hosting

MCP is a tool protocol used by clients that cannot or should not call an
application's REST API directly. An MCP tool gives the client a structured
entry point, while MCA keeps the tool contract aligned with the underlying
HTTP routes and schemas.

## One shared MCP host

`MCPHost` wraps a Django ASGI application and exposes the MCA routers explicitly
registered by the application. Use direct HTTP when the client can reach the
REST API and should use its normal authentication. Use MCP when the client
needs a tool-oriented connection or only has access to the MCP endpoint.

Each application registers its own router from its `api.py` module. The module
must be imported during Django startup, normally through the application's URL
configuration or `AppConfig.ready()`:

~~~python
# myapp/api.py
from mca.mcp import mcp_host

mcp_host.register(
    mca_registry,
    api_base_path="/api/items",
    description="Public item API.",
)
~~~

The project ASGI module can remain minimal:

~~~python
# project/asgi.py
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "project.settings")

from mca.mcp import mcp_host

application = mcp_host
~~~

For registered applications, the host provides one shared MCP endpoint and
tool:

~~~text
Streamable HTTP endpoint: /mcp
MCP tool name:           mc_api
REST API base paths:     the paths registered by each app
~~~

All other paths are passed to Django's normal ASGI application. The host
initializes Django, owns the MCP session-manager lifespan, and mounts one
stateless Streamable HTTP application for all explicitly registered routers.

## Configure the MCP mount and description

The MCP mount defaults to `/mcp`. Configure it before the host initializes
when the project exposes MCP elsewhere. `description_prefix` is optional and
appears before the generated usage instructions and registered API list:

~~~python
from mca.mcp import mcp_host

mcp_host.configure(
    mount_path="/api/v2/mcp",
    description_prefix=(
        "This tool accesses the public v2 billing APIs. "
        "Use it for invoice lookup and management."
    ),
)
~~~

The prefix lets an application explain what the tool accesses, when to use it,
and what kind of routes it accepts. The host's MCP path is intentionally an
implementation detail; clients should use the configured endpoint rather than
depend on an internal property.

## Register multiple API mounts

Pass each router's complete REST API mount path when registering it. The path
must match the route where the corresponding Ninja API is mounted, including
any version and application segments:

~~~python
from mca.mcp import mcp_host

mcp_host.register(
    app1_registry,
    api_base_path="/api/v1/app1",
    description="Version 1 application API.",
)
mcp_host.register(
    app2_registry,
    api_base_path="/api/v2/app2",
    description="Version 2 application API.",
)
~~~

The single `mc_api` tool routes a request to the registered API whose base path
matches the request. The base path is also used to strip the prefix before
dispatching to the selected router. A route under `/api/v2/app2` is never
dispatched to the `/api/v1/app1` registry.

The MCP host does not infer these paths from a root NinjaAPI. Register the
complete mount path explicitly so composition remains correct when separate
apps are mounted at different versions or prefixes.

## Use the `mc_api` tool

The tool accepts a full API HTTP-style route and an optional JSON body:

~~~text
mc_api(route, body=None)
~~~

The tool description lists every registered API base path and its description.
Start discovery at the relevant API base path:

~~~text
mc_api(route="GET /api/items")
mc_api(route="GET /api/items?guide=items.md")
mc_api(route="GET /api/items?operation=get_item")
~~~

Then invoke operations with their full API paths:

~~~text
mc_api(route="GET /api/items/items/7")
mc_api(
    route="POST /api/items/items",
    body={"name": "Created through MCP", "description": "Example"},
)
mc_api(route="DELETE /api/items/items/7")
~~~

The route must include the registered REST API prefix and must not include the
MCP prefix:

~~~text
Correct:   GET /api/items/items/7
Incorrect: GET /items/7
Incorrect: GET /mcp/api/items/items/7
~~~

The route parser accepts a method and full API path with or without a leading
slash, parses query strings, preserves repeated query parameters, accepts JSON
bodies only for POST, PUT, and PATCH, and resolves the path after the
registered API prefix against the selected router.

## Authentication and errors

MCP execution supports both synchronous and asynchronous Ninja operations,
including asynchronous `get_context` discovery and mounted-router composition.
`MCPHost` copies incoming MCP transport headers into a synthetic Django
request, so header-based authentication works without application-specific ASGI
code. An MCP client can therefore send the same token or API key header that a
normal HTTP client would send.

For a shared authentication policy across all MCP-exposed APIs, configure an
async or synchronous Ninja-compatible callback on the host:

~~~python
from mca.mcp import mcp_host
from myapp.auth import MCPJWTAuthAsync

mcp_host.configure(
    mount_path="/api/v2/mcp",
    auth=MCPJWTAuthAsync(),
    description_prefix="This tool accesses the v2 formulation APIs.",
)
~~~

If importing the callback module requires Django's app registry, defer the
import until after Django initializes by using its dotted path:

~~~python
from mca.mcp import mcp_host

mcp_host.configure(
    mount_path="/api/v2/mcp",
    auth_path="core.auth.jwt_auth",
)
~~~

The path must identify an already-constructed callable export. The MCP host
resolves it after Django initialization. Applications that import the callback
directly must initialize Django before the import, for example with
`django.setup()` in an ASGI entry point.

The callback runs as Ninja operation authentication on the final request passed
to each handler, including discovery. It can populate `request.user`,
`request.auth`, or application-specific fields such as `request.token_log` and
`request.iced_key`. The MCP callback replaces operation-level auth only for
MCP execution; normal Django and Ninja routes keep their configured
authentication. With a host callback, the shared router can remain a plain
`Router(tags=[...])` without repeating `auth=` on every operation.

Applications that need custom users, sessions, or credential translation can
provide a `request_context_factory`; its final argument is a mapping of headers
from the incoming MCP transport request.

Successful results are returned as JSON text. A 204 response is represented as
`null`. Invalid routes, validation failures, unknown operations, and endpoint
errors are returned as MCP tool errors containing the MCA error code, detail,
field, and HTTP status.

## Build one MCP server directly

Applications that want to mount the shared API tool without using the `MCPHost`
ASGI wrapper can build the MCP server directly:

~~~python
from mca.mcp import MCPHost
from myapp.api import mca_registry

host = MCPHost()
host.register(
    mca_registry,
    api_base_path="/api/items",
    description="Public item API.",
)
server = host.build_server()
application = server.streamable_http_app(
    streamable_http_path="/",
    stateless_http=True,
)
~~~

The resulting server exposes the `mc_api` tool. The surrounding ASGI
application is responsible for starting the server's session manager and for
providing Django settings.
