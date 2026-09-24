"""Private billing operations implemented with the Pydantic MCA router."""

from pathlib import Path

from pydantic import BaseModel, Field

from mca.pydantic import PydanticMCARouter

EXAMPLE_ROOT = Path(__file__).resolve().parent.parent


class InvoiceParams(BaseModel):
    """Path parameters used to identify an invoice."""

    invoice_id: int = Field(..., description="Unique identifier of the invoice.")


class InvoiceCreate(BaseModel):
    """Fields required to create a new invoice."""

    customer: str = Field(..., description="Name of the customer billed by the invoice.")
    total: float = Field(..., description="Total amount of the invoice in the example currency.")


class InvoiceUpdate(BaseModel):
    """Optional fields that can be changed on an existing invoice."""

    customer: str | None = Field(
        None,
        description="Replacement customer name, when changing the billed customer.",
    )
    total: float | None = Field(
        None,
        description="Replacement total amount, when changing the invoice amount.",
    )
    status: str | None = Field(
        None,
        description="Replacement invoice status, when changing its state.",
    )


class InvoiceOut(BaseModel):
    """Invoice returned by the billing service."""

    invoice_id: int = Field(..., description="Unique identifier of the invoice.")
    customer: str = Field(..., description="Name of the customer billed by the invoice.")
    total: float = Field(..., description="Total amount of the invoice in the example currency.")
    status: str = Field(..., description="Current lifecycle status of the invoice.")


class InvoiceDeletedOut(BaseModel):
    """Result returned after an invoice is deleted."""

    invoice_id: int = Field(..., description="Unique identifier of the deleted invoice.")
    status: str = Field(..., description="Final status assigned after deletion.")


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
