# Errors and authentication

An MCA error is a normal part of the published contract, not an implementation
detail. Clients need a stable code to decide whether to retry, ask for a
missing value, choose another operation, or report a domain failure.

## Structured errors

Use `MCAError` for stable machine-readable errors:

~~~python
from mca.base import MCAError

raise MCAError(
    "item_not_found",
    "The requested item does not exist.",
    field="item_id",
    status=404,
)
~~~

The error has `code`, `detail`, an optional `field`, and an HTTP `status`. The
Pydantic adapter raises `MCAError` so an RPC client can preserve the error
across a service boundary. The Django Ninja adapter catches `MCAError` raised
by a registered operation and returns the structured `ErrorOut` payload with
the matching HTTP status. MCP converts the resulting HTTP error into an MCP
tool error.

## Authentication

MCA does not impose an authentication policy. Ninja requests use the
authentication configured on Django middleware, the NinjaAPI, or the route.
Internal execution can explicitly opt into an application's trusted anonymous
mode, but that decision belongs to the application boundary.

For MCP tool calls, incoming credentials and cookies are forwarded through
Django's normal middleware and URL routing. Header-based tokens, API keys, and
session authentication therefore follow the same rules as direct HTTP requests.
The `/mcp` protocol endpoint and `tools/list` do not pass through Django
middleware; each API route decides whether discovery and operations require
authentication. Clients must supply credentials on each tool call, because
cookies set by an API response are not relayed through the MCP result.
