from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import routes
from api.routes import RateLimiter
from app import clock
from app.clock import InvalidTestClock, frozen_at, parse_test_clock, utc_now
from app.config import get_settings
from app.database import Base, get_db
from app.main import app
from app.models import ImportBatch, ImportRowError, Store, Transaction
from app.services import csv_validator

REAL_NOW = datetime(2026, 10, 19, 6, 30, tzinfo=UTC)  # 12:00 IST on the 19th.
HEADER = (
    "transaction_id,store_code,sold_at,sku,product_name,quantity,"
    "unit_price,discount,payment_mode,original_transaction_id\n"
)


@pytest.fixture(autouse=True)
def fixed_system_clock(monkeypatch):
    monkeypatch.setattr(clock, "system_now", lambda: REAL_NOW)


# Parsing and freezing


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("2026-10-17T09:00:00Z", datetime(2026, 10, 17, 9, 0, tzinfo=UTC)),
        ("2026-10-17T14:30:00+05:30", datetime(2026, 10, 17, 9, 0, tzinfo=UTC)),
        (
            "2026-10-17T09:00:00",
            datetime(2026, 10, 17, 9, 0, tzinfo=UTC),
        ),  # No offset: UTC.
        ("2026-10-17", datetime(2026, 10, 17, 0, 0, tzinfo=UTC)),
        (
            " 2026-10-17T09:00:00.250Z ",
            datetime(2026, 10, 17, 9, 0, 0, 250000, tzinfo=UTC),
        ),
    ],
    ids=["utc-z", "ist-offset", "no-offset", "date-only", "fraction-and-spaces"],
)
def test_parse_test_clock_returns_utc(header, expected):
    parsed = parse_test_clock(header)

    assert parsed == expected
    assert parsed.tzinfo == UTC


@pytest.mark.parametrize(
    "header", ["yesterday", "", "2026-13-01T00:00:00Z", "17/10/2026"]
)
def test_parse_test_clock_rejects_invalid_values(header):
    with pytest.raises(InvalidTestClock):
        parse_test_clock(header)


def test_frozen_at_shifts_utc_now_only_inside_the_block():
    moment = datetime(2020, 1, 1, tzinfo=UTC)

    assert utc_now() == REAL_NOW
    with frozen_at(moment):
        assert utc_now() == moment
        assert csv_validator.utc_now() == moment  # The importer uses the same clock.
    assert utc_now() == REAL_NOW


def test_frozen_at_none_keeps_the_real_clock():
    with frozen_at(None):
        assert utc_now() == REAL_NOW


# The X-Test-Clock header


@pytest.fixture
def session_factory(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"autocommit": False, "check_same_thread": False},
        poolclass=StaticPool,  # The test client calls the app from other threads.
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        session.add(Store(code="ST01", name="Store One", city="Chennai"))
        session.commit()
    monkeypatch.setattr(csv_validator, "SessionLocal", factory)  # Background worker.
    yield factory
    engine.dispose()


@pytest.fixture
def client(session_factory):
    def test_db():
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[routes.get_upload_rate_limiter] = lambda: RateLimiter(100)
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def test_clock_enabled(monkeypatch):
    monkeypatch.setattr(get_settings(), "allow_test_clock", True)


def upload(client, csv_rows, test_clock=None):
    headers = {"X-Test-Clock": test_clock} if test_clock else {}
    return client.post(
        "/api/v1/imports",
        files=[("files", ("sales.csv", (HEADER + csv_rows).encode(), "text/csv"))],
        headers=headers,
    )


def sale(transaction_id, sold_at_ist):
    return f"{transaction_id},ST01,{sold_at_ist},SKU1,Masala Chai,1,100.00,0,UPI,\n"


def saved_ids(session_factory):
    with session_factory() as session:
        return sorted(session.scalars(select(Transaction.transaction_id)))


def row_errors(session_factory):
    with session_factory() as session:
        return [
            (e.row_number, e.column_name, e.message)
            for e in session.scalars(
                select(ImportRowError).order_by(ImportRowError.row_number)
            )
        ]


