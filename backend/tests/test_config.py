import pytest

from app.config import Settings


@pytest.mark.parametrize(
    ("configured_url", "expected_url"),
    [
        (
            "postgresql://user:pw@host/db?sslmode=require",
            "postgresql+psycopg://user:pw@host/db?sslmode=require",
        ),
        ("postgres://user:pw@host/db", "postgresql+psycopg://user:pw@host/db"),
        (
            "postgresql+psycopg://user:pw@host/db",
            "postgresql+psycopg://user:pw@host/db",
        ),
        ("sqlite:///local.db", "sqlite:///local.db"),
    ],
)
def test_database_url_uses_psycopg3_driver(configured_url, expected_url):
    settings = Settings(database_url=configured_url, _env_file=None)

    assert settings.database_url == expected_url


def test_database_url_is_required(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(ValueError):
        Settings(_env_file=None)
