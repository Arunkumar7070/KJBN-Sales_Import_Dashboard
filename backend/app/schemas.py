"""Pydantic response models for the API (they drive the Swagger / OpenAPI docs).

Money is always integer paise in the database. Responses expose it as a Money
object with both paise and rupees; rupees is derived from paise, so the two can
never disagree, and it is a Decimal serialized as an exact string ("99.99").
Response models read straight from SQLAlchemy objects (from_attributes=True).
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    computed_field,
)

# Same values as the CHECK constraints in app/models.py.
BatchStatus = Literal["QUEUED", "PROCESSING", "COMPLETED", "REJECTED_HEADER", "FAILED"]
PaymentMode = Literal["CASH", "UPI", "CARD"]

PAISE_PER_RUPEE = 100


def as_utc(value: datetime) -> datetime:
    """Express a database timestamp in UTC, so every response uses one offset.

    PostgreSQL returns timestamptz in the connection's time zone (which varies
    by server), and some drivers (e.g. SQLite) return it with no time zone.
    """
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


UtcDatetime = Annotated[datetime, AfterValidator(as_utc)]


class Money(BaseModel):
    """An amount of money, in paise and in rupees."""

    model_config = ConfigDict(frozen=True)

    paise: int = Field(
        description="Exact amount in paise (1 rupee = 100 paise). Negative for returns.",
        examples=[9999],
    )

    @computed_field(
        description="The same amount in rupees, with exactly 2 decimal places.",
        examples=["99.99"],
    )
    @property
    def rupees(self) -> Decimal:
        return (Decimal(self.paise) / PAISE_PER_RUPEE).quantize(Decimal("0.01"))


def money_from_paise(value: object) -> object:
    # Lets an integer paise column (e.g. Transaction.unit_price_paise) fill a Money field.
    return Money(paise=value) if isinstance(value, int) else value


PaiseAmount = Annotated[Money, BeforeValidator(money_from_paise)]


class StoreResponse(BaseModel):
    """A store, for filters such as the store dropdown."""

    model_config = ConfigDict(from_attributes=True)

    code: str = Field(
        description="Store code used in CSVs and filters.", examples=["ST01"]
    )
    name: str = Field(description="Store display name.", examples=["Anna Nagar"])
    city: str = Field(examples=["Chennai"])


class BatchResponse(BaseModel):
    """One uploaded CSV file and the outcome of importing it."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Batch ID.")
    filename: str = Field(
        description="Original file name.", examples=["sales_2026-10-17.csv"]
    )
    status: BatchStatus = Field(description="Where the import is in its lifecycle.")
    uploaded_at: UtcDatetime = Field(description="When the file was uploaded (UTC).")
    started_at: UtcDatetime | None = Field(
        description="When processing last started (UTC)."
    )
    completed_at: UtcDatetime | None = Field(
        description="When processing finished (UTC)."
    )
    rows_total: int = Field(ge=0, description="Data rows in the file.", examples=[120])
    rows_accepted: int = Field(
        ge=0, description="Rows saved as transactions.", examples=[117]
    )
    rows_rejected: int = Field(
        ge=0, description="Rows rejected with an error.", examples=[3]
    )
    attempt_count: int = Field(
        ge=0, description="Processing attempts so far.", examples=[1]
    )
    failure_reason: str | None = Field(
        description="Why the whole file failed or was rejected; null otherwise."
    )

    @computed_field(
        description="Download link for the rejected rows as CSV; null if none were rejected.",
        examples=["/api/v1/imports/3f2c9a7e-0b1d-4c55-9a0e-2d1f6b7c8e90/errors.csv"],
    )
    @property
    def error_report_url(self) -> str | None:
        if self.rows_rejected == 0:
            return None
        return f"/api/v1/imports/{self.id}/errors.csv"


class BatchListResponse(BaseModel):
    """One page of batches, newest upload first."""

    items: list[BatchResponse]
    total: int = Field(
        ge=0, description="Number of batches across all pages.", examples=[57]
    )
    limit: int = Field(description="Page size used for this response.", examples=[20])
    offset: int = Field(description="Batches skipped before this page.", examples=[0])


class TransactionResponse(BaseModel):
    """One imported sale (positive quantity) or return (negative quantity)."""

    model_config = ConfigDict(from_attributes=True)

    transaction_id: str = Field(
        description="Transaction ID from the CSV.", examples=["T1001"]
    )
    store_code: str = Field(description="Store that made the sale.", examples=["ST01"])
    sold_at: UtcDatetime = Field(
        description="Sale time in UTC (the CSV gives IST).",
        examples=["2026-10-17T03:30:00Z"],
    )
    sku: str = Field(description="Product SKU.", examples=["SKU1"])
    product_name: str = Field(
        description="Product name as written in the CSV row.", examples=["Masala Chai"]
    )
    quantity: int = Field(
        description="Units: positive for a sale, negative for a return.", examples=[2]
    )
    unit_price: PaiseAmount = Field(
        validation_alias="unit_price_paise", description="Price of one unit."
    )
    discount: PaiseAmount = Field(
        validation_alias="discount_paise",
        description="Discount on the line (always 0 for returns).",
    )
    line_total: PaiseAmount = Field(
        validation_alias="line_total_paise",
        description="quantity × unit_price − discount (negative for returns).",
    )
    payment_mode: PaymentMode = Field(description="How the customer paid.")
    is_return: bool = Field(description="True when quantity is negative.")
    original_transaction_id: str | None = Field(
        description="For a return, the sale it refunds; null for a sale.",
        examples=[None],
    )
    batch_id: uuid.UUID = Field(description="Batch (uploaded file) this row came from.")
    created_at: UtcDatetime = Field(description="When the row was saved (UTC).")


