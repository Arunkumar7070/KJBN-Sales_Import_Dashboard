import io
import json
import logging
import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.main as main_module
from api import routes
from api.routes import RateLimiter
from app.config import get_settings
from app.database import Base, get_db
from app.logging_config import JsonFormatter, current_request_id, request_context
from app.main import app
from app.models import Store
from app.services import import_worker, sales_importer

CSV_HEADER = (
    "transaction_id,store_code,sold_at,sku,product_name,quantity,"
    "unit_price,discount,payment_mode,original_transaction_id\n"
)


@pytest.fixture
def json_logs():
    """Collect every log line, formatted exactly as in production, as dicts."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    yield lambda: [json.loads(line) for line in stream.getvalue().splitlines()]
    root.removeHandler(handler)


@pytest.fixture
def session_factory(monkeypatch):
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
    monkeypatch.setattr(import_worker, "SessionLocal", factory)
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


def request_lines(logs):
    return [line for line in logs if line["logger"] == "app.requests"]


# Header propagation


def test_missing_request_id_gets_a_new_uuid4(client):
    response = client.get("/health")

    request_id = response.headers["X-Request-ID"]
    assert uuid.UUID(request_id).version == 4


def test_incoming_request_id_is_echoed_back(client):
    response = client.get("/health", headers={"X-Request-ID": "frontend-42.retry_1"})

    assert response.headers["X-Request-ID"] == "frontend-42.retry_1"


@pytest.mark.parametrize(
    "unsafe",
    ["has space", "x" * 129, "semi;colon", '{"json":1}', ""],
    ids=["space", "too-long", "semicolon", "json", "empty"],
)
def test_unsafe_incoming_request_id_is_replaced(client, unsafe):
    response = client.get("/health", headers={"X-Request-ID": unsafe})

    assert response.headers["X-Request-ID"] != unsafe
    assert uuid.UUID(response.headers["X-Request-ID"]).version == 4


def test_each_request_gets_its_own_id(client):
    ids = {client.get("/health").headers["X-Request-ID"] for _ in range(5)}

    assert len(ids) == 5


@pytest.mark.parametrize(
    ("path", "expected_status"),
    [
        ("/no-such-route", 404),
        ("/api/v1/transactions?page=0", 422),
        ("/api/v1/imports/not-a-uuid/errors.csv", 422),
    ],
)
def test_error_responses_also_carry_the_request_id(client, path, expected_status):
    response = client.get(path, headers={"X-Request-ID": "trace-err"})

    assert response.status_code == expected_status
    assert response.headers["X-Request-ID"] == "trace-err"


def test_test_clock_rejection_carries_the_request_id(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "allow_test_clock", True)

    response = client.get(
        "/api/v1/stores",
        headers={"X-Test-Clock": "nonsense", "X-Request-ID": "trace-clock"},
    )

    assert response.status_code == 400
    assert response.headers["X-Request-ID"] == "trace-clock"


# Structured logs


def test_request_is_logged_as_one_json_line(client, json_logs):
    response = client.get("/api/v1/stores", headers={"X-Request-ID": "trace-1"})

    [line] = request_lines(json_logs())
    assert line["message"] == "Request completed"
    assert line["level"] == "INFO"
    assert line["request_id"] == "trace-1" == response.headers["X-Request-ID"]
    assert (line["method"], line["path"], line["status_code"]) == (
        "GET",
        "/api/v1/stores",
        200,
    )
    assert isinstance(line["duration_ms"], float) and line["duration_ms"] >= 0
    assert datetime.fromisoformat(line["timestamp"]).utcoffset().total_seconds() == 0


def test_status_code_is_logged_for_errors(client, json_logs):
    client.get("/no-such-route")

    [line] = request_lines(json_logs())
    assert (line["status_code"], line["path"]) == (404, "/no-such-route")


def test_logs_inside_a_request_carry_its_id_and_path(client, json_logs, monkeypatch):
    def unavailable(session):
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr(main_module, "check_database_connection", unavailable)

    response = client.get("/health/db", headers={"X-Request-ID": "trace-db"})

    lines = json_logs()
    [failure] = [
        line for line in lines if line["message"] == "Database health check failed"
    ]
    assert response.status_code == 503
    assert (failure["level"], failure["request_id"], failure["path"]) == (
        "ERROR",
        "trace-db",
        "/health/db",
    )
    assert "connection refused" in failure["exception"]  # Details stay in the log.
    assert "connection refused" not in response.text  # ...not in the response.
    assert request_lines(lines)[0]["status_code"] == 503


def test_background_import_logs_carry_the_upload_request_id(
    client, json_logs, monkeypatch
):
    def broken_lookup(*args, **kwargs):
        raise OperationalError("SELECT", {}, Exception("connection lost"))

    monkeypatch.setattr(sales_importer, "load_import_state", broken_lookup)
    rows = "T1,ST01,2026-10-17 09:00:00,SKU1,Masala Chai,1,100.00,0,UPI,\n"

    response = client.post(
        "/api/v1/imports",
        files=[("files", ("day1.csv", (CSV_HEADER + rows).encode(), "text/csv"))],
        headers={"X-Request-ID": "trace-upload"},
    )

    worker_errors = [
        line for line in json_logs() if line["message"].startswith("Import of batch")
    ]
    assert response.status_code == 202
    assert len(worker_errors) == 3  # The drain retries the failing batch 3 times.
    assert {line["request_id"] for line in worker_errors} == {"trace-upload"}


def test_uploaded_csv_contents_are_never_logged(client, json_logs):
    secret = "SECRET-CUSTOMER-NOTE-7731"
    rows = (
        f"T1,ST01,2026-10-17 09:00:00,SKU1,{secret},1,100.00,0,UPI,\n"
        f"T2,NOPE,2026-10-17 09:00:00,SKU1,{secret},1,100.00,0,CHEQUE,\n"
    )

    client.post(
        "/api/v1/imports",
        files=[("files", ("day1.csv", (CSV_HEADER + rows).encode(), "text/csv"))],
    )

    logged = json.dumps(json_logs())
    assert request_lines(json_logs())[0]["path"] == "/api/v1/imports"
    assert secret not in logged
    assert "T2,NOPE" not in logged


def test_request_context_is_cleared_after_the_request(client):
    client.get("/health", headers={"X-Request-ID": "trace-scope"})

    assert current_request_id() is None


# Formatter


def test_formatter_includes_extra_fields_and_context():
    record = logging.makeLogRecord(
        {
            "name": "demo",
            "levelname": "WARNING",
            "msg": "Low stock %s",
            "args": ("SKU1",),
        }
    )
    record.sku = "SKU1"

    with request_context("trace-unit", "/api/v1/demo"):
        entry = json.loads(JsonFormatter().format(record))

    assert entry["message"] == "Low stock SKU1"
    assert (entry["level"], entry["logger"]) == ("WARNING", "demo")
    assert (entry["request_id"], entry["path"]) == ("trace-unit", "/api/v1/demo")
    assert entry["sku"] == "SKU1"


def test_formatter_outside_a_request_has_null_request_id():
    entry = json.loads(
        JsonFormatter().format(logging.makeLogRecord({"msg": "startup"}))
    )

    assert (entry["request_id"], entry["path"]) == (None, None)


def test_formatter_drops_uvicorn_colour_codes():
    record = logging.makeLogRecord({"msg": "Started server process [1]"})
    record.color_message = "Started server process [\x1b[36m%d\x1b[0m]"

    assert "color_message" not in json.loads(JsonFormatter().format(record))
