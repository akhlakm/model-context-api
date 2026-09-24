"""Interfaces for composing a public MCA router with private MCA services."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol


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
