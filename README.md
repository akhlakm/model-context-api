# Model Context API

model-context-api is a reusable registry and discovery layer for APIs that need
to be usable by both HTTP clients and AI agents.

The distribution name is model-context-api. The Python import package is mca:

~~~python
from mca.ninja import NinjaMCARouter
~~~

MCA provides:

- one operation registry shared by discovery, HTTP, and MCP;
- Markdown guides that can be discovered and read on demand;
- Pydantic dispatch for framework-independent engines;
- Django Ninja registration with generated request and response schemas;
- an MCP host that exposes registered Django applications as Streamable HTTP
  tools.

## Why MCA exists

An ordinary API can be perfectly usable by a human developer and still be
difficult for an AI agent to use. The agent needs to answer several questions
before it can safely call an endpoint:

- What capabilities does this API provide?
- Which operation should handle the task?
- What does each input field mean?
- Which values belong in the URL, query string, or JSON body?
- What business rules or workflow constraints are not expressed by a type?
- What should the agent do after a successful or failed call?

MCA makes those answers available through one progressive contract. The
machine-readable part describes routes, parameters, request bodies, and
responses. The human-readable part explains concepts, policies, and workflows.
An agent can retrieve only the context it needs instead of receiving a large
undifferentiated prompt.

The normal interaction looks like this:

~~~text
agent
  │
  ├─ GET /                         discover the API and list capabilities
  ├─ GET /?guide=...               read the relevant domain instructions
  ├─ GET /?operation=...           read one operation's request/response schema
  └─ POST /items                   call the selected operation
       │
       └─ the same registry can also be reached through an MCP tool
~~~

MCA does not replace the application that owns the data or business logic. It
gives that application a consistent way to publish its capabilities and
context through HTTP, in-process dispatch, or MCP.

## Terminology

| Term | Meaning |
| --- | --- |
| Registry | The object that owns operations, routes, discovery, and guides for one API. |
| Operation | One callable capability, such as get_item or make_board. |
| Route | The HTTP method and path used to invoke an operation. |
| Schema | Machine-readable information describing valid inputs and outputs. |
| Guide | Markdown written by the API owner to explain concepts, rules, and workflows. |
| Discovery | The entry point that lists capabilities and tells clients how to request more context. |
| Adapter | The layer that connects the shared MCA registry to Pydantic or Django Ninja. |
| Transport | The way a client reaches the registry, such as direct HTTP or MCP. |
| MCP host | The ASGI bridge that exposes Django registries as MCP tools. |

The word registry refers to the application-facing object. For example,
mca_registry is a Django Ninja registry that can be discovered by MCPHost. The
word operation refers to a single capability inside that registry, not to the
registry itself.

## Installation

The base package includes Pydantic support:

~~~bash
python -m pip install model-context-api
~~~

Install the adapters that your application uses:

~~~bash
python -m pip install "model-context-api[ninja]"
python -m pip install "model-context-api[mcp]"
python -m pip install "model-context-api[ninja,mcp]"
~~~

The ninja extra installs Django and Django Ninja. The mcp extra installs the
MCP Python SDK. Applications using MCP normally install both extras.

The package requires Python 3.12 or newer.

## The MCA model

A registry is the API's published catalog. It connects each named operation
to its route, validation rules, response shape, description, and relevant
guides. The registry does not store application data and does not decide
whether a user is authenticated; the endpoint implementation and host
application still own those responsibilities.

Every registry includes a discovery operation named get_mca. With the default
mca_path="/", it is exposed as GET / relative to the API mount. Discovery is
deliberately separate from ordinary business operations so a client can learn
how to use an API before attempting a mutation.

Operation names determine their HTTP methods:

| Function prefix | HTTP method | Example operation |
| --- | --- | --- |
| get_ | GET | get_item |
| make_ | POST | make_item |
| set_ | PUT | set_item |
| update_ | PATCH | update_item |
| remove_ | DELETE | remove_item |

The default operation name is the decorated function name. register_all can
generate several operations from one function by applying these prefixes.

### Choosing an adapter

Choose the smallest adapter that matches where the operation lives:

| Adapter | Use it when | What it adds |
| --- | --- | --- |
| PydanticMCARouter | The operation is a typed Python function or engine. | In-process validation, dispatch, and schemas. |
| NinjaMCARouter | The operation is part of a Django Ninja REST API. | HTTP route registration and OpenAPI-derived schemas. |
| MCPHost | MCP clients need access to Django registries. | MCP tools and Streamable HTTP endpoints. |
| BaseMCARouter | You are implementing another transport. | Shared registration, guides, resolution, and discovery behavior. |

The adapters share the same operation and discovery concepts. A route
registered with NinjaMCARouter can therefore be called directly over HTTP,
internally through execute_http_request, or through MCPHost without creating
three separate implementations.

