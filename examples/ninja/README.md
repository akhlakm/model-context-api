# Ninja composition example

This example runs a small Django server with two MCA routers:

- the public router is a Django Ninja `Router` wrapped by `NinjaMCARouter`;
- the private billing service is a `PydanticMCARouter` that is never added to
  Django URL configuration;
- `MockJsonRpcClient` is the only code that calls the private router, standing
  in for an async JSON-RPC client between two microservices.

The public handlers use generic JSON-compatible types and do not import the
private service's Pydantic models. During discovery, the public router fetches
and caches the private request and response schemas in one batched RPC request,
then uses them for generic public body and response schemas while retaining the
public HTTP route and path/query parameters.

From the repository root, install the Ninja and MCP extras if needed and run
Django's system check:

```bash
pip install -e ".[ninja,mcp]"
PYTHONPATH=. python examples/ninja/manage.py check
```

For the REST API only, start Django's development server:

```bash
PYTHONPATH=. python examples/ninja/manage.py runserver
```

For the REST API and MCP endpoint together, start the ASGI application:

```bash
PYTHONPATH=examples/ninja uvicorn demo.asgi:application --reload
```

The demo app registers the public registry from `demo/api.py`, while the ASGI
entry point exports the shared MCP host:

```text
Streamable HTTP endpoint: http://127.0.0.1:8000/api/demo/mcp
MCP tool name:           demo_api
REST API base path:      /api
```

The MCP tool uses routes relative to `/api`, for example
`demo_api(route="GET /")` for discovery or
`demo_api(route="GET /invoices/7")` for an operation. Configure the MCP
client's HTTP transport with `X-Demo-Token: demo-token` for authenticated
invoice operations. Discovery works without credentials.

Inspect public discovery:

```bash
curl http://127.0.0.1:8000/api/
curl 'http://127.0.0.1:8000/api/?operation=get_public_invoice,make_public_invoice,update_public_invoice,remove_public_invoice'
curl 'http://127.0.0.1:8000/api/?guide=billing/invoices.md'
curl 'http://127.0.0.1:8000/api/?guide=billing/invoices/legacy_format.md'
```

The root discovery response returns the public `guides/index.md` content as
`help` and the configured router instructions as `usage`.

Call the public GET operation with the demo token:

```bash
curl -H 'X-Demo-Token: demo-token' \
  http://127.0.0.1:8000/api/invoices/7
```

The public POST, PATCH, and DELETE operations use the same explicit RPC
composition pattern:

```bash
curl -X POST \
  -H 'X-Demo-Token: demo-token' \
  -H 'Content-Type: application/json' \
  -d '{"customer":"New Customer","total":42.5}' \
  http://127.0.0.1:8000/api/invoices

curl -X PATCH \
  -H 'X-Demo-Token: demo-token' \
  -H 'Content-Type: application/json' \
  -d '{"status":"paid"}' \
  http://127.0.0.1:8000/api/invoices/7

curl -X DELETE \
  -H 'X-Demo-Token: demo-token' \
  http://127.0.0.1:8000/api/invoices/7
```

The public handler performs authentication, checks the invoice ACL, records an
access log entry, and then calls `billing_rpc`. The RPC client invokes the
private Pydantic router and serializes its response as JSON. The private
operation names are `get_invoice`, `make_invoice`, `update_invoice`, and
`remove_invoice`; they are never registered as public HTTP routes. The limited
demo token authenticates successfully but is denied by the write/delete ACL.

The public discovery endpoint uses the async RPC path, so it awaits
`billing_rpc.adiscover()` before merging private schemas and guides. Async
application handlers may invoke `await billing_rpc.acall(...)` for private
operations, but operation-call helpers are not required by the mounted MCA
client contract.

```bash
curl -i -H 'X-Demo-Token: limited-token' \
  http://127.0.0.1:8000/api/invoices/7
```
