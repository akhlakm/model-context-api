"""Interfaces for composing a public MCA router with private MCA services."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Protocol

from pydantic import ValidationError

from .base import MCAError, RegisteredRoute
from .models import APIRouteSchemaOut, MCADiscoveryOut, MCAResponseOut


class MCAClient(Protocol):
    """Client boundary used by a router to reach a mounted MCA service."""

    def discover(
        self,
        *,
        guide: str | None = None,
        operation: str | None = None,
    ) -> Any:
        """Return a remote get_mca response as a model or JSON-like value."""

    def call(
        self,
        operation: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Any = None,
    ) -> Any:
        """Call a remote operation and return its JSON-compatible result."""


class MCACompositionMixin:
    """Shared explicit-composition behavior for MCA adapters."""

    _namespace_pattern = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")

    def __init__(self, *args: Any, **kwargs: Any):
        self._mounted_mcas: dict[str, MCAClient] = {}
        super().__init__(*args, **kwargs)

    def mount(self, namespace: str, client: MCAClient) -> None:
        """Make a private MCA available for explicitly delegated routes."""
        if not isinstance(namespace, str) or self._namespace_pattern.fullmatch(namespace) is None:
            raise ValueError(
                "MCA mount namespace must start with a letter and contain only letters, numbers, '-' or '_'."
            )
        if namespace in self._mounted_mcas:
            raise ValueError(f"MCA namespace {namespace!r} is already mounted.")
        if any(
            route.operation == namespace or route.operation.startswith(f"{namespace}.")
            for route in self._routes.values()
        ):
            raise ValueError(f"MCA namespace {namespace!r} conflicts with a public operation.")
        if not callable(getattr(client, "discover", None)) or not callable(getattr(client, "call", None)):
            raise TypeError("MCA client must provide discover() and call() methods.")
        self._mounted_mcas[namespace] = client

    def _validate_delegate_option(self, options: Mapping[str, Any]) -> None:
        target = options.get("delegate_to")
        if target is not None:
            self._delegate_target(target)

    def _delegate_target(self, target: Any) -> tuple[str, str]:
        if not isinstance(target, str):
            raise ValueError("delegate_to must use the form 'namespace.operation'.")
        namespace, separator, operation = target.partition(".")
        if not separator or not namespace or not operation:
            raise ValueError("delegate_to must use the form 'namespace.operation'.")
        if namespace not in self._mounted_mcas:
            raise ValueError(f"MCA namespace {namespace!r} is not mounted.")
        return namespace, operation

    def _route_metadata(self, endpoint: Any, options: Mapping[str, Any]) -> Mapping[str, Any]:
        self._validate_delegate_option(options)
        return super()._route_metadata(endpoint, options)

    def _validate_route_registration(
        self,
        path: str | None,
        method: str,
        operation: str,
    ) -> None:
        super()._validate_route_registration(path, method, operation)
        if any(
            operation == namespace or operation.startswith(f"{namespace}.")
            for namespace in self._mounted_mcas
        ):
            raise ValueError(f"MCA operation {operation!r} conflicts with a mounted namespace.")

    def _delegated_routes(self) -> tuple[RegisteredRoute, ...]:
        return tuple(
            route
            for route in self._routes.values()
            if route.operation != "get_mca"
            and route.meta("include_in_discovery", True)
            and route.meta("delegate_to") is not None
        )

    def _remote_discovery(
        self,
        namespace: str,
        *,
        guide: str | None = None,
        operation: str | None = None,
    ) -> dict[str, Any]:
        client = self._mounted_mcas[namespace]
        try:
            value = client.discover(guide=guide, operation=operation)
            model = MCAResponseOut if guide is None and operation is None else MCADiscoveryOut
            return model.model_validate(value).model_dump()
        except MCAError:
            raise
        except ValidationError as exc:
            raise MCAError(
                "invalid_upstream_response",
                f"Mounted MCA service '{namespace}' returned an invalid discovery response.",
                "service",
                502,
            ) from exc
        except Exception as exc:
            raise self._remote_error(namespace, "return discovery data", exc) from exc

    @staticmethod
    def _remote_error(namespace: str, action: str, exc: Exception) -> MCAError:
        return MCAError(
            "upstream_unavailable",
            f"Mounted MCA service '{namespace}' could not {action}.",
            "service",
            502,
        )

    def _remote_operation_schema(
        self,
        namespace: str,
        operation: str,
    ) -> APIRouteSchemaOut:
        result = self._remote_discovery(namespace, operation=operation)
        operations = result.get("operations") or {}
        if operation not in operations:
            raise MCAError(
                "unknown_operation",
                f"Mounted MCA service '{namespace}' has no operation '{operation}'.",
                "operation",
                404,
            )
        try:
            return APIRouteSchemaOut.model_validate(operations[operation])
        except ValidationError as exc:
            raise MCAError(
                "invalid_upstream_response",
                f"Mounted MCA service '{namespace}' returned an invalid operation schema.",
                "service",
                502,
            ) from exc

    def _route_guides(self, route: RegisteredRoute) -> list[str] | None:
        guides = list(route.meta("guides") or [])
        target = route.meta("delegate_to")
        if target is not None:
            namespace, operation = self._delegate_target(target)
            remote_schema = self._remote_operation_schema(namespace, operation)
            guides.extend(
                f"{namespace}/{name}"
                for name in remote_schema.guides or []
            )
        return list(dict.fromkeys(guides)) or None

    def _exposed_remote_guides(self) -> set[str]:
        guides: set[str] = set()
        for route in self._delegated_routes():
            route_guides = self._route_guides(route) or []
            guides.update(
                guide
                for guide in route_guides
                if "/" in guide and guide.partition("/")[0] in self._mounted_mcas
            )
        return guides

    def _split_remote_guides(
        self,
        value: str | None,
    ) -> tuple[list[str], dict[str, list[str]]]:
        names = [] if value is None else [item.strip() for item in value.split(",")]
        remote_names = {
            name
            for name in names
            if "/" in name and name.partition("/")[0] in self._mounted_mcas
        }
        exposed = self._exposed_remote_guides() if remote_names else set()
        local_names: list[str] = []
        mounted_names: dict[str, list[str]] = {}
        for name in names:
            namespace, separator, remote_name = name.partition("/")
            if separator and namespace in self._mounted_mcas:
                if name not in exposed:
                    raise MCAError(
                        "unknown_guides",
                        f"Unavailable guide(s): {name}.",
                        "guide",
                        404,
                    )
                mounted_names.setdefault(namespace, []).append(remote_name)
            else:
                local_names.append(name)
        return local_names, mounted_names
