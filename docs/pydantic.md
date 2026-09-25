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

For composing public routers with private MCA services, see
[Composing MCA services](composition.md).
