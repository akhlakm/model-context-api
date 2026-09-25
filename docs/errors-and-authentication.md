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

MCA does not impose an authentication policy. Normal Ninja requests use the
authentication configured on the NinjaAPI or route. Internal execution and MCP
hosting can explicitly opt into an application's trusted anonymous mode, but
that decision belongs to the application boundary.

For MCP requests, incoming transport headers are copied into the synthetic
Django request used for dispatch. Header-based tokens and API keys can
therefore be handled by the same Ninja authentication code as normal HTTP
requests. Use `MCPHost.configure(auth=...)` when all MCP-exposed registries
share one authentication callback; it runs at Ninja operation level and can
hydrate the request with application-specific authentication fields. Use
`request_context_factory` when an application instead needs custom user,
session, or credential translation.
