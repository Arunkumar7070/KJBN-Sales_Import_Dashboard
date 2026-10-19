"""Dashboard analytics against a small dataset with hand-computed answers.

Sales (IST)                         store  sku   qty  price   line total  mode
T1  2026-10-15 10:00:00             ST01   SKU1    3  100.00    300.00    UPI
T2  2026-10-15 23:59:59             ST01   SKU2    1   50.00     50.00    CASH
T3  2026-10-17 00:00:00             ST02   SKU1    2  100.00    200.00    CARD
T4  2026-10-17 01:00:00             ST02   SKU3   10   15.00    150.00    UPI
R1  2026-10-17 12:00:00 (of T1)     ST01   SKU1   -1  100.00   -100.00    UPI
No sales on 2026-10-16; store ST03 never sells.
"""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import clock
from app.database import Base, get_db
from app.main import app
from app.models import ImportBatch, Product, Store, Transaction
from app.services.sales_rules import IST

SAMPLE = [
    ("T1", "ST01", "SKU1", "2026-10-15 10:00:00", 3, 10000, "UPI", None),
    ("T2", "ST01", "SKU2", "2026-10-15 23:59:59", 1, 5000, "CASH", None),
    ("T3", "ST02", "SKU1", "2026-10-17 00:00:00", 2, 10000, "CARD", None),
    ("T4", "ST02", "SKU3", "2026-10-17 01:00:00", 10, 1500, "UPI", None),
    ("R1", "ST01", "SKU1", "2026-10-17 12:00:00", -1, 10000, "UPI", "T1"),
]
NOW = datetime(2026, 10, 18, 23, 0, tzinfo=IST)  # The clock's "now" in these tests.


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    """Pin "now" so results don't depend on the day the tests run."""
    monkeypatch.setattr(clock, "system_now", lambda: NOW)


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
                Store(code="ST01", name="Anna Nagar", city="Chennai"),
                Store(code="ST02", name="Town Hall", city="Madurai"),
                Store(code="ST03", name="RS Puram", city="Coimbatore"),
                Product(sku="SKU1", name="Masala Chai"),
                Product(sku="SKU2", name="Filter Coffee"),
                Product(sku="SKU3", name="Samosa"),
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


def add(session_factory, rows):
    with session_factory() as session:
        batch_id = session.query(ImportBatch.id).scalar()
        for tid, store, sku, sold_at, qty, price, mode, original in rows:
            session.add(
                Transaction(
                    transaction_id=tid,
                    store_code=store,
                    sold_at=datetime.fromisoformat(sold_at)
                    .replace(tzinfo=IST)
                    .astimezone(UTC),
                    sku=sku,
                    product_name=sku,
                    quantity=qty,
                    unit_price_paise=price,
                    discount_paise=0,
                    payment_mode=mode,
                    original_transaction_id=original,
                    batch_id=batch_id,
                    line_total_paise=qty * price,
                    is_return=qty < 0,
                )
            )
            session.flush()
        session.commit()


@pytest.fixture
def sample(session_factory):
    add(session_factory, SAMPLE)


