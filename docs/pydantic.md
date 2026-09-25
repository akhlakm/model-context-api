# Framework-independent Pydantic APIs

Use `PydanticMCARouter` when the engine should be callable without Django or
another web framework.

## Registration and dispatch

The example below publishes three capabilities: reading an item, creating an
item, and reading API status. The decorator supplies an optional route and
description. If no description is supplied, MCA uses the operation function's
docstring. The function name supplies the HTTP method and operation name. The
Pydantic annotations tell MCA which values are inputs and what a successful
response looks like.

~~~python
# myapp/engine.py
from pathlib import Path

from pydantic import BaseModel, Field

from mca.pydantic import PydanticMCARouter


class ItemParams(BaseModel):
    item_id: int = Field(..., description="Unique item identifier.")
    verbose: bool = Field(False, description="Include the long description.")


class ItemCreate(BaseModel):
    name: str = Field(..., min_length=1)
    description: str = ""


class ItemOut(BaseModel):
    item_id: int
    name: str
    description: str


router = PydanticMCARouter(
    guides_dir=Path(__file__).with_name("guides"),
    title="Items API",
    version=1.0,
    usage="Use operation and guide discovery before calling an item route.",
)


@router.register(
    "/items/{item_id}",
    description="Read one item.",
    guides=["items.md"],
)
def get_item(params: ItemParams) -> ItemOut:
    return ItemOut(
        item_id=params.item_id,
        name=f"Item {params.item_id}",
        description="Detailed." if params.verbose else "",
    )


@router.register("/items", description="Create an item.")
def make_item(data: ItemCreate) -> ItemOut:
    return ItemOut(item_id=1, name=data.name, description=data.description)


@router.register(description="Read the current API status.")
def get_status() -> ItemOut:
    return ItemOut(item_id=1, name="status", description="Ready")
~~~

There is no web server in this example. The registry is an in-process
dispatcher: a caller supplies an operation or route, MCA validates the input,
calls the Python function, and validates the result. A Pydantic endpoint may
omit its route and then be called only by operation name. A web or MCP adapter
can expose path-based operations later; Django Ninja registrations always
require an explicit route.

The parameter conventions are:

- `params` is the typed path/query parameter object;
- `data` is the typed JSON body;
- the return annotation is the response schema;
- fields named in route placeholders such as `{item_id}` become path
  parameters;
- remaining `params` fields become query parameters.

A route such as `/items/{item_id}` with `ItemParams` produces this logical
request shape:

~~~json
{
  "path_params": {"item_id": 7},
  "query_params": {"verbose": true}
}
~~~

Dispatch by operation name or by an HTTP method and route path:

~~~python
# Root discovery.
discovery = router.dispatch("get_context")

# Read guides and an operation schema.
guides = router.dispatch("get_context", params={"guide": "items.md"})
schema = router.dispatch("get_context", params={"operation": "get_item"})

# Dispatch a typed operation.
created = router.dispatch(
    "make_item",
    data={"name": "Example", "description": "Created by an agent."},
)

# Route paths resolve path placeholders and validate their values.
item = router.dispatch("/items/7", params={"verbose": True}, method="GET")
~~~

Inputs are validated before an endpoint is called, and results are validated
against return annotations. Invalid request data raises a structured
`MCAError` with status 422. Unknown operations and routes raise errors with
status 404, while endpoint failures use status 500 unless the endpoint raises
an explicit status:

~~~python
from mca.base import MCAError

try:
    router.dispatch("/items/not-an-integer", method="GET")
except MCAError as exc:
    print(exc.code, exc.status, exc.field)
    # invalid_request 422 item_id
~~~

Register several methods when one implementation has the same input and
output shape:

~~~python
@router.register_all(
    "/items/{item_id}",
    operation_id="item",
    methods=("GET", "DELETE"),
)
def item(params: ItemParams) -> ItemOut | None:
    if params.item_id == 0:
        return None
    return ItemOut(item_id=params.item_id, name="Example", description="")
~~~

This creates `get_item` and `remove_item`. Separate `register` decorators are
clearer when methods have different request or response models.

## Composing private MCA services

The same explicit composition pattern applies to both adapters. A public
router can mount private MCA services through a small client adapter. The
client can use JSON-RPC, HTTP, or another transport; it only needs to provide
`discover()`, `adiscover()`, or both. Operation calls are owned by the public
endpoints and may invoke the underlying RPC or transport directly.

When discovery needs schemas for multiple delegated operations, the public
router batches the missing operation names into one `get_context` request per
mounted service and caches each returned schema. Guide content is requested
separately only when that guide is explicitly requested. Synchronous discovery
uses `discover()`; async discovery uses `await adiscover()`. A client that only
supports one discovery mode can still be mounted, but the corresponding
discovery path is unavailable.

### PydanticMCARouter

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

Mounting is only composition setup; it does not publish or dispatch private
operations automatically. Each capability that should be public gets its own
public operation and explicitly identifies the private operation it may call:

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

### NinjaMCARouter

Ninja composition is explicit. Mounting a private client does not register any
of its routes on the public API. Each public operation gets its own route,
authorization, and handler; the handler can perform authentication, ACL,
tracking, or input transformation before calling the private operation:

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

`delegate_to` is discovery metadata and is not passed to Django Ninja. The
public operation name and route remain authoritative, so discovery publishes
`get_invoice` and its public request/response schema—not the private route.
Guides attached to the private operation are available under names such as
`billing/invoices.md`. Private operations and unassociated private guides are
not published unless another public route explicitly delegates to them.
