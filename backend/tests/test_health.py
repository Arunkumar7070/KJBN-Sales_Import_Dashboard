from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.database import get_db
from app.main import app


class FakeSession:
    def __init__(self, error: Exception | None = None):
        self.error = error

    def execute(self, statement):
        if self.error:
            raise self.error


def client_with_session(session: FakeSession) -> TestClient:
    app.dependency_overrides[get_db] = lambda: session
    return TestClient(app)


def teardown_function():
    app.dependency_overrides.clear()


def test_database_health_reports_connected():
    response = client_with_session(FakeSession()).get("/health/db")

    assert response.status_code == 200
    assert response.json() == {"status": "Ok", "database": "connected"}


def test_database_health_returns_503_without_leaking_details():
    error = OperationalError("SELECT 1", {}, Exception("password=secret"))

    response = client_with_session(FakeSession(error)).get("/health/db")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"
    assert response.json()["error"]["message"] == "Database unavailable"
    assert "password" not in response.text
