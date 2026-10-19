from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import routes
from api.routes import RateLimiter
from app.config import get_settings
from app.database import Base, get_db
from app.main import app
from app.models import ImportBatch, ImportPayload, Store, Transaction
from app.services import csv_validator

FIXED_NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
HEADER = (
    "transaction_id,store_code,sold_at,sku,product_name,quantity,"
    "unit_price,discount,payment_mode,original_transaction_id\n"
)


def sales_csv(*transaction_ids):
    rows = "".join(
        f"{tid},ST01,2026-10-17 09:00:00,SKU1,Masala Chai,1,100.00,0,UPI,\n"
        for tid in transaction_ids
    )
    return (HEADER + rows).encode()


def upload(client, *files):
    return client.post(
        "/api/v1/imports",
        files=[("files", (name, content, "text/csv")) for name, content in files],
    )


@pytest.fixture
def session_factory(monkeypatch):
    # StaticPool: the test client calls the app from other threads, and every
    # thread must see the same in-memory database.
    engine = create_engine(
        "sqlite://",
        connect_args={"autocommit": False, "check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        session.add(Store(code="ST01", name="Store One", city="Chennai"))
        session.commit()
    monkeypatch.setattr(csv_validator, "SessionLocal", factory)  # Worker side.
    monkeypatch.setattr(csv_validator, "utc_now", lambda: FIXED_NOW)
    yield factory
    engine.dispose()


@pytest.fixture
def clock():
    return {"now": 1000.0}


@pytest.fixture
def client(session_factory, clock):
    def test_db():
        with session_factory() as session:
            yield session

    limiter = RateLimiter(max_requests=3, clock=lambda: clock["now"])
    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[routes.get_upload_rate_limiter] = lambda: limiter
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def saved(session_factory, model):
    with session_factory() as session:
        return session.scalars(select(model)).all()


def test_upload_returns_202_with_batch_ids_and_worker_imports(client, session_factory):
    response = upload(
        client, ("day1.csv", sales_csv("T1")), ("day2.csv", sales_csv("T2"))
    )

    body = response.json()
    batches = {b.filename: b for b in saved(session_factory, ImportBatch)}
    assert response.status_code == 202
    assert [f["status"] for f in body["files"]] == ["QUEUED", "QUEUED"]
    assert body["queued_batch_ids"] == [
        str(batches["day1.csv"].id),
        str(batches["day2.csv"].id),
    ]
    # The background worker ran after the response: rows imported, payloads gone.
    assert {b.status for b in batches.values()} == {"COMPLETED"}
    assert sorted(t.transaction_id for t in saved(session_factory, Transaction)) == [
        "T1",
        "T2",
    ]
    assert saved(session_factory, ImportPayload) == []


def test_bad_file_is_reported_and_good_files_still_queued(client, session_factory):
    response = upload(
        client,
        ("good.csv", sales_csv("T1")),
        ("notes.txt", sales_csv("T2")),
        ("empty.csv", b""),
    )

    files = response.json()["files"]
    assert response.status_code == 202
    assert [(f["filename"], f["status"]) for f in files] == [
        ("good.csv", "QUEUED"),
        ("notes.txt", "INVALID"),
        ("empty.csv", "INVALID"),
    ]
    assert files[1]["message"] == "File must have a .csv extension."
    assert files[1]["batch_id"] is None
    assert [t.transaction_id for t in saved(session_factory, Transaction)] == ["T1"]


def test_only_invalid_files_returns_400(client, session_factory):
    response = upload(client, ("notes.txt", b"hello"))

    assert response.status_code == 400
    assert response.json()["files"][0]["status"] == "INVALID"
    assert response.json()["queued_batch_ids"] == []
    assert saved(session_factory, ImportBatch) == []


def test_reupload_returns_200_with_the_existing_batch(client, session_factory):
    first = upload(client, ("day1.csv", sales_csv("T1")))

    second = upload(client, ("copy.csv", sales_csv("T1")))

    original_id = first.json()["queued_batch_ids"][0]
    assert second.status_code == 200
    assert second.json()["files"][0]["status"] == "ALREADY UPLOADED"
    assert second.json()["files"][0]["batch_id"] == original_id
    assert len(saved(session_factory, ImportBatch)) == 1


def test_oversized_file_is_rejected_without_staging(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_upload_size_bytes", 50)

    response = upload(client, ("big.csv", b"x" * 51))

    assert response.status_code == 400
    assert response.json()["files"][0]["message"] == (
        "File is 51 bytes, which exceeds the 50-byte limit."
    )


def test_directory_parts_are_stripped_from_filenames(client, session_factory):
    upload(client, ("../../etc/day1.csv", sales_csv("T1")))

    assert saved(session_factory, ImportBatch)[0].filename == "day1.csv"


def test_too_many_files_in_one_request_is_rejected(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_files_per_upload", 2)
    files = [(f"f{i}.csv", sales_csv(f"T{i}")) for i in range(3)]

    response = upload(client, *files)

    assert response.status_code == 400
    assert response.json()["detail"] == "At most 2 files per upload; got 3."


def test_request_without_files_is_a_validation_error(client):
    assert client.post("/api/v1/imports").status_code == 422


def test_database_down_returns_503_without_details(client, monkeypatch):
    # The session connects lazily, so an outage surfaces on the first query,
    # inside staging.
    def unavailable(*args, **kwargs):
        raise OperationalError("SELECT", {}, Exception("password=secret"))

    monkeypatch.setattr(routes, "enqueue_upload", unavailable)

    response = upload(client, ("day1.csv", sales_csv("T1")))

    assert response.status_code == 503
    assert response.json() == {"detail": "Database unavailable. Please try again."}


# Rate limiting (fixture limit: 3 uploads per 60 seconds per client)


def test_fourth_upload_within_a_minute_gets_429(client, clock):
    for i in range(3):
        assert upload(client, (f"f{i}.csv", sales_csv(f"T{i}"))).status_code == 202
        clock["now"] += 10

    response = upload(client, ("f3.csv", sales_csv("T3")))

    assert response.status_code == 429
    assert (
        response.headers["Retry-After"] == "30"
    )  # First upload leaves the window at +60s.
    assert response.json() == {"detail": "Too many uploads. Try again in 30 seconds."}


def test_uploads_are_allowed_again_after_the_window(client, clock):
    for i in range(3):
        upload(client, (f"f{i}.csv", sales_csv(f"T{i}")))
    assert upload(client, ("f3.csv", sales_csv("T3"))).status_code == 429

    clock["now"] += 60

    assert upload(client, ("f3.csv", sales_csv("T3"))).status_code == 202


def test_rejected_requests_also_count_toward_the_limit(client):
    for _ in range(3):
        assert upload(client, ("notes.txt", b"x")).status_code == 400

    assert upload(client, ("day1.csv", sales_csv("T1"))).status_code == 429


@pytest.mark.parametrize(
    ("timestamps", "now", "expected"),
    [
        ([], 0.0, None),
        ([0.0, 1.0], 2.0, None),
        ([0.0, 1.0, 2.0], 3.0, 57.0),
        ([0.0, 1.0, 2.0], 60.0, None),  # The first request has just left the window.
    ],
    ids=["first", "under-limit", "at-limit", "window-slid"],
)
def test_rate_limiter_sliding_window(timestamps, now, expected):
    current = {"now": 0.0}
    limiter = RateLimiter(max_requests=3, clock=lambda: current["now"])
    for moment in timestamps:
        current["now"] = moment
        assert limiter.check("ip") is None

    current["now"] = now

    assert limiter.check("ip") == expected


def test_rate_limiter_tracks_each_client_separately():
    limiter = RateLimiter(max_requests=1, clock=lambda: 0.0)

    assert limiter.check("10.0.0.1") is None
    assert limiter.check("10.0.0.1") is not None
    assert limiter.check("10.0.0.2") is None


def test_swagger_documents_the_upload_endpoint(client):
    operation = client.get("/openapi.json").json()["paths"]["/api/v1/imports"]["post"]

    assert set(operation["responses"]) >= {"200", "202", "400", "422", "429", "503"}
    assert "multipart/form-data" in operation["requestBody"]["content"]
