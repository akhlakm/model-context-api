"""ASGI entry point for the MCA composition example."""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "demo.settings")

from mca.mcp import mcp_host

mcp_host.configure(
    mount_path="/mcp",
    description_prefix=(
        "This tool accesses the demo public invoice API. "
        "Use it to discover and manage demo invoices."
    ),
)
application = mcp_host
