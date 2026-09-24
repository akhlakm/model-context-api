# Public API Rules

Invoice requests require the `X-Demo-Token` header. The public API performs
authentication and access control before delegating to the private billing
service.
