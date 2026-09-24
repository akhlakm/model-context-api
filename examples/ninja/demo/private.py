"""Private billing operations implemented with the Pydantic MCA router."""

from pathlib import Path

from pydantic import BaseModel

from mca.pydantic import PydanticMCARouter

EXAMPLE_ROOT = Path(__file__).resolve().parent.parent


class InvoiceParams(BaseModel):
    invoice_id: int


class InvoiceCreate(BaseModel):
    customer: str
    total: float


class InvoiceUpdate(BaseModel):
    customer: str | None = None
    total: float | None = None
    status: str | None = None


class InvoiceOut(BaseModel):
    invoice_id: int
    customer: str
    total: float
    status: str


class InvoiceDeletedOut(BaseModel):
    invoice_id: int
    status: str


private_router = PydanticMCARouter(
    guides_dir=EXAMPLE_ROOT / "private_guides",
    title="Private Billing Service",
    help="Use operation discovery before calling private billing operations.",
)


@private_router.register(
    "/private/invoices/{invoice_id}",
    guides=["invoices.md", "invoices/legacy_format.md"],
)
def get_invoice(params: InvoiceParams) -> InvoiceOut:
    """Read an invoice from the private billing service."""
    return InvoiceOut(
        invoice_id=params.invoice_id,
        customer="Example Customer",
        total=125.50,
        status="open",
    )


@private_router.register(
    "/private/invoices",
    guides=["invoices.md"],
)
def make_invoice(data: InvoiceCreate) -> InvoiceOut:
    """Create an invoice in the private billing service."""
    return InvoiceOut(
        invoice_id=8,
        customer=data.customer,
        total=data.total,
        status="open",
    )


@private_router.register(
    "/private/invoices/{invoice_id}",
    guides=["invoices.md"],
)
def update_invoice(params: InvoiceParams, data: InvoiceUpdate) -> InvoiceOut:
    """Update an invoice in the private billing service."""
    return InvoiceOut(
        invoice_id=params.invoice_id,
        customer=data.customer or "Example Customer",
        total=data.total if data.total is not None else 125.50,
        status=data.status or "open",
    )


@private_router.register(
    "/private/invoices/{invoice_id}",
    guides=["invoices.md"],
)
def remove_invoice(params: InvoiceParams) -> InvoiceDeletedOut:
    """Delete an invoice from the private billing service."""
    return InvoiceDeletedOut(invoice_id=params.invoice_id, status="deleted")
