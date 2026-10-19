import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import routes
from api.routes import RateLimiter
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
    limiter = RateLimiter(100)
    app.dependency_overrides[routes.get_upload_rate_limiter] = lambda: limiter
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


# POST /api/v1/stores: import stores from CSV


def import_csv(client, text, filename="stores.csv"):
    content = text.encode() if isinstance(text, str) else text
    return client.post(
        "/api/v1/stores", files={"file": (filename, content, "text/csv")}
    )


def stored(session_factory):
    with session_factory() as session:
        return {
            store.code: (store.name, store.city)
            for store in session.scalars(select(Store).order_by(Store.code))
        }


def seed(session_factory, *stores):
    with session_factory() as session:
        session.add_all(Store(code=c, name=n, city=city) for c, n, city in stores)
        session.commit()


def test_new_stores_are_inserted(client, session_factory):
    response = import_csv(
        client,
        "code,name,city\nST01,Anna Nagar,Chennai\nST02,Town Hall,Madurai\n",
    )

    assert response.status_code == 200
    assert response.json() == {
        "rows_total": 2,
        "inserted": 2,
        "updated": 0,
        "unchanged": 0,
        "rejected": 0,
        "errors": [],
    }
    assert stored(session_factory) == {
        "ST01": ("Anna Nagar", "Chennai"),
        "ST02": ("Town Hall", "Madurai"),
    }
    assert [s["code"] for s in client.get("/api/v1/stores").json()] == ["ST01", "ST02"]


def test_existing_codes_are_updated_and_new_ones_inserted(client, session_factory):
    seed(
        session_factory,
        ("ST01", "Old Name", "Chennai"),
        ("ST02", "Town Hall", "Madurai"),
    )

    body = import_csv(
        client,
        "code,name,city\n"
        "ST01,Anna Nagar,Chennai\n"  # name changed
        "ST02,Town Hall,Madurai\n"  # same
        "ST03,RS Puram,Coimbatore\n",  # new
    ).json()

    assert (body["inserted"], body["updated"], body["unchanged"]) == (1, 1, 1)
    assert stored(session_factory) == {
        "ST01": ("Anna Nagar", "Chennai"),
        "ST02": ("Town Hall", "Madurai"),
        "ST03": ("RS Puram", "Coimbatore"),
    }


def test_importing_the_same_file_twice_changes_nothing(client, session_factory):
    text = "code,name,city\nST01,Anna Nagar,Chennai\n"
    import_csv(client, text)

    body = import_csv(client, text).json()

    assert (body["inserted"], body["updated"], body["unchanged"]) == (0, 0, 1)


def test_city_change_counts_as_update(client, session_factory):
    seed(session_factory, ("ST01", "Anna Nagar", "Chennai"))

    body = import_csv(client, "code,name,city\nST01,Anna Nagar,Madras\n").json()

    assert body["updated"] == 1
    assert stored(session_factory)["ST01"] == ("Anna Nagar", "Madras")


def test_columns_in_any_order_with_bom_crlf_and_spaces(client, session_factory):
    text = "\ufeffcity,code,name\r\n  Chennai , ST01 ,  Anna Nagar \r\n"

    body = import_csv(client, text.encode()).json()

    assert body["inserted"] == 1
    assert stored(session_factory) == {"ST01": ("Anna Nagar", "Chennai")}


def test_bad_rows_are_reported_and_good_rows_still_imported(client, session_factory):
    body = import_csv(
        client,
        "code,name,city\n"
        "ST01,Anna Nagar,Chennai\n"  # row 2: ok
        "ST02,Town Hall,\n"  # row 3: no city
        "ST03,RS Puram\n"  # row 4: 2 columns
        "ST01,Anna Nagar East,Chennai\n"  # row 5: repeats row 2
        "   ,No Code,Salem\n"  # row 6: blank code
        "\n"  # blank line: skipped
        "ST07,Fort,Vellore\n",  # row 8: ok
    ).json()

    assert (body["rows_total"], body["inserted"], body["rejected"]) == (6, 2, 4)
    assert body["errors"] == [
        {
            "row_number": 3,
            "column_name": "city",
            "message": "Missing required value(s): city.",
        },
        {
            "row_number": 4,
            "column_name": "__row__",
            "message": "Row has 2 column(s); expected 3.",
        },
        {
            "row_number": 5,
            "column_name": "code",
            "message": "Store code 'ST01' already appears in row 2.",
        },
        {
            "row_number": 6,
            "column_name": "code",
            "message": "Missing required value(s): code.",
        },
    ]
    assert stored(session_factory) == {
        "ST01": ("Anna Nagar", "Chennai"),  # The first ST01 row wins.
        "ST07": ("Fort", "Vellore"),
    }


def test_large_file_is_upserted_in_chunks(client, session_factory):
    seed(session_factory, ("S0000", "Old", "Old City"))
    rows = "".join(f"S{i:04d},Store {i},City {i % 7}\n" for i in range(1200))

    body = import_csv(client, "code,name,city\n" + rows).json()

    assert (body["inserted"], body["updated"]) == (1199, 1)
    saved = stored(session_factory)
    assert len(saved) == 1200
    assert saved["S0000"] == ("Store 0", "City 0")
    assert saved["S1199"] == ("Store 1199", "City 2")


@pytest.mark.parametrize(
    ("filename", "content", "message_start"),
    [
        (
            "stores.csv",
            "code,name\nST01,Anna Nagar\n",
            "Invalid header: missing column(s): city",
        ),
        (
            "stores.csv",
            "Code,name,city\nST01,A,B\n",
            "Invalid header: missing column(s): code; unexpected column(s): 'Code' (expected 'code')",
        ),
        (
            "stores.txt",
            "code,name,city\nST01,A,B\n",
            "File must have a .csv extension.",
        ),
        ("stores.csv", "", "File is empty."),
        ("stores.csv", b"code,name,city\nST01,Caf\xe9,B\n", "File is not valid UTF-8"),
        (
            "stores.csv",
            'code,name,city\nST01,"unclosed,B\n',
            "Malformed CSV near line 2",
        ),
    ],
    ids=["missing-column", "wrong-case", "not-csv", "empty", "not-utf8", "bad-quotes"],
)
def test_unusable_file_is_400_and_imports_nothing(
    client, session_factory, filename, content, message_start
):
    response = import_csv(client, content, filename)

    error = response.json()["error"]
    assert response.status_code == 400
    assert error["code"] == "bad_request"
    assert error["message"].startswith(message_start)
    assert stored(session_factory) == {}


def test_missing_file_is_422(client):
    response = client.post("/api/v1/stores")

    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["field"] == "body.file"


def test_database_failure_is_503_without_details(client, monkeypatch):
    def unavailable(*args, **kwargs):
        raise OperationalError("INSERT", {}, Exception("password=secret"))

    monkeypatch.setattr(routes, "import_stores", unavailable)

    response = import_csv(client, "code,name,city\nST01,A,B\n")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"
    assert "password" not in response.text


def test_store_import_shares_the_upload_rate_limit(client):
    limiter = RateLimiter(1)
    app.dependency_overrides[routes.get_upload_rate_limiter] = lambda: limiter
    import_csv(client, "code,name,city\nST01,A,B\n")

    response = import_csv(client, "code,name,city\nST02,A,B\n")

    assert response.status_code == 429
    assert "Retry-After" in response.headers
