import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models import Store


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,  # The test client calls the app from other threads.
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(engine, expire_on_commit=False)
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


def test_lists_all_stores_ordered_by_code(client, session_factory):
    with session_factory() as session:
        session.add_all(
            [
                Store(code="ST03", name="RS Puram", city="Coimbatore"),
                Store(code="ST01", name="Anna Nagar", city="Chennai"),
                Store(code="ST02", name="Town Hall", city="Madurai"),
            ]
        )
        session.commit()

    response = client.get("/api/v1/stores")

    assert response.status_code == 200
    assert response.json() == [
        {"code": "ST01", "name": "Anna Nagar", "city": "Chennai"},
        {"code": "ST02", "name": "Town Hall", "city": "Madurai"},
        {"code": "ST03", "name": "RS Puram", "city": "Coimbatore"},
    ]


def test_no_stores_gives_empty_list(client):
    response = client.get("/api/v1/stores")

    assert response.status_code == 200
    assert response.json() == []


def test_swagger_documents_the_stores_endpoint(client):
    openapi = client.get("/openapi.json").json()

    response_schema = openapi["paths"]["/api/v1/stores"]["get"]["responses"]["200"]
    schema = response_schema["content"]["application/json"]["schema"]
    assert schema["type"] == "array"
    assert schema["items"] == {"$ref": "#/components/schemas/StoreResponse"}
    assert set(openapi["components"]["schemas"]["StoreResponse"]["properties"]) == {
        "code",
        "name",
        "city",
    }
