"""ASGI entry point for the MCA composition example."""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "demo.settings")

from mca.mcp import mcp_host

application = mcp_host
