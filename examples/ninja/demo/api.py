"""Public Ninja API that composes the private billing MCA explicitly."""

import logging
from pathlib import Path
from typing import Any

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from mca.ninja import NinjaMCARouter

from .private import InvoiceOut
from .rpc import billing_rpc

LOGGER = logging.getLogger(__name__)
EXAMPLE_ROOT = Path(__file__).resolve().parent.parent

public_router = Router()
public_mca = NinjaMCARouter(
    public_router,
    guides_dir=EXAMPLE_ROOT / "guides",
    title="Public Invoice API",
    help="Discover public operations and guides before requesting an invoice.",
)
public_mca.mount("billing", billing_rpc)


def demo_auth(request: HttpRequest) -> dict[str, Any] | None:
    """Authenticate the demo caller from a request header."""
    principals = {
        "demo-token": {
            "subject": "demo-user",
            "scopes": {"billing:read"},
            "invoice_ids": {7},
        },
        "limited-token": {
            "subject": "limited-user",
            "scopes": {"billing:read"},
            "invoice_ids": set(),
        },
    }
    return principals.get(request.headers.get("X-Demo-Token"))


@public_mca.register(
    "/invoices/{invoice_id}",
    operation_id="get_public_invoice",
    response=InvoiceOut,
    auth=demo_auth,
    guides=["api.md"],
    delegate_to="billing.get_invoice",
)
def get_public_invoice(request: HttpRequest, invoice_id: int) -> InvoiceOut:
    """Authenticate, authorize, track, and then delegate invoice access."""
    if invoice_id not in request.auth["invoice_ids"]:
        raise HttpError(403, "The authenticated caller cannot access this invoice.")

    LOGGER.info(
        "invoice_access subject=%s invoice_id=%s",
        request.auth["subject"],
        invoice_id,
    )
    result = billing_rpc.call(
        "get_invoice",
        params={"invoice_id": invoice_id},
    )
    return InvoiceOut.model_validate(result)