## Guides and discovery

### What a guide is

A guide is a Markdown document written for the person or agent using an API.
It explains information that types and route schemas cannot fully express:

- what the resource represents;
- which workflow the operations belong to;
- business rules and state transitions;
- safety requirements before destructive actions;
- examples of valid combinations of operations;
- when to choose one operation or guide over another.

For example, a board API might use these guides:

~~~text
index.md
    What the API is for and where an agent should begin.
api.md
    Route conventions, request shapes, identifiers, and error rules.
workflow.md
    Board states, card movement rules, terminal states, and history behavior.
~~~

The operation schema can say that a delete operation accepts a boolean
confirmed field. The guide can explain why deletion is destructive, when
confirmation is required, and what related history is removed. The schema is
for validation; the guide is for understanding and decision-making.

Guides are not executable code, are not a replacement for validation, and are
not automatically sent with every operation. Keeping them as separate
retrievable documents lets an agent first discover the API, then load only the
domain context needed for its current task.

### How discovery works

Discovery is the entry point for an unfamiliar client. A client should not
guess operation names or request shapes. It should progressively ask for:

1. the root document, which gives the API's purpose and available names;
2. the relevant guide, which gives domain and workflow context;
3. the selected operation schema, which gives exact input and output shape;
4. the operation route, which performs the requested work.

Guides are optional. Pass `guides_dir` to enable guide discovery and reading;
pass `guides_dir=None` (the default) for an API without guides. When guides are
disabled, guide-related fields are omitted from discovery responses and guide
requests return an `unknown_guides` error.

When guides are enabled, the guide directory should contain index.md:

~~~text
myapp/
├── api.py
└── guides/
    ├── index.md
    ├── items.md
    └── workflows.md
~~~

With guides enabled, the root discovery response returns the registry metadata,
index content, available guide names, and a map of available operations. Guide
and operation details are requested separately. Without guides, the response
contains the registry metadata, help text, and available operations only:

~~~http
GET /                         Root discovery
GET /?guide=items.md          Read one guide
GET /?guide=items.md,api.md   Read several guides
GET /?operation=get_item     Read one operation schema
~~~

A guide response maps each requested filename to its Markdown content.
Operations can advertise relevant guides without embedding those guides in
every response:

~~~python
@router.register("/items/{item_id}", guides=["items.md"])
def get_item(params: ItemParams) -> ItemOut:
    ...
~~~

Use include_in_discovery=False for a callable route that should not appear in
available_operations:

~~~python
@router.register("/internal/rebuild", include_in_discovery=False)
def make_rebuild() -> None:
    ...
~~~

### A complete agent session

Suppose an agent needs to create an item but has never seen this API. A
well-behaved client can follow this sequence:

1. Discover the API:

   ~~~http
   GET /api/items/
   ~~~

   The response says that the API manages items and lists make_item as a
   creation operation.

2. Read the relevant domain guide:

   ~~~http
   GET /api/items/?guide=items.md
   ~~~

   The guide might explain naming rules, required relationships, or when an
   item should be created instead of updated.

3. Read the exact operation schema:

   ~~~http
   GET /api/items/?operation=make_item
   ~~~

   The response identifies the POST route and describes the required body.

4. Invoke the operation:

   ~~~http
   POST /api/items/items
   Content-Type: application/json

   {"name": "Example", "description": "Created after discovery."}
   ~~~

The same sequence can use an MCP tool instead: call items_api with
GET /, then request the guide and schema, then call items_api with POST /items
and the JSON body. The business operation is still the same registered
operation.

## Framework-independent Pydantic APIs

Use PydanticMCARouter when the engine should be callable without Django or
another web framework.

The example below publishes three capabilities: reading an item, creating an
item, and reading API status. The decorator supplies an optional route and
description. If no
description is supplied, MCA uses the operation function's docstring. The
function name supplies the HTTP method and operation name. The Pydantic
annotations tell MCA which values are inputs and what a successful response
looks like.

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
    help="Use operation and guide discovery before calling an item route.",
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

- params is the typed path/query parameter object;
- data is the typed JSON body;
- the return annotation is the response schema;
- fields named in route placeholders such as {item_id} become path parameters;
- remaining params fields become query parameters.

A route such as /items/{item_id} with ItemParams produces this logical request
shape:

~~~json
{
  "path_params": {"item_id": 7},
  "query_params": {"verbose": true}
}
~~~

Dispatch by operation name or by an HTTP method and route path:

~~~python
# Root discovery.
discovery = router.dispatch("get_mca")

# Read guides and an operation schema.
guides = router.dispatch("get_mca", params={"guide": "items.md"})
schema = router.dispatch("get_mca", params={"operation": "get_item"})

