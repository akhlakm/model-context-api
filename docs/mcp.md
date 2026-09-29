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

Each application registers its own router when the router is constructed. An
`api.py` import during Django startup is a common arrangement, but it is not
required: the host accepts valid registrations after Django and MCP have
initialized as well.

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
Registrations added later are immediately available for routing and appear in
subsequent `tools/list` results. Clients that cache tool metadata should refresh
their tool list after a late registration.

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

The single `mc_api` tool accepts only routes belonging to a registered API.
It then sends the full route through Django's ASGI application, so the route
must also exist at that path in Django's URL configuration. A route under
`/api/v2/app2` is never accepted by the `/api/v1/app1` registration.

The MCP host does not infer these paths from a root NinjaAPI. Register the
complete mount path explicitly so composition remains correct when separate
apps are mounted at different versions or prefixes.

## Alternative: Build one MCP server directly

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

## Use the `mc_api` tool

The tool accepts a full API HTTP-style route and an optional JSON body:

~~~text
mc_api(route, body=None)
~~~

The tool description lists every registered API base path and its description.
Start discovery at the router's actual Django URL. For a Ninja router mounted
at `/api/items`, its default discovery URL is typically `/api/items/`:

~~~text
mc_api(route="GET /api/items/")
mc_api(route="GET /api/items/?guide=items.md")
mc_api(route="GET /api/items/?operation=get_item")
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
slash, preserves query strings and repeated query parameters, and accepts JSON
bodies only for POST, PUT, and PATCH. The URL must match the mounted Django
route, including any required trailing slash.

## Authentication and errors

MCP execution supports both synchronous and asynchronous Ninja operations,
including asynchronous `get_context` discovery and mounted-router composition.
Each tool call passes through Django URL routing and middleware before Ninja
runs the operation. Incoming MCP credentials and cookies are forwarded to the
API request; Django middleware and Ninja route auth enforce the same policy as
direct HTTP. Public HTTP routes, including discovery when configured as public,
remain public through MCP. The `/mcp` protocol endpoint and `tools/list` remain
outside Django middleware. Clients should send credentials on every tool call;
cookies set by an API response are not relayed back through the MCP result.

Successful results are returned as JSON text. A 204 response is represented as
`null`. Invalid routes, validation failures, unknown operations, and endpoint
errors are returned as MCP tool errors containing the MCA error code, detail,
field, and HTTP status.

## Connect MCP clients with an API token

Once the Django middleware or Ninja route is configured to validate a bearer
token, clients can attach the token to their Streamable HTTP MCP connection.
The client configuration supplies the HTTP header; it does not define the
application's authentication policy.

### Connect from Codex CLI with an API token

The recommended Codex setup uses a local `http_headers_helper`. Current Codex
versions have bugs in their bearer-token environment-variable support, while a
helper gives Codex the header explicitly for each MCP HTTP request.

The helper should print one JSON object containing the headers Codex should add
to MCP HTTP requests. This example reads the raw token from a protected file:

~~~sh
#!/bin/sh
set -eu

token_file="/absolute/path/to/API_TOKEN"
token="$(tr -d '\r\n' < "$token_file")"
printf '{"Authorization":"Bearer %s"}\n' "$token"
~~~

Save the script as an executable file and keep the token file readable only by
the user:

~~~bash
chmod 700 /absolute/path/to/mca-mcp-headers
chmod 600 /absolute/path/to/API_TOKEN
~~~

Register the server through the Codex CLI. The `-c` option sets the nested
configuration value, so no manual editing of `config.toml` is needed:

~~~bash
codex mcp add my-mca \
    --url https://your-host.example.com/mcp \
    -c 'mcp_servers.my-mca.http_headers_helper="/absolute/path/to/mca-mcp-headers"'

codex mcp get my-mca
codex mcp list
~~~

The token file in this example contains only the raw token, not an
`API_TOKEN=...` assignment. The helper must print only valid JSON on stdout;
Codex uses the resulting `Authorization: Bearer <token>` header for MCP
initialization and tool calls. This option is supported for locally connected
HTTP MCP servers; see the
[Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
for the full setting definition.

Codex also supports attaching a bearer token from an environment variable, but
this option is currently unreliable and should be treated as a fallback:

~~~bash
export MCA_API_TOKEN='your-api-token'

codex mcp add my-mca \
    --url https://your-host.example.com/mcp \
    --bearer-token-env-var MCA_API_TOKEN

codex mcp list
~~~

Start or restart Codex from the environment where `MCA_API_TOKEN` is set. Codex
sends the token as `Authorization: Bearer <token>` during MCP initialization and
subsequent tool calls when the environment-variable integration works. The
token value is not written into the Codex server configuration; only the
environment-variable name is configured.

After the server connects, invoke the shared tool normally, using the full REST
API route rather than the MCP mount path:

~~~text
mc_api(route="GET /api/items/")
mc_api(route="GET /api/items/items/7")
~~~

### Connect from Claude Code with an API token

Claude Code can configure a remote HTTP MCP server with a bearer token expanded
from an environment variable:

~~~bash
export MCA_API_TOKEN='your-api-token'

claude mcp add-json my-mca \
    '{"type":"http","url":"https://your-host.example.com/mcp","headers":{"Authorization":"Bearer ${MCA_API_TOKEN}"}}'

claude mcp get my-mca
claude mcp list
~~~

The JSON is single-quoted so the shell leaves `${MCA_API_TOKEN}` for Claude Code
to expand when it connects. Start or restart Claude Code from the environment
where `MCA_API_TOKEN` is set, then use `/mcp` to inspect the connection. Use
`--scope user` when the server should be available across projects; the default
scope is local to the current project. See the
[Claude Code MCP documentation](https://code.claude.com/docs/en/mcp) for other
scopes and authentication options.

### Connect from Pi CLI with an API token

Pi can connect to this endpoint through the `pi-mcp-adapter` extension. Install
the extension and export the token before starting Pi:

~~~bash
pi install npm:pi-mcp-adapter
export MCA_API_TOKEN='your-api-token'
~~~

Add the server to the project's `.mcp.json`:

~~~json
{
    "mcpServers": {
        "my-mca": {
            "url": "https://your-host.example.com/mcp",
            "auth": "bearer",
            "bearerTokenEnv": "MCA_API_TOKEN"
        }
    }
}
~~~

Start or restart Pi and check the connection with `/mcp`. If the server uses
lazy startup, connect it with `/mcp reconnect my-mca`. Pi sends the token as
`Authorization: Bearer <token>` while keeping the token value out of the MCP
configuration. See the [`pi-mcp-adapter` documentation](https://pi.dev/packages/pi-mcp-adapter)
for global configuration and additional connection options.
