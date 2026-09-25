# API reference

The public modules are:

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
    MCPHost, MCPRegistration, mcp_host
~~~

Use `BaseMCARouter` when implementing another transport adapter. A custom
adapter supplies discovery endpoints, transport registration, dispatch, and
error conversion while reusing route registration, method mapping, guide
catalogs, route resolution, and discovery behavior from the base class.

For detailed usage, see [Pydantic APIs](pydantic.md), [Django Ninja
APIs](ninja.md), and [MCP hosting](mcp.md).
