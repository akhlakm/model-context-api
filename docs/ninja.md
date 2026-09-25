# Django Ninja APIs

Use `NinjaMCARouter` to register operations on either a NinjaAPI or a Django
Ninja Router. Options such as `response`, `auth`, `tags`, and other Django
Ninja route options are passed through to the corresponding registration
method.

When a Router is supplied, its own auth and throttle configuration is used for
MCA schema discovery and internal execution. The Router can then be mounted on
a root NinjaAPI with `api.add_router(...)`; MCA does not require that root API
to generate operation schemas.

This adapter is for an existing HTTP API. It does not create a second business
logic layer: the decorated functions remain ordinary Django Ninja endpoints.
MCA adds the shared discovery operation, guide access, operation descriptions,
and a schema view assembled from Django Ninja's OpenAPI metadata. Router-backed
registries bind lazily to an internal NinjaAPI for this metadata and execution.

## Register a Ninja API

~~~python
# myapp/api.py
from pathlib import Path

from django.http import HttpRequest
from ninja import NinjaAPI, Path as NinjaPath, Query
from pydantic import BaseModel, Field

from mca.base import MCAError
from mca.mcp import mcp_host
from mca.ninja import NinjaMCARouter


class ItemIn(BaseModel):
    name: str = Field(..., min_length=1)
    description: str = ""


class ItemOut(BaseModel):
    item_id: int
    name: str
    description: str


api = NinjaAPI(title="Items API", version="1.0")

# Register this router explicitly with the shared MCP host.
mca_registry = NinjaMCARouter(
    api,
    guides_dir=Path(__file__).with_name("guides"),
    title="Items API",
    version=1.0,
)
mcp_host.register(
    mca_registry,
    api_base_path="/api/items",
    description="Public item API.",
)


@mca_registry.register(
    "/items",
    response=list[ItemOut],
    description="List items.",
)
def get_items(request: HttpRequest) -> list[ItemOut]:
    return [ItemOut(item_id=1, name="Example", description="")]


@mca_registry.register(
    "/items/{item_id}",
    response=ItemOut,
    description="Read one item.",
)
def get_item(
    request: HttpRequest,
    item_id: int = NinjaPath(..., description="Unique item identifier."),
    verbose: bool = Query(False, description="Include the long description."),
) -> ItemOut:
    return ItemOut(
        item_id=item_id,
        name=f"Item {item_id}",
        description="Detailed." if verbose else "",
    )


@mca_registry.register(
    "/items",
    response=ItemOut,
    description="Create an item.",
)
def make_item(request: HttpRequest, payload: ItemIn) -> ItemOut:
    return ItemOut(item_id=2, name=payload.name, description=payload.description)


@api.exception_handler(MCAError)
def handle_mca_error(request: HttpRequest, exc: MCAError):
    return api.create_response(
        request,
        {"code": exc.code, "detail": exc.detail, "field": exc.field},
        status=exc.status,
    )
~~~

Mount the NinjaAPI in Django as usual:

~~~python
# project/urls.py
from django.urls import path

from myapp.api import api

urlpatterns = [
    path("api/items/", api.urls),
]
~~~

The resulting routes are:

~~~text
GET  /api/items/                    Root MCA discovery
GET  /api/items/?guide=items.md     Read a guide
GET  /api/items/?operation=get_item Read an operation schema
GET  /api/items/items/7             Read an item
POST /api/items/items              Create an item
~~~

Routes registered with MCA are relative to the NinjaAPI mount. Both slash
forms are registered for non-root routes; the alternate form is hidden from
the generated schema and discovery output. Operation schemas are built from
Django Ninja's OpenAPI schema and describe `path_params`, `query_params`, and
`body`.

This means a client can use the same API in two ways:

- a normal HTTP client follows the REST route and Django authentication rules;
- an agent first uses MCA discovery, reads the relevant context, and then calls
  the same REST operation with a validated request.

## Internal Ninja dispatch

A registered operation can be executed without issuing an HTTP request or
re-entering ASGI. This is useful for internal orchestration and is also how
the MCP adapter invokes registered endpoints:

~~~python
response = mca_registry.execute_http_request(
    "get_item",
    source_request=request,
    path_params={"item_id": 7},
    query_params={"verbose": True},
)
~~~

`execute_http_request` creates a request with the registered method and route,
validates it through Django Ninja, and returns a Django `HttpResponse`. When
`source_request` is supplied, the authenticated user, cookies, session, and
relevant request metadata are copied.

`allow_anonymous=True` marks a generated request as explicitly trusted internal
traffic. Only use it at a trusted boundary, and never expose it as a
user-controlled HTTP option.

For an already-created `HttpRequest`, use `execute_http`:

~~~python
response = mca_registry.execute_http(
    "get_item",
    request,
    path_params={"item_id": 7},
)
~~~

Both synchronous methods reject asynchronous Ninja endpoints. Use
`execute_http_async()` when invoking async operations or async discovery:

~~~python
response = await mca_registry.execute_http_async(
    "get_context",
    request,
)
~~~

The public Ninja `get_context` endpoint is async-aware and awaits mounted
`adiscover()` calls. Operation handlers are responsible for invoking their
underlying RPC or transport directly; `call()` and `acall()` helpers are
optional client conveniences.
