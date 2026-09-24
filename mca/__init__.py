"""Reusable Model Context API routers and MCP hosting utilities."""

from .base import BaseMCARouter, GuideCatalog, MCAError
from .composition import MCAClient

__version__ = "0.1.2"

__all__ = ["BaseMCARouter", "GuideCatalog", "MCAClient", "MCAError", "__version__"]
