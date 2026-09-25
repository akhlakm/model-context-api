"""ASGI entry point for the MCA composition example."""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "demo.settings")

from mca.mcp import MCPHost

application = MCPHost()
