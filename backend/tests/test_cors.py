import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api import routes
from api.routes import RateLimiter
from app.config import Settings
from app.main import app

FRONTEND = "http://localhost:5173"


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def preflight(client, origin=FRONTEND, method="POST", headers="content-type"):
    return client.options(
        "/api/v1/imports",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": headers,
        },
    )


def comma_set(value):
    return {item.strip().lower() for item in value.split(",")}


# Preflight (OPTIONS) from the Vite dev server


def test_preflight_from_frontend_is_allowed(client):
    response = preflight(client, headers="content-type, x-request-id, x-test-clock")

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == FRONTEND
    assert response.headers["access-control-allow-credentials"] == "true"
    assert comma_set(response.headers["access-control-allow-headers"]) >= {
        "content-type",
        "x-request-id",
        "x-test-clock",
    }
    assert response.headers["access-control-max-age"] == "600"


@pytest.mark.parametrize(
    "method", ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
)
def test_preflight_allows_standard_methods(client, method):
    response = preflight(client, method=method)

    assert response.status_code == 200
    allowed = {
        m.strip() for m in response.headers["access-control-allow-methods"].split(",")
    }
    assert method in allowed


def test_127_0_0_1_is_also_allowed(client):
    response = preflight(client, origin="http://127.0.0.1:5173")

    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"


@pytest.mark.parametrize(
    "origin",
    ["http://evil.example", "http://localhost:3000", "https://localhost:5173"],
    ids=["other-site", "other-port", "https-not-listed"],
)
def test_preflight_from_other_origins_is_refused(client, origin):
    response = preflight(client, origin=origin)

    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_preflight_with_unlisted_header_is_refused(client):
    response = preflight(client, headers="x-not-allowed")

    assert response.status_code == 400


# Actual requests


def test_get_from_frontend_has_cors_headers(client):
    response = client.get("/health", headers={"Origin": FRONTEND})

    assert response.headers["access-control-allow-origin"] == FRONTEND
    assert response.headers["access-control-allow-credentials"] == "true"
    assert comma_set(response.headers["access-control-expose-headers"]) == {
        "x-request-id",
        "retry-after",
        "content-disposition",
    }
    assert "x-request-id" in response.headers


def test_get_from_other_origin_has_no_cors_headers(client):
    response = client.get("/health", headers={"Origin": "http://evil.example"})

    assert response.status_code == 200  # The server answers; the browser blocks it.
    assert "access-control-allow-origin" not in response.headers


def test_same_origin_or_non_browser_requests_are_unaffected(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_error_responses_also_have_cors_headers(client):
    not_found = client.get("/api/v1/nope", headers={"Origin": FRONTEND})

    limiter = RateLimiter(1)
    app.dependency_overrides[routes.get_upload_rate_limiter] = lambda: limiter
    files = [("files", ("a.txt", b"x", "text/plain"))]
    client.post("/api/v1/imports", files=files, headers={"Origin": FRONTEND})
    limited = client.post("/api/v1/imports", files=files, headers={"Origin": FRONTEND})

    for response in (not_found, limited):
        assert response.headers["access-control-allow-origin"] == FRONTEND
    assert limited.status_code == 429
    assert "retry-after" in comma_set(limited.headers["access-control-expose-headers"])


def test_unexpected_500_also_has_cors_headers(client):
    def crash():
        raise RuntimeError("boom")

    app.add_api_route("/_test/cors-crash", crash, methods=["GET"])
    try:
        response = client.get("/_test/cors-crash", headers={"Origin": FRONTEND})
    finally:
        app.router.routes[:] = [
            r
            for r in app.router.routes
            if getattr(r, "path", "") != "/_test/cors-crash"
        ]

    assert response.status_code == 500
    assert response.headers["access-control-allow-origin"] == FRONTEND


# The setting


def test_default_origins_are_the_vite_dev_server():
    settings = Settings(database_url="sqlite://", _env_file=None)

    assert settings.cors_allowed_origins == [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]


def test_origins_can_be_set_comma_separated(monkeypatch):
    monkeypatch.setenv(
        "CORS_ALLOWED_ORIGINS",
        " https://dashboard.example.com/ , http://localhost:5173",
    )

    settings = Settings(database_url="sqlite://", _env_file=None)

    assert settings.cors_allowed_origins == [
        "https://dashboard.example.com",
        "http://localhost:5173",
    ]


def test_wildcard_origin_is_rejected(monkeypatch):
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "*")

    with pytest.raises(ValidationError, match="list origins explicitly"):
        Settings(database_url="sqlite://", _env_file=None)