def test_import_uses_header_time_for_future_check(
    client, session_factory, test_clock_enabled
):
    # The clock is moved back to 2026-10-10 09:00 IST: a sale on the 12th is
    # now in the future, while one on the 9th is fine.
    response = upload(
        client,
        sale("PAST", "2026-10-09 09:00:00") + sale("LATER", "2026-10-12 09:00:00"),
        test_clock="2026-10-10T03:30:00Z",
    )

    assert response.status_code == 202
    assert saved_ids(session_factory) == ["PAST"]
    assert row_errors(session_factory) == [(3, "sold_at", "sold_at is in the future.")]


def test_header_can_move_the_clock_forward(client, session_factory, test_clock_enabled):
    # A sale on 2026-12-01 is in the future by the real clock but not by the header.
    upload(
        client, sale("XMAS", "2026-12-01 10:00:00"), test_clock="2026-12-31T00:00:00Z"
    )

    assert saved_ids(session_factory) == ["XMAS"]


def test_without_header_the_real_clock_applies(
    client, session_factory, test_clock_enabled
):
    upload(client, sale("XMAS", "2026-12-01 10:00:00"))

    assert saved_ids(session_factory) == []
    assert row_errors(session_factory) == [(2, "sold_at", "sold_at is in the future.")]


def test_batch_timestamps_use_the_header_time(
    client, session_factory, test_clock_enabled
):
    upload(client, sale("T1", "2026-10-09 09:00:00"), test_clock="2026-10-10T03:30:00Z")

    with session_factory() as session:
        batch = session.scalars(select(ImportBatch)).one()
    simulated = datetime(2026, 10, 10, 3, 30)
    assert (batch.started_at, batch.completed_at) == (simulated, simulated)


def test_dashboard_is_as_of_the_header_time(
    client, session_factory, test_clock_enabled
):
    upload(
        client, sale("D17", "2026-10-17 10:00:00") + sale("D18", "2026-10-18 10:00:00")
    )

    shifted = client.get(
        "/api/v1/analytics/dashboard", headers={"X-Test-Clock": "2026-10-17T12:00:00Z"}
    ).json()
    current = client.get("/api/v1/analytics/dashboard").json()

    # As of 17:30 IST on the 17th, the sale on the 18th hasn't happened yet.
    assert [(d["day"], d["revenue"]["paise"]) for d in shifted["daily_revenue"]] == [
        ("2026-10-17", 10000)
    ]
    assert [(d["day"], d["revenue"]["paise"]) for d in current["daily_revenue"]] == [
        ("2026-10-17", 10000),
        ("2026-10-18", 10000),
        ("2026-10-19", 0),  # Today by the real clock.
    ]


def test_transaction_search_is_as_of_the_header_time(
    client, session_factory, test_clock_enabled
):
    upload(
        client, sale("D17", "2026-10-17 10:00:00") + sale("D18", "2026-10-18 10:00:00")
    )

    body = client.get(
        "/api/v1/transactions", headers={"X-Test-Clock": "2026-10-17T12:00:00Z"}
    ).json()

    assert [t["transaction_id"] for t in body["items"]] == ["D17"]
    assert body["total"] == 1


@pytest.mark.parametrize("bad", ["yesterday", "2026-13-01T00:00:00Z"])
def test_invalid_header_returns_400(client, test_clock_enabled, bad):
    response = client.get("/api/v1/stores", headers={"X-Test-Clock": bad})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"
    assert response.json()["error"]["message"] == (
        "Invalid X-Test-Clock header: expected an ISO-8601 date-time "
        "such as 2026-10-17T09:00:00Z."
    )


def test_header_is_ignored_when_not_enabled(client, session_factory):
    assert (
        client.get("/api/v1/stores", headers={"X-Test-Clock": "nonsense"}).status_code
        == 200
    )

    upload(
        client, sale("XMAS", "2026-12-01 10:00:00"), test_clock="2026-12-31T00:00:00Z"
    )

    assert saved_ids(session_factory) == []  # Real clock: still in the future.


def test_setting_defaults_to_off():
    from app.config import Settings

    assert Settings(database_url="sqlite://", _env_file=None).allow_test_clock is False


def test_real_time_after_the_request_is_unaffected(client, test_clock_enabled):
    client.get("/api/v1/stores", headers={"X-Test-Clock": "2000-01-01T00:00:00Z"})

    assert utc_now() == REAL_NOW
    assert utc_now() - REAL_NOW == timedelta(0)
