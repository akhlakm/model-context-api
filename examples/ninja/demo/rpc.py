"""In-process JSON-RPC stand-in for the private billing service."""

from typing import Any

from pydantic import BaseModel

from .private import private_router


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


class MockJsonRpcClient:
    """Call the private router as if it were behind a JSON-RPC transport."""

    def __init__(self, router: Any):
        self.router = router
        self.calls: list[dict[str, Any]] = []

    def discover(
        self,
        *,
        guide: str | None = None,
        operation: str | None = None,
    ) -> Any:
        return self._discover(guide=guide, operation=operation)

    async def adiscover(
        self,
        *,
        guide: str | None = None,
        operation: str | None = None,
    ) -> Any:
        """Asynchronously discover the private router through mock RPC."""
        self.calls.append({"method": "get_context", "guide": guide, "operation": operation})
        return _json_value(
            await self.router.adispatch(
                "get_context",
                params={"guide": guide, "operation": operation},
            )
        )

    def _discover(
        self,
        *,
        guide: str | None,
        operation: str | None,
    ) -> Any:
        self.calls.append({"method": "get_context", "guide": guide, "operation": operation})
        return _json_value(
            self.router.dispatch(
                "get_context",
                params={"guide": guide, "operation": operation},
            )
        )

    def call(
        self,
        operation: str,
        *,
        params: dict[str, Any] | None = None,
        data: Any = None,
    ) -> Any:
        return self._call(operation, params=params, data=data)

    async def acall(
        self,
        operation: str,
        *,
        params: dict[str, Any] | None = None,
        data: Any = None,
    ) -> Any:
        """Asynchronously call a private operation through mock RPC."""
        return self._call(operation, params=params, data=data)

    def _call(
        self,
        operation: str,
        *,
        params: dict[str, Any] | None,
        data: Any,
    ) -> Any:
        self.calls.append(
            {"method": operation, "params": params, "data": data}
        )
        return _json_value(
            self.router.dispatch(
                operation,
                params=params,
                data=data,
            )
        )


# This is the only object in the example that talks to the private router.
billing_rpc = MockJsonRpcClient(private_router)
