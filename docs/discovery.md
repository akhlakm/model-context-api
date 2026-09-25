# Guides and discovery

MCA exposes context progressively. A client can discover the API, read the
domain guidance relevant to its task, inspect one operation's schema, and then
call the operation.

## What a guide is

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
`confirmed` field. The guide can explain why deletion is destructive, when
confirmation is required, and what related history is removed. The schema is
for validation; the guide is for understanding and decision-making.

Guides are not executable code, are not a replacement for validation, and are
not automatically sent with every operation. Keeping them as separate
retrievable documents lets an agent first discover the API, then load only the
domain context needed for its current task.

## How discovery works

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

When guides are enabled, an optional `index.md` file provides the root guide
description:

~~~text
myapp/
├── api.py
└── guides/
    ├── index.md                 (optional)
    ├── items.md
    ├── invoices/
    │   └── legacy_format.md
    └── workflows.md
~~~

The contents of `index.md` are returned as `help` in the root discovery
response. The router's configured instructions are returned as `usage`.
Because the index content is already present in root discovery, `index.md` is
omitted from `available_guides`. It remains readable with
`?guide=index.md` for clients that explicitly request it.

Guide names are paths relative to `guides_dir`, using `/` separators. Nested
guides can be requested with names such as `invoices/legacy_format.md`.

With guides enabled, the root discovery response returns the registry metadata,
available guide names, and a map of available operations. Guide and operation
details are requested separately. Without guides, the response contains the
registry metadata, usage instructions, and available operations only:

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
def get_item(params: ItemParams):
    ...
~~~

Use `include_in_discovery=False` for a callable route that should not appear in
`available_operations`:

~~~python
@router.register("/internal/rebuild", include_in_discovery=False)
def make_rebuild() -> None:
    ...
~~~

## A complete agent session

Suppose an agent needs to create an item but has never seen this API. A
well-behaved client can follow this sequence:

1. Discover the API:

   ~~~http
   GET /api/items/
   ~~~

   The response says that the API manages items and lists `make_item` as a
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

The same sequence can use the shared `mc_api` tool instead: call it with
`GET /api/items`, then request the guide and schema, then call it with
`POST /api/items/items` and the JSON body. The business operation is still the
same registered operation.