# Dispatch a typed operation.
created = router.dispatch(
    "make_item",
    data={"name": "Example", "description": "Created by an agent."},
)

# Route paths resolve path placeholders and validate their values.
item = router.dispatch("/items/7", params={"verbose": True}, method="GET")
~~~

Inputs are validated before an endpoint is called, and results are validated
against return annotations. Invalid requests, unknown operations, unknown
routes, and endpoint failures are returned as ErrorOut values:

~~~python
result = router.dispatch("/items/not-an-integer", method="GET")
print(result.model_dump())
# {
#     "code": "invalid_request",
#     "detail": "...",
#     "field": "item_id",
# }
~~~

Register several methods when one implementation has the same input and output
shape:

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

This creates get_item and remove_item. Separate register decorators are clearer
when methods have different request or response models.

### Composing private Pydantic services

A public Pydantic router can mount private MCA services through a small client
adapter. The client can use JSON-RPC, HTTP, or another transport; it only needs
to provide `discover()` and `call()` methods:

~~~python
from mca.pydantic import PydanticMCARouter


class JsonRpcMCAClient:
    def __init__(self, rpc):
        self.rpc = rpc

    def discover(self, *, guide=None, operation=None):
        return self.call(
            "get_mca",
            params={"guide": guide, "operation": operation},
        )

    def call(self, operation, *, params=None, data=None):
        return self.rpc.call(
            "mca.dispatch",
            {"operation": operation, "params": params, "data": data},
        )


public_router = PydanticMCARouter(title="Public API")
public_router.mount("billing", JsonRpcMCAClient(billing_rpc))
~~~

Mounted operations are namespaced to keep the public catalog unambiguous. A
private `get_invoice` operation is discovered and called as
`billing.get_invoice`; a private `invoices.md` guide is requested as
`billing/invoices.md`. The public router owns its title, help text, and root
index while merging mounted operations and guides into discovery:

~~~python
discovery = public_router.dispatch("get_mca")
schema = public_router.dispatch(
    "get_mca",
    params={"operation": "billing.get_invoice"},
)
result = public_router.dispatch(
    "billing.get_invoice",
    params={"invoice_id": 42},
)
~~~

The private service remains unreachable directly by public clients. Its own
router continues to validate its inputs and outputs; the public router only
forwards the operation name, parameters, and body.

## Django Ninja APIs

Use NinjaMCARouter to register operations on either a NinjaAPI or a Django
Ninja Router. Options such as response, auth, tags, and other Django Ninja
route options are passed through to the corresponding registration method.

When a Router is supplied, its own auth and throttle configuration is used for
MCA schema discovery and internal execution. The Router can then be mounted on
a root NinjaAPI with `api.add_router(...)`; MCA does not require that root API
to generate operation schemas.

This adapter is for an existing HTTP API. It does not create a second business
logic layer: the decorated functions remain ordinary Django Ninja endpoints.
MCA adds the shared discovery operation, guide access, operation descriptions,
and a schema view assembled from Django Ninja's OpenAPI metadata. Router-backed
registries bind lazily to an internal NinjaAPI for this metadata and execution.

~~~python
# myapp/api.py
from pathlib import Path

from django.http import HttpRequest
from ninja import NinjaAPI, Path as NinjaPath, Query
from pydantic import BaseModel, Field

from mca.base import MCAError
from mca.ninja import NinjaMCARouter


class ItemIn(BaseModel):
    name: str = Field(..., min_length=1)
    description: str = ""


class ItemOut(BaseModel):
    item_id: int
    name: str
    description: str


api = NinjaAPI(title="Items API", version="1.0")

# Name this mca_registry when MCPHost should discover it automatically.
mca_registry = NinjaMCARouter(
    api,
    guides_dir=Path(__file__).with_name("guides"),
    title="Items API",
    version=1.0,
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

Routes registered with MCA are relative to the NinjaAPI mount. Both slash forms
are registered for non-root routes; the alternate form is hidden from the
generated schema and discovery output. Operation schemas are built from
Django Ninja's OpenAPI schema and describe path_params, query_params, and body.

This means a client can use the same API in two ways:

- a normal HTTP client follows the REST route and Django authentication rules;
- an agent first uses MCA discovery, reads the relevant context, and then calls
  the same REST operation with a validated request.

### Internal Ninja dispatch

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

execute_http_request creates a request with the registered method and route,
validates it through Django Ninja, and returns a Django HttpResponse. When
source_request is supplied, the authenticated user, cookies, session, and
relevant request metadata are copied.

For an already-created HttpRequest, use execute_http:

~~~python
response = mca_registry.execute_http(
    "get_item",
    request,
    path_params={"item_id": 7},
)
~~~

Both methods are synchronous and reject asynchronous Ninja endpoints.
allow_anonymous=True marks a generated request as explicitly trusted internal
traffic. Only use it at a trusted boundary, and never expose it as a
user-controlled HTTP option.

## MCP hosting

MCP is a tool protocol used by clients that cannot or should not call an
application's REST API directly. An MCP tool gives the client a structured
entry point, while MCA keeps the tool contract aligned with the underlying
HTTP routes and schemas.

MCPHost wraps a Django ASGI application and automatically exposes every
installed Django app that publishes a mca_registry from its api module. Use
direct HTTP when the client can reach the REST API and should use its normal
authentication. Use MCP when the client needs a tool-oriented connection or
only has access to the MCP endpoint.

~~~python
# project/asgi.py
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "project.settings")

