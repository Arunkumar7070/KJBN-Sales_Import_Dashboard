"""Concurrency test for returns against the same original sale.

Needs real PostgreSQL row locks (SQLite ignores SELECT ... FOR UPDATE), so it
runs only when TEST_POSTGRES_URL is set. All tables live in a temporary schema
that is dropped afterwards; existing tables in that database are not touched.

    TEST_POSTGRES_URL=postgresql://user@localhost:5432/postgres pytest tests/test_concurrent_returns.py
"""

import os
import threading
import time
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.database import Base
from app.models import ImportBatch, ImportRowError, Product, Store, Transaction
from app.services import csv_validator

TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not TEST_POSTGRES_URL, reason="set TEST_POSTGRES_URL to a PostgreSQL database"
)

FIXED_NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
HEADER = (
    "transaction_id,store_code,sold_at,sku,product_name,quantity,"
    "unit_price,discount,payment_mode,original_transaction_id\n"
)
LOCK_WAIT_TIMEOUT_SECONDS = 10


@pytest.fixture
def postgres(monkeypatch):
    """Session factory bound to a throwaway schema in TEST_POSTGRES_URL."""
    schema = f"test_concurrency_{uuid.uuid4().hex[:12]}"
    url = Settings(database_url=TEST_POSTGRES_URL, _env_file=None).database_url
    base_engine = create_engine(url)
    with base_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = base_engine.execution_options(schema_translate_map={None: schema})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(csv_validator, "SessionLocal", factory)
    monkeypatch.setattr(csv_validator, "utc_now", lambda: FIXED_NOW)
    try:
        yield factory, base_engine
    finally:
        with base_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        base_engine.dispose()


def seed_sale_of_one_unit(factory):
    with factory() as session:
        batch = ImportBatch(
            filename="seed.csv", file_sha256="0" * 64, status="COMPLETED"
        )
        session.add_all(
            [
                Store(code="ST01", name="Store One", city="Chennai"),
                Product(sku="SKU1", name="Masala Chai"),
                batch,
            ]
        )
        session.flush()
        session.add(
            Transaction(
                transaction_id="T1",
                store_code="ST01",
                sold_at=datetime(2026, 10, 17, 3, 30, tzinfo=UTC),  # 09:00 IST
                sku="SKU1",
                product_name="Masala Chai",
                quantity=1,
                unit_price_paise=10000,
                discount_paise=0,
                payment_mode="UPI",
                batch_id=batch.id,
                line_total_paise=10000,
                is_return=False,
            )
        )
        session.commit()


def write_return_file(directory, name, return_id, sold_at):
    path = directory / name
    path.write_text(
        HEADER + f"{return_id},ST01,{sold_at},SKU1,Masala Chai,-1,100.00,0,UPI,T1\n"
    )
    return path


def wait_until_a_backend_waits_on_a_lock(engine):
    deadline = time.monotonic() + LOCK_WAIT_TIMEOUT_SECONDS
    query = text(
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE wait_event_type = 'Lock' AND datname = current_database()"
    )
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            if connection.scalar(query):
                return
        time.sleep(0.05)
    pytest.fail(
        "Import B never waited for a row lock: it read the original sale "
        "without SELECT ... FOR UPDATE, so both imports could return it."
    )


def test_concurrent_returns_of_last_unit_only_one_succeeds(
    postgres, tmp_path, monkeypatch
):
    factory, engine = postgres
    seed_sale_of_one_unit(factory)
    file_a = write_return_file(tmp_path, "a.csv", "R1", "2026-10-17 10:00:00")
    file_b = write_return_file(tmp_path, "b.csv", "R2", "2026-10-17 10:30:00")

    # Import A pauses right after loading its state (i.e. while holding the
    # lock on T1) until import B is seen blocked on that lock.
    a_holds_lock = threading.Event()
    release_a = threading.Event()
    real_load = csv_validator.load_import_state

    def load_then_pause_in_a(session, rows):
        state = real_load(session, rows)
        if threading.current_thread().name == "import-a":
            a_holds_lock.set()
            release_a.wait(LOCK_WAIT_TIMEOUT_SECONDS)
        return state

    monkeypatch.setattr(csv_validator, "load_import_state", load_then_pause_in_a)

    exit_codes = {}

    def run(name, path):
        exit_codes[name] = csv_validator.main([str(path)])

    thread_a = threading.Thread(target=run, args=("a", file_a), name="import-a")
    thread_b = threading.Thread(target=run, args=("b", file_b), name="import-b")
    thread_a.start()
    assert a_holds_lock.wait(LOCK_WAIT_TIMEOUT_SECONDS)
    thread_b.start()
    try:
        wait_until_a_backend_waits_on_a_lock(engine)
    finally:
        release_a.set()
        thread_a.join(LOCK_WAIT_TIMEOUT_SECONDS)
        thread_b.join(LOCK_WAIT_TIMEOUT_SECONDS)

    assert exit_codes == {"a": 0, "b": 0}  # Both batches complete normally.
    with factory() as session:
        returns = session.scalars(
            select(Transaction.transaction_id).where(Transaction.quantity < 0)
        ).all()
        batch_b = session.scalars(
            select(ImportBatch).where(ImportBatch.filename == "b.csv")
        ).one()
        errors_b = session.execute(
            select(
                ImportRowError.row_number,
                ImportRowError.column_name,
                ImportRowError.message,
            ).where(ImportRowError.batch_id == batch_b.id)
        ).all()

    assert returns == ["R1"]
    assert (batch_b.rows_accepted, batch_b.rows_rejected) == (0, 1)
    assert errors_b == [
        (
            2,
            "quantity",
            (
                "Returning 1 would bring total returns to 2, "
                "more than the 1 originally sold."
            ),
        )
    ]
