import csv
import io
import uuid
from datetime import UTC, datetime, timedelta

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
from app.models import ImportBatch, ImportPayload, ImportRowError, Store, Transaction
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
    app.dependency_overrides[routes.get_session_factory] = lambda: session_factory
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


# Batch history: GET /api/v1/imports


def add_batches(session_factory, count, start=FIXED_NOW):
    """Batches uploaded one minute apart; returns their IDs, oldest first."""
    ids = []
    with session_factory() as session:
        for i in range(count):
            batch = ImportBatch(
                filename=f"day{i}.csv",
                file_sha256=f"{i:064d}",
                status="COMPLETED",
                uploaded_at=start + timedelta(minutes=i),
            )
            session.add(batch)
            session.flush()
            ids.append(str(batch.id))
        session.commit()
    return ids


def test_list_is_empty_without_batches(client):
    response = client.get("/api/v1/imports")

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "limit": 20, "offset": 0}


def test_list_returns_newest_batches_first(client, session_factory):
    ids = add_batches(session_factory, 3)

    items = client.get("/api/v1/imports").json()["items"]

    assert [item["id"] for item in items] == ids[::-1]
    assert items[0]["filename"] == "day2.csv"
    assert set(items[0]) >= {
        "status",
        "rows_total",
        "rows_rejected",
        "error_report_url",
    }


def test_list_pages_cover_every_batch_once(client, session_factory):
    ids = add_batches(session_factory, 5)

    pages = [
        client.get("/api/v1/imports", params={"limit": 2, "offset": offset}).json()
        for offset in (0, 2, 4)
    ]

    assert [len(page["items"]) for page in pages] == [2, 2, 1]
    assert {page["total"] for page in pages} == {5}
    assert [item["id"] for page in pages for item in page["items"]] == ids[::-1]


def test_list_breaks_upload_time_ties_by_id(client, session_factory):
    with session_factory() as session:
        for i in range(3):
            session.add(
                ImportBatch(
                    filename=f"same{i}.csv",
                    file_sha256=f"{i:064d}",
                    status="QUEUED",
                    uploaded_at=FIXED_NOW,
                )
            )
        session.commit()

    first = client.get("/api/v1/imports", params={"limit": 2}).json()["items"]
    second = client.get("/api/v1/imports", params={"limit": 2, "offset": 2}).json()[
        "items"
    ]

    ids = [item["id"] for item in first + second]
    assert len(set(ids)) == 3
    assert ids == sorted(ids, key=uuid.UUID, reverse=True)


def test_offset_past_the_end_returns_no_items(client, session_factory):
    add_batches(session_factory, 2)

    body = client.get("/api/v1/imports", params={"offset": 10}).json()

    assert (body["items"], body["total"]) == ([], 2)


@pytest.mark.parametrize(
    "params",
    [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"limit": "ten"}],
    ids=["limit-zero", "limit-over-max", "negative-offset", "not-a-number"],
)
def test_invalid_paging_parameters_are_rejected(client, params):
    assert client.get("/api/v1/imports", params=params).status_code == 422


# Rejected rows: GET /api/v1/imports/{batch_id}/errors.csv


def read_csv(response):
    text = response.content.decode("utf-8")
    assert text.startswith("\ufeff")  # BOM so Excel reads UTF-8.
    return list(csv.reader(io.StringIO(text.removeprefix("\ufeff"))))


def test_error_csv_lists_rejected_rows(client, session_factory):
    rows = (
        HEADER
        + "T1,ST01,2026-10-17 09:00:00,SKU1,Masala Chai,1,100.00,0,UPI,\n"
        + "T2,ST01,2026-10-17 09:00:00,SKU1,Masala Chai,1,100.00,0,CHEQUE,\n"
        + "T3,ST01\n"
        + 'T4,ST99,2026-10-17 09:00:00,SKU1,"Chai, ""special""",1,1,0,UPI,\n'
    ).encode()
    batch_id = upload(client, ("day1.csv", rows)).json()["queued_batch_ids"][0]

    response = client.get(f"/api/v1/imports/{batch_id}/errors.csv")

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["content-disposition"] == (
        f'attachment; filename="batch-{batch_id}-errors.csv"'
    )
    assert read_csv(response) == [
        ["row_number", "column_name", "message", "transaction_id"],
        [
            "3",
            "payment_mode",
            "payment_mode must be CASH, UPI or CARD; got 'CHEQUE'.",
            "T2",
        ],
        ["4", "__row__", "Row has 2 column(s); expected 10.", ""],
        ["5", "store_code", "Store code 'ST99' does not exist.", "T4"],
    ]


def test_error_csv_for_batch_without_rejections_is_header_only(client):
    batch_id = upload(client, ("day1.csv", sales_csv("T1"))).json()["queued_batch_ids"][
        0
    ]

    response = client.get(f"/api/v1/imports/{batch_id}/errors.csv")

    assert response.status_code == 200
    assert read_csv(response) == [
        ["row_number", "column_name", "message", "transaction_id"]
    ]


def test_error_csv_for_unknown_batch_is_404(client):
    response = client.get(f"/api/v1/imports/{uuid.uuid4()}/errors.csv")

    assert response.status_code == 404
    assert response.json() == {"detail": "Batch not found."}


def test_error_csv_with_malformed_batch_id_is_422(client):
    assert client.get("/api/v1/imports/not-a-uuid/errors.csv").status_code == 422


def add_row_errors(session_factory, errors):
    with session_factory() as session:
        batch = ImportBatch(filename="x.csv", file_sha256="e" * 64, status="COMPLETED")
        session.add(batch)
        session.flush()
        session.add_all(
            ImportRowError(
                batch_id=batch.id,
                row_number=row,
                column_name=column,
                message=message,
                transaction_id=transaction_id,
            )
            for row, column, message, transaction_id in errors
        )
        session.commit()
        return batch.id


@pytest.mark.parametrize(
    ("transaction_id", "exported"),
    [
        ('=HYPERLINK("http://evil")', '\'=HYPERLINK("http://evil")'),
        ("+1+2", "'+1+2"),
        ("-5", "'-5"),
        ("@SUM(A1)", "'@SUM(A1)"),
        ("T-1", "T-1"),  # Only a leading character matters.
    ],
)
def test_error_csv_neutralises_spreadsheet_formulas(
    client, session_factory, transaction_id, exported
):
    batch_id = add_row_errors(
        session_factory, [(2, "store_code", "bad", transaction_id)]
    )

    rows = read_csv(client.get(f"/api/v1/imports/{batch_id}/errors.csv"))

    assert rows[1] == ["2", "store_code", "bad", exported]


def test_error_csv_streams_large_batches_completely_in_row_order(
    client, session_factory
):
    count = routes.ERROR_CSV_CHUNK_ROWS * 2 + 7  # Spans several chunks.
    errors = [
        (row, "sku", f"error {row}", f"T{row}") for row in range(count + 1, 1, -1)
    ]
    batch_id = add_row_errors(session_factory, errors)

    rows = read_csv(client.get(f"/api/v1/imports/{batch_id}/errors.csv"))

    assert len(rows) == count + 1
    assert [int(row[0]) for row in rows[1:]] == list(range(2, count + 2))
