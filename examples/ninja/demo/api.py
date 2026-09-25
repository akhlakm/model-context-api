"""Public Ninja API that composes the private billing MCA explicitly."""

import logging
from pathlib import Path
from typing import Any

from django.http import HttpRequest
from ninja import Body
from ninja import Path as NinjaPath
from ninja import Router
from ninja.errors import HttpError

from mca.mcp import mcp_host
from mca.ninja import NinjaMCARouter

from .rpc import billing_rpc

LOGGER = logging.getLogger(__name__)
EXAMPLE_ROOT = Path(__file__).resolve().parent.parent

public_router = Router()
public_mca = NinjaMCARouter(
    public_router,
    guides_dir=EXAMPLE_ROOT / "guides",
    title="Public Invoice API",
    usage="Discover public operations and guides before requesting an invoice.",
)
public_mca.mount("billing", billing_rpc)
mcp_host.register(
    public_mca,
    "demo",
    api_base_path="/api",
    description="Public invoice and billing API.",
)


def demo_auth(request: HttpRequest) -> dict[str, Any] | None:
    """Authenticate the demo caller from a request header."""
    principals = {
        "demo-token": {
            "subject": "demo-user",
            "scopes": {"billing:read", "billing:write", "billing:delete"},
            "invoice_ids": {7},
        },
        "limited-token": {
            "subject": "limited-user",
            "scopes": {"billing:read"},
            "invoice_ids": set(),
        },
    }
    return principals.get(request.headers.get("X-Demo-Token"))


def require_access(
    request: HttpRequest,
    scope: str,
    invoice_id: int | None = None,
) -> None:
    """Apply the example's scope and resource-level access checks."""
    if scope not in request.auth["scopes"]:
        raise HttpError(403, f"The authenticated caller lacks the {scope} scope.")
    if invoice_id is not None and invoice_id not in request.auth["invoice_ids"]:
        raise HttpError(403, "The authenticated caller cannot access this invoice.")


@public_mca.register(
    "/invoices/{invoice_id}",
    operation_id="get_public_invoice",
    response=dict[str, Any],
    auth=demo_auth,
    guides=["api.md"],
    delegate_to="billing.get_invoice",
)
def get_public_invoice(
    request: HttpRequest,
    invoice_id: int = NinjaPath(..., description="Unique identifier of the invoice."),
) -> dict[str, Any]:
    """Authenticate, authorize, track, and then delegate invoice access."""
    require_access(request, "billing:read", invoice_id)

    LOGGER.info(
        "invoice_access subject=%s invoice_id=%s",
        request.auth["subject"],
        invoice_id,
    )
    result = billing_rpc.call(
        "get_invoice",
        params={"invoice_id": invoice_id},
    )
    return result


@public_mca.register(
    "/invoices",
    operation_id="make_public_invoice",
    response=dict[str, Any],
    auth=demo_auth,
    guides=["api.md"],
    delegate_to="billing.make_invoice",
)
def make_public_invoice(
    request: HttpRequest,
    payload: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    """Authorize and delegate invoice creation."""
    require_access(request, "billing:write")
    LOGGER.info("invoice_create subject=%s", request.auth["subject"])
    return billing_rpc.call("make_invoice", data=payload)


@public_mca.register(
    "/invoices/{invoice_id}",
    operation_id="update_public_invoice",
    response=dict[str, Any],
    auth=demo_auth,
    guides=["api.md"],
    delegate_to="billing.update_invoice",
)
def update_public_invoice(
    request: HttpRequest,
    invoice_id: int = NinjaPath(..., description="Unique identifier of the invoice."),
    payload: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    """Authorize and delegate invoice updates."""
    require_access(request, "billing:write", invoice_id)
    LOGGER.info(
        "invoice_update subject=%s invoice_id=%s",
        request.auth["subject"],
        invoice_id,
    )
    result = billing_rpc.call(
        "update_invoice",
        params={"invoice_id": invoice_id},
        data=payload,
    )
    return result


@public_mca.register(
    "/invoices/{invoice_id}",
    operation_id="remove_public_invoice",
    response=dict[str, Any],
    auth=demo_auth,
    guides=["api.md"],
    delegate_to="billing.remove_invoice",
)
def remove_public_invoice(
    request: HttpRequest,
    invoice_id: int = NinjaPath(..., description="Unique identifier of the invoice."),
) -> dict[str, Any]:
    """Authorize and delegate invoice deletion."""
    require_access(request, "billing:delete", invoice_id)
    LOGGER.info(
        "invoice_delete subject=%s invoice_id=%s",
        request.auth["subject"],
        invoice_id,
    )
    result = billing_rpc.call(
        "remove_invoice",
        params={"invoice_id": invoice_id},
    )
    return result
