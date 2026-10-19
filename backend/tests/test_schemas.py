import uuid
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from app.models import ImportBatch, Store, Transaction
from app.schemas import (
    BatchResponse,
    DashboardResponse,
    Money,
    StoreResponse,
    TopProduct,
    TransactionResponse,
)

NOW = datetime(2026, 10, 17, 3, 30, tzinfo=UTC)
BATCH_ID = uuid.UUID("3f2c9a7e-0b1d-4c55-9a0e-2d1f6b7c8e90")


def a_batch(**changes):
    fields = {
        "id": BATCH_ID,
        "filename": "sales.csv",
        "status": "COMPLETED",
        "uploaded_at": NOW,
        "started_at": NOW,
        "completed_at": NOW,
        "rows_total": 3,
        "rows_accepted": 2,
        "rows_rejected": 1,
        "attempt_count": 1,
        "failure_reason": None,
    }
    return ImportBatch(**(fields | changes))


def a_return():
    return Transaction(
        transaction_id="R1",
        store_code="ST01",
        sold_at=NOW,
        sku="SKU1",
        product_name="Masala Chai",
        quantity=-2,
        unit_price_paise=9999,
        discount_paise=0,
        payment_mode="UPI",
        original_transaction_id="T1",
        batch_id=BATCH_ID,
        line_total_paise=-19998,
        is_return=True,
        created_at=NOW,
    )


@pytest.mark.parametrize(
    ("paise", "rupees"),
    [
        (0, "0.00"),
        (1, "0.01"),
        (5, "0.05"),
        (9999, "99.99"),
        (100, "1.00"),
        (-19998, "-199.98"),
    ],
)
def test_money_shows_exact_rupees_with_two_decimals(paise, rupees):
    assert Money(paise=paise).model_dump(mode="json") == {
        "paise": paise,
        "rupees": rupees,
    }
    assert Money(paise=paise).rupees == Decimal(rupees)


def test_transaction_response_reads_orm_object_and_formats_money():
    data = TransactionResponse.model_validate(a_return()).model_dump(mode="json")

    assert data["unit_price"] == {"paise": 9999, "rupees": "99.99"}
    assert data["discount"] == {"paise": 0, "rupees": "0.00"}
    assert data["line_total"] == {"paise": -19998, "rupees": "-199.98"}
    assert data["sold_at"] == "2026-10-17T03:30:00Z"
    assert (data["quantity"], data["is_return"], data["original_transaction_id"]) == (
        -2,
        True,
        "T1",
    )
    assert "unit_price_paise" not in data  # Only the Money fields are exposed.


def test_batch_response_links_error_csv_when_rows_were_rejected():
    data = BatchResponse.model_validate(a_batch(rows_rejected=1)).model_dump(
        mode="json"
    )

    assert data["error_report_url"] == f"/api/v1/imports/{BATCH_ID}/errors.csv"
    assert (data["rows_total"], data["rows_accepted"], data["rows_rejected"]) == (
        3,
        2,
        1,
    )


def test_batch_response_has_no_error_link_without_rejections():
    data = BatchResponse.model_validate(a_batch(rows_rejected=0)).model_dump(
        mode="json"
    )

    assert data["error_report_url"] is None


def test_batch_response_rejects_unknown_status():
    with pytest.raises(ValidationError):
        BatchResponse.model_validate(a_batch(status="DONE"))


def test_dashboard_allows_at_most_ten_top_products():
    product = TopProduct(
        rank=1, sku="SKU1", product_name="Chai", quantity_sold=1, revenue=Money(paise=1)
    )

    with pytest.raises(ValidationError):
        DashboardResponse(
            daily_revenue=[],
            store_revenue=[],
            top_products=[product] * 11,
            store_return_rates=[],
            payment_split=[],
        )


def test_swagger_documents_the_response_models():
    app = FastAPI()

    @app.get("/batches/{batch_id}", response_model=BatchResponse)
    def get_batch(): ...

    @app.get("/transactions", response_model=list[TransactionResponse])
    def list_transactions(): ...

    @app.get("/dashboard", response_model=DashboardResponse)
    def dashboard(): ...

    schemas = app.openapi()["components"]["schemas"]

    money = schemas["Money"]["properties"]
    assert set(money) == {"paise", "rupees"}
    assert money["rupees"]["examples"] == ["99.99"]
    assert schemas["BatchResponse"]["properties"]["status"]["enum"] == [
        "QUEUED",
        "PROCESSING",
        "COMPLETED",
        "REJECTED_HEADER",
        "FAILED",
    ]
    assert "error_report_url" in schemas["BatchResponse"]["properties"]
    transaction = schemas["TransactionResponse"]["properties"]
    assert "unit_price" in transaction and "unit_price_paise" not in transaction
    assert schemas["DashboardResponse"]["properties"]["top_products"]["maxItems"] == 10
    for name in (
        "DailyRevenue",
        "StoreRevenue",
        "TopProduct",
        "StoreReturnRate",
        "PaymentSplit",
    ):
        assert name in schemas


@pytest.mark.parametrize(
    "stored",
    [
        datetime(2026, 10, 17, 9, 0, tzinfo=timezone(timedelta(hours=5, minutes=30))),
        datetime(2026, 10, 17, 3, 30),  # No time zone, as SQLite returns it.
        datetime(2026, 10, 17, 3, 30, tzinfo=UTC),
    ],
    ids=["ist-session", "naive", "utc"],
)
def test_timestamps_are_always_returned_in_utc(stored):
    transaction = a_return()
    transaction.sold_at = stored

    data = TransactionResponse.model_validate(transaction).model_dump(mode="json")

    assert data["sold_at"] == "2026-10-17T03:30:00Z"


def test_store_response_reads_orm_store():
    store = Store(code="ST01", name="Anna Nagar", city="Chennai")

    assert StoreResponse.model_validate(store).model_dump() == {
        "code": "ST01",
        "name": "Anna Nagar",
        "city": "Chennai",
    }
