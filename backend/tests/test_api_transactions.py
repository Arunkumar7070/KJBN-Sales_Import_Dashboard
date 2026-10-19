from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models import ImportBatch, Product, Store, Transaction
from app.services.sales_rules import IST


def ist(text):
    """'2026-10-17 09:00:00' in IST, as the UTC datetime stored in the database."""
    return datetime.fromisoformat(text).replace(tzinfo=IST).astimezone(UTC)


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,  # The test client calls the app from other threads.
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        session.add_all(
            [
                Store(code="ST01", name="Store One", city="Chennai"),
                Store(code="ST02", name="Store Two", city="Madurai"),
                Product(sku="SKU1", name="Masala Chai"),
                Product(sku="SKU2", name="Filter Coffee"),
                ImportBatch(
                    filename="seed.csv", file_sha256="0" * 64, status="COMPLETED"
                ),
            ]
        )
        session.commit()
    yield factory
    engine.dispose()


@pytest.fixture
def client(session_factory):
    def test_db():
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = test_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def add_sales(session_factory, *rows):
    """rows: (transaction_id, store_code, sku, sold_at IST text, quantity[, original])."""
    with session_factory() as session:
        batch_id = session.query(ImportBatch.id).scalar()
        for transaction_id, store_code, sku, sold_at, quantity, *original in rows:
            session.add(
                Transaction(
                    transaction_id=transaction_id,
                    store_code=store_code,
                    sold_at=ist(sold_at),
                    sku=sku,
                    product_name="Masala Chai" if sku == "SKU1" else "Filter Coffee",
                    quantity=quantity,
                    unit_price_paise=9999,
                    discount_paise=0,
                    payment_mode="UPI",
                    original_transaction_id=original[0] if original else None,
                    batch_id=batch_id,
                    line_total_paise=quantity * 9999,
                    is_return=quantity < 0,
                )
            )
            session.flush()
        session.commit()


@pytest.fixture
def sample(session_factory):
    add_sales(
        session_factory,
        ("T1", "ST01", "SKU1", "2026-10-15 10:00:00", 2),
        ("T2", "ST01", "SKU2", "2026-10-16 23:59:59", 1),  # Last second of the 16th.
        ("T3", "ST02", "SKU1", "2026-10-17 00:00:00", 3),  # First second of the 17th.
        ("R1", "ST01", "SKU1", "2026-10-17 12:00:00", -1, "T1"),
        ("T4", "ST02", "SKU2", "2026-10-18 08:00:00", 1),
    )


def search(client, **params):
    response = client.get("/api/v1/transactions", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def ids(body):
    return [item["transaction_id"] for item in body["items"]]


def test_no_filters_returns_everything_newest_first(client, sample):
    body = search(client)

    assert ids(body) == ["T4", "R1", "T3", "T2", "T1"]
    assert (body["total"], body["page"], body["limit"], body["pages"]) == (5, 1, 20, 1)


def test_items_show_money_in_paise_and_rupees(client, sample):
    item = search(client, sku="SKU1", returns_only=True)["items"][0]

    assert item["unit_price"] == {"paise": 9999, "rupees": "99.99"}
    assert item["line_total"] == {"paise": -9999, "rupees": "-99.99"}
    assert item["sold_at"] == "2026-10-17T06:30:00Z"  # 12:00 IST.


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"store_code": "ST01"}, ["R1", "T2", "T1"]),
        ({"sku": "SKU2"}, ["T4", "T2"]),
        ({"returns_only": "true"}, ["R1"]),
        ({"returns_only": "false"}, ["T4", "R1", "T3", "T2", "T1"]),
        ({"store_code": "ST01", "sku": "SKU1"}, ["R1", "T1"]),
        ({"store_code": "ST02", "returns_only": "true"}, []),
        ({"store_code": "st01"}, []),  # Exact match.
        ({"store_code": "NOPE"}, []),
    ],
    ids=[
        "store",
        "sku",
        "returns-only",
        "returns-only-false-means-all",
        "store-and-sku",
        "no-returns-in-store",
        "case-sensitive",
        "unknown-store",
    ],
)
def test_filters_combine_with_and(client, sample, params, expected):
    body = search(client, **params)

    assert ids(body) == expected
    assert body["total"] == len(expected)


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"date_from": "2026-10-17"}, ["T4", "R1", "T3"]),
        ({"date_to": "2026-10-16"}, ["T2", "T1"]),
        ({"date_from": "2026-10-17", "date_to": "2026-10-17"}, ["R1", "T3"]),
        ({"date_from": "2026-10-16", "date_to": "2026-10-16"}, ["T2"]),
        ({"date_from": "2026-10-19"}, []),
    ],
    ids=["from", "to-includes-last-second", "single-day", "other-single-day", "future"],
)
def test_date_range_uses_inclusive_ist_days(client, sample, params, expected):
    assert ids(search(client, **params)) == expected


