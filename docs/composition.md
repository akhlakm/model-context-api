# Composing MCA services

MCA supports explicit composition when a public API delegates selected
capabilities to private MCA services. The public router owns the published
operation, route, authorization, and response contract. The private service is
accessed through a client adapter such as JSON-RPC, HTTP, or another transport.

Mounting a private service is composition setup only. It does not publish the
private service's operations or routes automatically. Each capability that
should be public gets its own public operation and explicitly calls the private
operation it needs.

## Discovery and client contracts

A mounted client needs to provide `discover()`, `adiscover()`, or both. The
public router uses the synchronous method for synchronous discovery and awaits
the asynchronous method for async discovery. A client that only supports one
mode can still be mounted, but the corresponding discovery path is unavailable.

When discovery needs schemas for multiple delegated operations, the public
router batches missing operation names into one `get_context` request per
mounted service and caches each returned schema. Guide content is requested
separately only when that guide is explicitly requested.

Operation calls remain owned by the public endpoint. The handler can perform
authentication, ACL checks, auditing, or input transformation before invoking
the private transport.

## PydanticMCARouter

~~~python
from mca.pydantic import PydanticMCARouter


class JsonRpcMCAClient:
    def __init__(self, rpc):
        self.rpc = rpc

    def discover(self, *, guide=None, operation=None):
        return self.call(
            "get_context",
            params={"guide": guide, "operation": operation},
        )

    def call(self, operation, *, params=None, data=None):
        return self.rpc.call(
            "mca.dispatch",
            {"operation": operation, "params": params, "data": data},
        )

    async def adiscover(self, *, guide=None, operation=None):
        return await self.acall(
            "get_context",
            params={"guide": guide, "operation": operation},
        )

    async def acall(self, operation, *, params=None, data=None):
        return await self.rpc.call(
            "mca.dispatch",
            {"operation": operation, "params": params, "data": data},
        )


public_router = PydanticMCARouter(title="Public API")
billing = JsonRpcMCAClient(billing_rpc)
public_router.mount("billing", billing)
~~~

Each public capability identifies the private operation it delegates to:

~~~python
@public_router.register(
    "/invoices/{invoice_id}",
    delegate_to="billing.get_invoice",
)
def get_public_invoice(params: InvoiceParams) -> InvoiceOut:
    result = billing.call(
        "get_invoice",
        params={"invoice_id": params.invoice_id},
    )
    return InvoiceOut(**result)
~~~

Discovery publishes `get_public_invoice` and its public schema. The private
operation remains unavailable as `billing.get_invoice` through the public
router. Guides attached to the delegated private operation are available under
names such as `billing/invoices.md`; unassociated private operations and
guides remain undiscoverable.

## NinjaMCARouter

Ninja composition follows the same explicit model. Mounting a private client
does not register any of its routes on the public API. Each public operation
gets its own route, authorization, and handler:

~~~python
from ninja import NinjaAPI
from mca.ninja import NinjaMCARouter

api = NinjaAPI()
public_router = NinjaMCARouter(api, title="Public API")
billing = JsonRpcMCAClient(billing_rpc)
public_router.mount("billing", billing)


@public_router.register(
    "/invoices/{invoice_id}",
    operation_id="get_invoice",
    response=InvoiceOut,
    auth=public_auth,
    delegate_to="billing.get_invoice",
)
def get_invoice(request, invoice_id: int):
    audit.log(request.auth, "get_invoice", invoice_id)
    return billing.call("get_invoice", params={"invoice_id": invoice_id})
~~~

The handler can apply authentication, ACL, tracking, or input transformation
before calling the private operation. `delegate_to` is discovery metadata and
is not passed to Django Ninja. The public operation name and route remain
authoritative, so discovery publishes `get_invoice` and its public
request/response schema—not the private route.

Guides attached to the private operation are available under names such as
`billing/invoices.md`. Private operations and unassociated private guides are
not published unless another public route explicitly delegates to them.

## Example

The [Ninja composition example](../examples/ninja/README.md) demonstrates a
public Django Ninja router backed by a private Pydantic service over a mock
JSON-RPC client. It also shows how the composed public registry is exposed
through the shared MCP endpoint.