from mca.mcp import MCPHost

application = MCPHost()
~~~

For an installed Django app whose label is items, the host provides:

~~~text
Streamable HTTP endpoint: /api/items/mcp
MCP tool name:           items_api
REST API base path:      /api/items
~~~

All other paths are passed to Django's normal ASGI application. The host
initializes Django, discovers registries, owns MCP session-manager lifespans,
and mounts stateless Streamable HTTP applications for each app.

Automatic discovery works by inspecting installed Django apps, importing each
app's api module, and looking for a NinjaMCARouter named mca_registry. The app
label determines both the endpoint path and the tool name. This convention is
why the registry must be defined in the app's api module with that exact name.

The tool accepts an API-relative HTTP-style route and an optional JSON body:

~~~text
items_api(route, body=None)
~~~

Start with discovery:

~~~text
items_api(route="GET /")
items_api(route="GET /?guide=items.md")
items_api(route="GET /?operation=get_item")
~~~

Then invoke operations relative to /api/items:

~~~text
items_api(route="GET /items/7")
items_api(
    route="POST /items",
    body={"name": "Created through MCP", "description": "Example"},
)
items_api(route="DELETE /items/7")
~~~

Do not include the REST or MCP prefix:

~~~text
Correct:   GET /items/7
Incorrect: GET /api/items/items/7
Incorrect: GET /api/items/mcp/items/7
~~~

The route parser accepts a method and relative path with or without a leading
slash, parses query strings, preserves repeated query parameters, accepts JSON
bodies only for POST, PUT, and PATCH, and resolves path parameters against the
registered routes.

Successful results are returned as JSON text. A 204 response is represented as
null. Invalid routes, validation failures, unknown operations, and endpoint
errors are returned as MCP tool errors containing the MCA error code, detail,
field, and HTTP status.

### Building one MCP server manually

For applications that do not want automatic Django app discovery:

~~~python
from mca.mcp import MCPHost
from myapp.api import mca_registry

host = MCPHost()
server = host.build_server(mca_registry, "items")
application = server.streamable_http_app(
    streamable_http_path="/",
    stateless_http=True,
)
~~~

The resulting server exposes the items_api tool. The surrounding ASGI
application is responsible for starting the server's session manager and for
providing Django settings.

## Errors and authentication

An MCA error is a normal part of the published contract, not an implementation
detail. Clients need a stable code to decide whether to retry, ask for a
missing value, choose another operation, or report a domain failure.

Use MCAError for stable machine-readable errors:

~~~python
from mca.base import MCAError

raise MCAError(
    "item_not_found",
    "The requested item does not exist.",
    field="item_id",
    status=404,
)
~~~

The error has code, detail, an optional field, and an HTTP status. The Pydantic
adapter converts MCA errors and validation failures into ErrorOut values. The
Django Ninja adapter raises the error through the normal request path, so
register an application exception handler when the API needs a consistent JSON
shape. MCP converts the resulting HTTP error into an MCP tool error.

MCA does not impose an authentication policy. Normal Ninja requests use the
authentication configured on the NinjaAPI or route. Internal execution and MCP
hosting can explicitly opt into an application's trusted anonymous mode, but
that decision belongs to the application boundary.

## Public modules

~~~text
mca.base
    BaseMCARouter, GuideCatalog, MCAError, RegisteredRoute
mca.models
    ErrorOut, MCAResponseOut, APIRouteSchemaOut,
    MCADiscoveryOut, DiscoveryParams
mca.pydantic
    PydanticMCARouter
mca.ninja
    NinjaMCARouter, MCAExecutionError
mca.mcp
    MCPHost, MCPRoute
~~~

Use BaseMCARouter when implementing another transport adapter. A custom adapter
supplies discovery endpoints, transport registration, dispatch, and error
conversion while reusing route registration, method mapping, guide catalogs,
route resolution, and discovery behavior from the base class.