def test_all_filters_together(client, sample):
    body = search(
        client,
        store_code="ST01",
        sku="SKU1",
        date_from="2026-10-17",
        date_to="2026-10-17",
        returns_only="true",
    )

    assert ids(body) == ["R1"]


def test_pages_cover_every_match_once_with_total(client, sample):
    pages = [search(client, limit=2, page=page) for page in (1, 2, 3)]

    assert [ids(page) for page in pages] == [["T4", "R1"], ["T3", "T2"], ["T1"]]
    assert {(page["total"], page["pages"]) for page in pages} == {(5, 3)}


def test_total_counts_filtered_rows_not_the_page(client, sample):
    body = search(client, store_code="ST01", limit=1)

    assert len(body["items"]) == 1
    assert (body["total"], body["pages"]) == (3, 3)


def test_page_past_the_end_is_empty_but_keeps_total(client, sample):
    body = search(client, page=99)

    assert (body["items"], body["total"]) == ([], 5)


def test_equal_sale_times_are_ordered_by_transaction_id(client, session_factory):
    add_sales(
        session_factory,
        *[(f"T{i}", "ST01", "SKU1", "2026-10-17 09:00:00", 1) for i in range(5)],
    )

    first = ids(search(client, limit=3))
    second = ids(search(client, limit=3, page=2))

    assert first + second == ["T4", "T3", "T2", "T1", "T0"]


def test_no_transactions_gives_zero_pages(client):
    body = search(client)

    assert (body["items"], body["total"], body["pages"]) == ([], 0, 0)


@pytest.mark.parametrize(
    "params",
    [
        {"page": 0},
        {"limit": 0},
        {"limit": 101},
        {"date_from": "17-10-2026"},
        {"date_to": "2026-02-30"},
        {"returns_only": "maybe"},
    ],
    ids=[
        "page-zero",
        "limit-zero",
        "limit-over-max",
        "bad-date-format",
        "bad-day",
        "bad-bool",
    ],
)
def test_invalid_parameters_are_rejected(client, params):
    assert client.get("/api/v1/transactions", params=params).status_code == 422


def test_date_from_after_date_to_is_rejected_with_message(client):
    response = client.get(
        "/api/v1/transactions",
        params={"date_from": "2026-10-18", "date_to": "2026-10-17"},
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "date_from must be on or before date_to."}


def test_ist_day_boundaries_are_exact(client, session_factory):
    # Midnight IST on the 17th is 18:30 UTC on the 16th.
    add_sales(
        session_factory,
        ("BEFORE", "ST01", "SKU1", "2026-10-16 23:59:59", 1),
        ("AT", "ST01", "SKU1", "2026-10-17 00:00:00", 1),
    )

    assert ids(search(client, date_from="2026-10-17")) == ["AT"]
    assert ids(search(client, date_to="2026-10-16")) == ["BEFORE"]
    assert ist("2026-10-17 00:00:00") == datetime(2026, 10, 16, 18, 30, tzinfo=UTC)
    assert ist("2026-10-17 00:00:00") - ist("2026-10-16 23:59:59") == timedelta(
        seconds=1
    )