def dashboard(client, **params):
    response = client.get("/api/v1/analytics/dashboard", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def paise(entries, key):
    return [(entry[key], entry["revenue"]["paise"]) for entry in entries]


# All time


def test_daily_revenue_groups_by_ist_day_and_fills_gaps(client, sample):
    daily = dashboard(client)["daily_revenue"]

    assert paise(daily, "day") == [
        ("2026-10-15", 35000),  # T2 at 23:59:59 IST stays on the 15th.
        ("2026-10-16", 0),  # No sales: still listed.
        ("2026-10-17", 25000),  # T3 at 00:00 IST is the 17th; R1 subtracts.
        ("2026-10-18", 0),  # Without date_to the list runs until today.
    ]
    assert daily[0]["revenue"]["rupees"] == "350.00"


def test_store_revenue_includes_stores_without_sales(client, sample):
    stores = dashboard(client)["store_revenue"]

    assert paise(stores, "store_code") == [
        ("ST02", 35000),
        ("ST01", 25000),
        ("ST03", 0),
    ]
    assert [s["store_name"] for s in stores] == ["Town Hall", "Anna Nagar", "RS Puram"]


def test_top_products_rank_by_net_quantity(client, sample):
    top = dashboard(client)["top_products"]

    assert [
        (p["rank"], p["sku"], p["product_name"], p["quantity_sold"]) for p in top
    ] == [
        (1, "SKU3", "Samosa", 10),
        (2, "SKU1", "Masala Chai", 4),  # 3 + 2 sold, 1 returned.
        (3, "SKU2", "Filter Coffee", 1),
    ]
    assert [p["revenue"]["paise"] for p in top] == [15000, 40000, 5000]


def test_return_rates_per_store_with_zero_for_no_sales(client, sample):
    rates = dashboard(client)["store_return_rates"]

    assert rates == [
        {
            "store_code": "ST01",
            "units_sold": 4,
            "units_returned": 1,
            "return_rate_percent": 25.0,
        },
        {
            "store_code": "ST02",
            "units_sold": 12,
            "units_returned": 0,
            "return_rate_percent": 0.0,
        },
        {
            "store_code": "ST03",
            "units_sold": 0,
            "units_returned": 0,
            "return_rate_percent": 0.0,
        },
    ]


def test_payment_split_counts_sales_only_and_shares_add_to_100(client, sample):
    split = dashboard(client)["payment_split"]

    assert [
        (
            s["payment_mode"],
            s["transaction_count"],
            s["revenue"]["paise"],
            s["share_percent"],
        )
        for s in split
    ] == [
        ("CASH", 1, 5000, 7.14),
        ("UPI", 2, 45000, 64.29),  # The UPI return R1 is not counted.
        ("CARD", 1, 20000, 28.57),
    ]
    assert sum(s["share_percent"] for s in split) == pytest.approx(100, abs=0.01)


# Date ranges


def test_single_day_range(client, sample):
    body = dashboard(client, date_from="2026-10-17", date_to="2026-10-17")

    assert paise(body["daily_revenue"], "day") == [("2026-10-17", 25000)]
    assert paise(body["store_revenue"], "store_code") == [
        ("ST02", 35000),
        ("ST03", 0),
        ("ST01", -10000),  # Only a return that day.
    ]
    assert [(p["sku"], p["quantity_sold"]) for p in body["top_products"]] == [
        ("SKU3", 10),
        ("SKU1", 1),
    ]
    assert [(s["payment_mode"], s["share_percent"]) for s in body["payment_split"]] == [
        ("CASH", 0.0),
        ("UPI", 42.86),
        ("CARD", 57.14),
    ]


def test_store_with_only_returns_has_zero_rate_not_division_error(client, sample):
    rates = dashboard(client, date_from="2026-10-17", date_to="2026-10-17")[
        "store_return_rates"
    ]

    st01 = next(rate for rate in rates if rate["store_code"] == "ST01")
    assert st01 == {
        "store_code": "ST01",
        "units_sold": 0,
        "units_returned": 1,
        "return_rate_percent": 0.0,
    }


def test_range_without_any_sales_returns_zeros_without_errors(client, sample):
    body = dashboard(client, date_from="2026-11-01", date_to="2026-11-03")

    assert paise(body["daily_revenue"], "day") == [
        ("2026-11-01", 0),
        ("2026-11-02", 0),
        ("2026-11-03", 0),
    ]
    assert {s["revenue"]["paise"] for s in body["store_revenue"]} == {0}
    assert body["top_products"] == []
    assert {r["return_rate_percent"] for r in body["store_return_rates"]} == {0.0}
    assert [
        (s["payment_mode"], s["transaction_count"], s["share_percent"])
        for s in body["payment_split"]
    ] == [
        ("CASH", 0, 0.0),
        ("UPI", 0, 0.0),
        ("CARD", 0, 0.0),
    ]


def test_only_date_from_runs_until_today(client, sample):
    daily = dashboard(client, date_from="2026-10-16")["daily_revenue"]

    assert [d["day"] for d in daily] == ["2026-10-16", "2026-10-17", "2026-10-18"]


def test_product_with_only_returns_is_not_a_top_product(
    client, sample, session_factory
):
    add(
        session_factory,
        [("R2", "ST01", "SKU2", "2026-10-18 09:00:00", -1, 5000, "CASH", "T2")],
    )

    body = dashboard(client, date_from="2026-10-18", date_to="2026-10-18")

    assert body["top_products"] == []
    assert body["payment_split"][0]["transaction_count"] == 0  # Returns excluded.


def test_empty_database_without_dates(client):
    body = dashboard(client)

    assert body["daily_revenue"] == []
    assert body["top_products"] == []
    assert [s["store_code"] for s in body["store_revenue"]] == ["ST01", "ST02", "ST03"]


def test_more_than_ten_products_returns_top_ten(client, session_factory):
    with session_factory() as session:
        session.add_all(Product(sku=f"P{i:02d}", name=f"Item {i}") for i in range(12))
        session.commit()
    add(
        session_factory,
        [
            (
                f"T{i}",
                "ST01",
                f"P{i:02d}",
                "2026-10-15 10:00:00",
                i + 1,
                100,
                "UPI",
                None,
            )
            for i in range(12)
        ],
    )

    top = dashboard(client)["top_products"]

    assert [p["sku"] for p in top] == [f"P{i:02d}" for i in range(11, 1, -1)]
    assert [p["rank"] for p in top] == list(range(1, 11))


@pytest.mark.parametrize(
    "params",
    [{"date_from": "2026-10-18", "date_to": "2026-10-17"}, {"date_from": "17/10/2026"}],
    ids=["from-after-to", "bad-format"],
)
def test_invalid_dates_are_rejected(client, params):
    assert client.get("/api/v1/analytics/dashboard", params=params).status_code == 422
