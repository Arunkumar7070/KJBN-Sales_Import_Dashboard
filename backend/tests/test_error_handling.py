import asyncio
import io
import json
import logging

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from pydantic import BaseModel

from api import routes
from api.routes import RateLimiter
from app.logging_config import JsonFormatter
from app.main import INTERNAL_ERROR_MESSAGE, app, handle_unexpected_error


class Payload(BaseModel):
    quantity: int


def echo(payload: Payload):
    return payload


def crash():
    raise RuntimeError("secret internal detail: password=hunter2")


@pytest.fixture
def client():
    # Temporary routes: the real API has no JSON-body endpoint or crashing route.
    app.add_api_route("/_test/echo", echo, methods=["POST"])
    app.add_api_route("/_test/crash", crash, methods=["GET"])
    app.dependency_overrides[routes.get_upload_rate_limiter] = lambda: RateLimiter(100)
    with TestClient(app) as test_client:
        yield test_client
    app.router.routes[:] = [
        route
        for route in app.router.routes
        if not getattr(route, "path", "").startswith("/_test/")
    ]
    app.dependency_overrides.clear()


@pytest.fixture
def json_logs():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logging.getLogger().addHandler(handler)
    yield lambda: [json.loads(line) for line in stream.getvalue().splitlines()]
    logging.getLogger().removeHandler(handler)


def error_of(response):
    body = response.json()
    assert set(body) == {"error"}, body  # Always the same envelope.
    return body["error"]


# Envelope shape for intentional errors


def test_unknown_route_is_a_404_envelope(client):
    response = client.get("/api/v1/nothing-here", headers={"X-Request-ID": "trace-404"})

    assert response.status_code == 404
    assert error_of(response) == {
        "status": 404,
        "code": "not_found",
        "message": "Not Found",
        "request_id": "trace-404",
    }


def test_wrong_method_is_a_405_envelope(client):
    response = client.delete("/api/v1/stores")

    assert response.status_code == 405
    assert error_of(response)["code"] == "method_not_allowed"


def test_rate_limit_keeps_retry_after_header(client):
    limiter = RateLimiter(1)  # One instance, so requests are counted together.
    app.dependency_overrides[routes.get_upload_rate_limiter] = lambda: limiter
    files = [("files", ("a.txt", b"x", "text/plain"))]
    client.post("/api/v1/imports", files=files)

    response = client.post("/api/v1/imports", files=files)

    assert response.status_code == 429
    assert error_of(response)["code"] == "too_many_requests"
    assert int(response.headers["Retry-After"]) > 0


# Bad input: 400 / 422, never 500


def test_malformed_json_body_is_400(client):
    response = client.post(
        "/_test/echo",
        content=b'{"quantity": 3,,}',
        headers={"Content-Type": "application/json", "X-Request-ID": "trace-json"},
    )

    assert response.status_code == 400
    assert error_of(response) == {
        "status": 400,
        "code": "malformed_json",
        "message": "Request body is not valid JSON.",
        "request_id": "trace-json",
    }


def test_well_formed_but_invalid_json_is_422_with_field_details(client):
    response = client.post("/_test/echo", json={"quantity": "lots"})

    error = error_of(response)
    assert response.status_code == 422
    assert (error["code"], error["message"]) == (
        "validation_error",
        "Some request values are missing or invalid.",
    )
    assert error["details"] == [
        {
            "field": "body.quantity",
            "message": "Input should be a valid integer, unable to parse string as an integer",
            "type": "int_parsing",
        }
    ]


def test_validation_errors_do_not_echo_submitted_values(client):
    response = client.post("/_test/echo", json={"quantity": "SECRET-VALUE-99"})

    assert "SECRET-VALUE-99" not in response.text


@pytest.mark.parametrize(
    ("url", "field"),
    [
        ("/api/v1/transactions?page=0", "query.page"),
        ("/api/v1/transactions?limit=abc", "query.limit"),
        ("/api/v1/transactions?date_from=17-10-2026", "query.date_from"),
        ("/api/v1/analytics/dashboard?date_to=2026-02-30", "query.date_to"),
        ("/api/v1/imports/not-a-uuid/errors.csv", "path.batch_id"),
    ],
)
def test_bad_query_and_path_values_are_422_with_the_field(client, url, field):
    response = client.get(url)

    error = error_of(response)
    assert response.status_code == 422
    assert [detail["field"] for detail in error["details"]] == [field]


def test_upload_without_files_is_422(client):
    response = client.post("/api/v1/imports")

    assert response.status_code == 422
    assert error_of(response)["details"][0]["field"] == "body.files"


def test_broken_multipart_upload_is_400_not_500(client):
    response = client.post(
        "/api/v1/imports",
        content=b"--abc\r\nnot really multipart",
        headers={"Content-Type": "multipart/form-data; boundary=abc"},
    )

    assert response.status_code == 400
    assert error_of(response)["code"] == "bad_request"
    assert error_of(response)["message"] == "Invalid multipart data."


# Unexpected errors: logged in full, sanitized for the client


def test_unexpected_error_returns_sanitized_500(client):
    response = client.get("/_test/crash", headers={"X-Request-ID": "trace-boom"})

    assert response.status_code == 500
    assert error_of(response) == {
        "status": 500,
        "code": "internal_server_error",
        "message": INTERNAL_ERROR_MESSAGE,
        "request_id": "trace-boom",
    }
    assert response.headers["X-Request-ID"] == "trace-boom"
    assert "hunter2" not in response.text
    assert "Traceback" not in response.text


def test_unexpected_error_is_logged_with_traceback_and_request_id(client, json_logs):
    client.get("/_test/crash", headers={"X-Request-ID": "trace-boom"})

    [line] = [line for line in json_logs() if line["message"] == "Unhandled error"]
    assert line["level"] == "ERROR"
    assert (line["request_id"], line["path"], line["status_code"]) == (
        "trace-boom",
        "/_test/crash",
        500,
    )
    assert "Traceback" in line["exception"]
    assert "hunter2" in line["exception"]  # Full detail stays in the log.


def test_crash_inside_a_real_endpoint_is_a_clean_500(client, monkeypatch):
    def broken_dashboard(*args, **kwargs):
        return 1 / 0

    monkeypatch.setattr(routes, "build_dashboard", broken_dashboard)

    response = client.get("/api/v1/analytics/dashboard")

    assert response.status_code == 500
    assert error_of(response)["code"] == "internal_server_error"
    assert "ZeroDivisionError" not in response.text


def test_server_keeps_working_after_a_crash(client):
    client.get("/_test/crash")

    assert client.get("/health").status_code == 200


def test_last_resort_handler_returns_the_envelope():
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})

    response = asyncio.run(handle_unexpected_error(request, RuntimeError("boom")))

    body = json.loads(response.body)
    assert response.status_code == 500
    assert body["error"]["message"] == INTERNAL_ERROR_MESSAGE
    assert "boom" not in response.body.decode()
