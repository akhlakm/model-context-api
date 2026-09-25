# Model Context API

model-context-api is a reusable registry and discovery layer for APIs that
need to be usable by both HTTP clients and AI agents.

The distribution name is `model-context-api`. The Python import package is
`mca`:

~~~python
from mca.ninja import NinjaMCARouter
~~~

MCA provides:

- one operation registry shared by discovery, HTTP, and MCP;
- Markdown guides that can be discovered and read on demand;
- Pydantic dispatch for framework-independent engines;
- Django Ninja registration with generated request and response schemas;
- one MCP host and `mc_api` tool for registered Django applications.

## Why MCA exists

An ordinary API can be perfectly usable by a human developer and still be
difficult for an AI agent to use. MCA publishes both a machine-readable
contract—routes, parameters, request bodies, and responses—and human-readable
context about concepts, policies, and workflows.

An agent can progressively discover an API, read only the relevant guide,
inspect one operation's schema, and then invoke the selected route:

~~~text
agent
  │
  ├─ GET /                         discover the API and list capabilities
  ├─ GET /?guide=...               read relevant domain instructions
  ├─ GET /?operation=...           read one operation's schema
  └─ POST /items                   call the selected operation
~~~

MCA does not replace the application that owns the data or business logic. It
gives that application a consistent way to publish its capabilities through
HTTP, in-process dispatch, or MCP.

## Terminology

| Term | Meaning |
| --- | --- |
| Registry | The object that owns operations, routes, discovery, and guides for one API. |
| Operation | One callable capability, such as `get_item` or `make_board`. |
| Route | The HTTP method and path used to invoke an operation. |
| Schema | Machine-readable information describing valid inputs and outputs. |
| Guide | Markdown written by the API owner to explain concepts, rules, and workflows. |
| Discovery | The entry point that lists capabilities and tells clients how to request more context. |
| Adapter | The layer that connects the shared MCA registry to Pydantic or Django Ninja. |
| Transport | The way a client reaches the registry, such as direct HTTP or MCP. |
| MCP host | The ASGI bridge that exposes registered Django registries as one MCP tool. |

Every registry includes a discovery operation named `get_context`. With the
default `mca_path="/"`, it is exposed as `GET /` relative to the API mount.
When guides are enabled, the root discovery response returns the contents of
`index.md` as `help`; the router's instructions are returned as `usage`.

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

The `ninja` extra installs Django and Django Ninja. The `mcp` extra installs
the MCP Python SDK. Applications using MCP normally install both extras. The
package requires Python 3.12 or newer.

## The MCA model

A registry is the API's published catalog. It connects each named operation to
its route, validation rules, response shape, description, and relevant guides.
The registry does not store application data and does not decide whether a
user is authenticated; the endpoint implementation and host application own
those responsibilities.

Operation names determine their HTTP methods:

| Function prefix | HTTP method | Example operation |
| --- | --- | --- |
| `get_` | GET | `get_item` |
| `make_` | POST | `make_item` |
| `set_` | PUT | `set_item` |
| `update_` | PATCH | `update_item` |
| `remove_` | DELETE | `remove_item` |

Choose the smallest adapter that matches where the operation lives:

| Adapter | Use it when |
| --- | --- |
| `PydanticMCARouter` | The operation is a typed Python function or engine. |
| `NinjaMCARouter` | The operation is part of a Django Ninja REST API. |
| `MCPHost` | MCP clients need access to registered Django registries. |
| `BaseMCARouter` | You are implementing another transport. |

## Documentation

The detailed guides are organized by topic:

- [Documentation index](docs/index.md)
- [Guides and discovery](docs/discovery.md)
- [Pydantic APIs](docs/pydantic.md)
- [Django Ninja APIs](docs/ninja.md)
- [MCP hosting](docs/mcp.md)
- [Errors and authentication](docs/errors-and-authentication.md)
- [API reference](docs/api-reference.md)

The [Ninja composition example](examples/ninja/README.md) demonstrates a
public Django Ninja router, a private Pydantic service, and the shared MCP
endpoint.
