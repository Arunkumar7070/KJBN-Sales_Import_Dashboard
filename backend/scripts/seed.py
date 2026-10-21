"""Load the demo data: the six stores and seven weeks of their sales.

The data goes through the same code as the API (store upsert, upload staging
and the import worker), so every business rule applies to it. Running the
script again changes nothing: stores are upserted, and the sales file has the
same hash, so it is reported as already uploaded.

The seed sales (seed_data/sales.csv, 1 Sep - 20 Oct 2026) use their own
transaction IDs (T-5000001 onwards), so the files in Sample_Data still import
as new when a reviewer uploads them.

Usage, from backend/ (migrate first):  python -m scripts.seed
"""

import sys
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.models import ImportBatch
from app.services.import_worker import enqueue_upload, run_worker
from app.services.store_import import import_stores

SEED_DATA_DIR = Path(__file__).resolve().parent.parent / "seed_data"
STORES_FILE = SEED_DATA_DIR / "stores.csv"
SALES_FILE = SEED_DATA_DIR / "sales.csv"


def seed_stores(session: Session, max_size_bytes: int) -> None:
    summary = import_stores(
        session, STORES_FILE.name, STORES_FILE.read_bytes(), max_size_bytes
    )
    print(
        f"Stores: {summary.inserted} inserted, {summary.updated} updated, "
        f"{summary.unchanged} unchanged, {len(summary.errors)} rejected."
    )


def stage_sales(session: Session, max_size_bytes: int) -> uuid.UUID | None:
    outcome = enqueue_upload(
        session, SALES_FILE.name, SALES_FILE.read_bytes(), max_size_bytes
    )
    print(f"Sales file: {outcome.status}: {outcome.detail}")
    return outcome.batch_id


def seed() -> bool:
    """Seed the database; True if the sales batch ends up imported."""
    max_size_bytes = get_settings().max_upload_size_bytes
    with SessionLocal() as session:
        seed_stores(session, max_size_bytes)
        batch_id = stage_sales(session, max_size_bytes)
    if batch_id is None:
        return False

    # Also finishes a seed batch that an earlier run left queued; an already
    # imported batch is not claimable, so this is then a no-op.
    run_worker(drain=True, batch_ids=[batch_id])
    with SessionLocal() as session:
        batch = session.get(ImportBatch, batch_id)
        print(
            f"Sales batch {batch_id}: {batch.status}, {batch.rows_accepted} accepted, "
            f"{batch.rows_rejected} rejected."
        )
        return batch.status == "COMPLETED"


if __name__ == "__main__":
    sys.exit(0 if seed() else 1)
