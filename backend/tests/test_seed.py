import csv
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import clock
from app.database import Base
from app.models import ImportBatch, Store, Transaction
from app.services import import_worker
from scripts import seed

# The seed sales end on 2026-10-20, so none of them is in the future.
FIXED_NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
SAMPLE_DATA_DIR = Path(__file__).resolve().parents[2] / "Sample_Data"


def data_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


@pytest.fixture
def session_factory(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(seed, "SessionLocal", factory)
    monkeypatch.setattr(import_worker, "SessionLocal", factory)
    monkeypatch.setattr(clock, "utc_now", lambda: FIXED_NOW)
    yield factory
    engine.dispose()


def count(factory, model) -> int:
    with factory() as session:
        return session.scalar(select(func.count()).select_from(model))


def test_seed_loads_six_stores_and_every_seed_sale(session_factory):
    assert seed.seed() is True

    assert count(session_factory, Store) == 6
    assert count(session_factory, Transaction) == len(data_rows(seed.SALES_FILE))
    with session_factory() as session:
        batch = session.scalars(select(ImportBatch)).one()
    assert batch.status == "COMPLETED"
    assert batch.rows_rejected == 0  # The seed data obeys every business rule.


def test_seed_twice_changes_nothing(session_factory, capsys):
    seed.seed()
    transactions_after_first_run = count(session_factory, Transaction)

    assert seed.seed() is True

    assert count(session_factory, Store) == 6
    assert count(session_factory, Transaction) == transactions_after_first_run
    assert count(session_factory, ImportBatch) == 1
    output = capsys.readouterr().out
    assert "6 unchanged" in output
    assert "ALREADY UPLOADED" in output


def test_seed_ids_never_clash_with_the_sample_files():
    # Reviewers upload the sample files on a seeded instance; they must still
    # import as new rows rather than as duplicates of the seed data.
    seed_ids = {row["transaction_id"] for row in data_rows(seed.SALES_FILE)}
    for sample in SAMPLE_DATA_DIR.glob("sample_*.csv"):
        sample_ids = {row.get("transaction_id") for row in data_rows(sample)}
        assert not seed_ids & sample_ids, sample.name