class TransactionListResponse(BaseModel):
    """One page of transactions matching the filters, newest sale first."""

    items: list[TransactionResponse]
    total: int = Field(
        ge=0,
        description="Transactions matching the filters, across all pages.",
        examples=[245],
    )
    page: int = Field(
        ge=1, description="Page number of this response (1-based).", examples=[1]
    )
    limit: int = Field(
        ge=1, description="Page size used for this response.", examples=[20]
    )

    @computed_field(
        description="Number of pages for these filters and this page size.",
        examples=[13],
    )
    @property
    def pages(self) -> int:
        return -(-self.total // self.limit)  # Ceiling division.


# Dashboard analytics


class DailyRevenue(BaseModel):
    """Net revenue for one day."""

    day: date = Field(description="Calendar day (IST).", examples=["2026-10-17"])
    revenue: Money = Field(
        description="Sum of line totals that day; returns subtract. 0 on days without sales."
    )


class StoreRevenue(BaseModel):
    """Net revenue for one store (stores without sales in the range show 0)."""

    store_code: str = Field(examples=["ST01"])
    store_name: str = Field(examples=["Anna Nagar"])
    revenue: Money = Field(description="Sum of line totals; returns subtract.")


class TopProduct(BaseModel):
    """A best-selling product, ranked by net units sold."""

    rank: int = Field(ge=1, le=10, description="1 = best seller.", examples=[1])
    sku: str = Field(examples=["SKU1"])
    product_name: str = Field(
        description="Product's canonical name.", examples=["Masala Chai"]
    )
    quantity_sold: int = Field(
        gt=0, description="Units sold minus units returned.", examples=[420]
    )
    revenue: Money = Field(description="Net revenue from this product.")


class StoreReturnRate(BaseModel):
    """How much of a store's sold quantity came back as returns, within the range."""

    store_code: str = Field(examples=["ST01"])
    units_sold: int = Field(
        ge=0, description="Units in sales (positive quantities).", examples=[400]
    )
    units_returned: int = Field(ge=0, description="Units in returns.", examples=[10])
    return_rate_percent: float = Field(
        ge=0,
        description="units_returned ÷ units_sold × 100, rounded to 2 decimals; 0 if nothing sold.",
        examples=[2.5],
    )


class PaymentSplit(BaseModel):
    """How customers paid for purchases: sales only, returns excluded."""

    payment_mode: PaymentMode
    transaction_count: int = Field(
        ge=0, description="Sales paid this way.", examples=[310]
    )
    revenue: Money = Field(description="Sales revenue paid this way.")
    share_percent: float = Field(
        ge=0,
        le=100,
        description="This mode's share of sales revenue, rounded to 2 decimals; 0 if no sales.",
        examples=[62.5],
    )


class DashboardResponse(BaseModel):
    """Everything the sales dashboard shows."""

    daily_revenue: list[DailyRevenue] = Field(
        description="One entry per day in the range (or from the first to the last sale), oldest first."
    )
    store_revenue: list[StoreRevenue] = Field(
        description="One entry per store, highest revenue first."
    )
    top_products: list[TopProduct] = Field(
        max_length=10, description="Up to 10 products with the most net units sold."
    )
    store_return_rates: list[StoreReturnRate] = Field(
        description="One entry per store."
    )
    payment_split: list[PaymentSplit] = Field(
        description="One entry per payment mode: CASH, UPI, CARD."
    )


# Uploads

UploadStatus = Literal["QUEUED", "ALREADY UPLOADED", "INVALID"]


class UploadedFileResult(BaseModel):
    """What happened to one file in an upload request."""

    filename: str = Field(examples=["sales_2026-10-17.csv"])
    status: UploadStatus = Field(
        description=(
            "QUEUED: staged; the worker will import it. ALREADY UPLOADED: the same "
            "file (same bytes) was uploaded before. INVALID: not accepted (see message)."
        )
    )
    batch_id: uuid.UUID | None = Field(
        description="Batch to poll for the import result; null when INVALID."
    )
    message: str = Field(
        examples=["batch 3f2c9a7e-0b1d-4c55-9a0e-2d1f6b7c8e90 queued."]
    )


class ImportUploadResponse(BaseModel):
    """Result of an upload request, one entry per file in the order sent."""

    files: list[UploadedFileResult]

    @computed_field(
        description="IDs of the batches created or re-queued by this request."
    )
    @property
    def queued_batch_ids(self) -> list[uuid.UUID]:
        return [f.batch_id for f in self.files if f.status == "QUEUED" and f.batch_id]
