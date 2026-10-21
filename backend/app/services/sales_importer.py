"""Database lookups, sales business-rule validation, and chunked batch inserts.

This module executes the core business logic of Problem 5:
- Two-pass chronological ordering (Sales pass first, then Returns pass).
- Idempotent duplicate checks (against DB and within file).
- Checking return quantities against original sales with locking.
- High-speed chunked batch inserts (1,000 rows/batch) to avoid network latency.
"""

import logging
import uuid
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import Table, func, insert, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import clock
from app.database import Base
from app.models import (
    ImportBatch,
    ImportRowError,
    Product,
    ProductAlias,
    Store,
    Transaction,
)
from app.services.csv_parser import CsvRow, FileCheckResult, RowError
from app.services.sales_rules import (
    SaleRow,
    SaleRuleViolation,
    line_total_paise,
    parse_quantity,
    parse_sale_row,
    parse_sold_at,
    validate_sale,
)

logger = logging.getLogger(__name__)

# Maximum parameters to fetch or insert in one SQL statement
LOOKUP_CHUNK_SIZE = 1000
INSERT_CHUNK_SIZE = 1000
MAX_WRITE_ATTEMPTS = 2
CONFLICT_SKIPPING_INSERTS = {"postgresql": postgresql_insert, "sqlite": sqlite_insert}


class ImportConflictError(Exception):
    """Raised if a concurrent import committed the exact same rows between planning and insertion."""


@dataclass
class ImportState:
    """Pre-loaded in-memory cache of relevant database entities for this file.
    
    Avoids N+1 database queries by pre-fetching all stores, transaction IDs,
    known products, and original sale records upfront in bulk.
    """
    existing_transaction_ids: set[str]
    store_codes: set[str]
    product_names: dict[str, str]       # sku -> canonical product name
    aliases: set[tuple[str, str]]       # (sku, alias_name)
    originals: dict[str, SaleRow]       # transaction_id -> SaleRow
    returned_quantity: Counter[str]     # original_transaction_id -> cumulative units returned
    accepted_row_numbers: dict[str, int] = field(default_factory=dict)

    def copy(self) -> "ImportState":
        """Creates an isolated clone of state so planning can run without mutating the original lookups."""
        return ImportState(
            existing_transaction_ids=set(self.existing_transaction_ids),
            store_codes=set(self.store_codes),
            product_names=dict(self.product_names),
            aliases=set(self.aliases),
            originals=dict(self.originals),
            returned_quantity=Counter(self.returned_quantity),
            accepted_row_numbers=dict(self.accepted_row_numbers),
        )


@dataclass
class ImportPlan:
    """Container holding all validated records ready to be written in bulk.
    
    Separates the validation decision from the database insert operation.
    """
    transactions: list[dict[str, object]] = field(default_factory=list)
    products: list[dict[str, str]] = field(default_factory=list)
    aliases: list[dict[str, str]] = field(default_factory=list)
    row_errors: list[RowError] = field(default_factory=list)


def process_batch(
    session: Session, batch: ImportBatch, result: FileCheckResult
) -> list[RowError]:
    """Top-level database coordinator for a batch.
    
    Executes row imports inside a single database transaction. If any database
    failure occurs, rolls back all changes, marks the batch as FAILED, and records
    the error so it can be retried cleanly.
    """
    try:
        accepted, errors = import_rows(session, batch, result, clock=clock.utc_now)
        batch.rows_total = accepted + len(errors)
        batch.rows_accepted = accepted
        batch.rows_rejected = len(errors)
        batch.status = "COMPLETED"
        batch.completed_at = clock.utc_now()
        session.commit()
        return errors
    except (SQLAlchemyError, ImportConflictError):
        session.rollback()
        logger.exception("Import of batch %s failed", batch.id)
        batch.status = "FAILED"
        batch.failure_reason = (
            "Database error while saving rows; nothing was saved. "
            "Upload the same file again to retry."
        )
        batch.completed_at = clock.utc_now()
        session.commit()
        return []


def import_rows(
    session: Session,
    batch: ImportBatch,
    result: FileCheckResult,
    clock: Callable[[], datetime],
) -> tuple[int, list[RowError]]:
    """Two-phase ingestion:
    
    1. Phase 1 (plan_import): Validates all rows in memory (CPU bound, <0.2s).
    2. Phase 2 (write_plan): Inserts products, aliases, and transactions in chunks of 1,000.
    
    If a concurrent process added one of the IDs during insertion, it rolls back
    to the savepoint, refreshes conflicting keys, and replans cleanly.
    """
    state = load_import_state(session, result.rows)
    for attempt in range(1, MAX_WRITE_ATTEMPTS + 1):
        plan = plan_import(result.rows, result.row_errors, state, batch.id, clock)
        savepoint = session.begin_nested()
        if write_plan(session, plan):
            savepoint.commit()
            break
        savepoint.rollback()
        if attempt == MAX_WRITE_ATTEMPTS:
            raise ImportConflictError(
                f"Rows of batch {batch.id} still clashed with another import."
            )
        refresh_conflicting_lookups(session, state, plan)

    # Insert rejected row error logs in chunks of 1,000
    insert_in_chunks(
        session,
        ImportRowError,
        [
            {
                "batch_id": batch.id,
                "row_number": error.row_number,
                "column_name": error.column_name,
                "message": error.message,
                "transaction_id": error.transaction_id,
            }
            for error in plan.row_errors
        ],
    )
    return len(plan.transactions), plan.row_errors


def plan_import(
    rows: Sequence[CsvRow],
    row_errors: Sequence[RowError],
    state: ImportState,
    batch_id: uuid.UUID,
    clock: Callable[[], datetime],
) -> ImportPlan:
    """Phase 1: In-memory business-rule validation.
    
    Runs every row through sales_rules.py without touching the database.
    Processes sales first so returns can immediately link to their original sale.
    """
    state = state.copy()
    plan = ImportPlan(row_errors=list(row_errors))
    for row in processing_order(rows):
        try:
            sale = accept_row(row.values, state, clock)
        except SaleRuleViolation as violation:
            plan.row_errors.append(
                RowError(
                    row.row_number,
                    violation.column_name,
                    violation.message,
                    transaction_id=row.values.get("transaction_id"),
                )
            )
            continue
        plan_row(plan, state, sale, row, batch_id)

    plan.row_errors.sort(key=lambda error: error.row_number)
    return plan


def plan_row(
    plan: ImportPlan,
    state: ImportState,
    sale: SaleRow,
    row: CsvRow,
    batch_id: uuid.UUID,
) -> None:
    """Staging helper for a single accepted row.
    
    Learns new product names, registers product aliases, and prepares the
    transaction dictionary ready for bulk database insertion.
    """
    product_name = row.values["product_name"]
    known_name = state.product_names.get(sale.sku)
    if known_name is None:
        plan.products.append({"sku": sale.sku, "name": product_name})
        state.product_names[sale.sku] = product_name
    elif product_name != known_name and (sale.sku, product_name) not in state.aliases:
        plan.aliases.append({"sku": sale.sku, "alias_name": product_name})
        state.aliases.add((sale.sku, product_name))

    plan.transactions.append(
        {
            "transaction_id": sale.transaction_id,
            "store_code": sale.store_code,
            "sold_at": sale.sold_at,
            "sku": sale.sku,
            "product_name": product_name,
            "quantity": sale.quantity,
            "unit_price_paise": sale.unit_price_paise,
            "discount_paise": sale.discount_paise,
            "payment_mode": sale.payment_mode,
            "original_transaction_id": sale.original_transaction_id,
            "batch_id": batch_id,
            "line_total_paise": line_total_paise(sale),
            "is_return": sale.quantity < 0,
        }
    )
    state.accepted_row_numbers[sale.transaction_id] = row.row_number
    state.originals[sale.transaction_id] = sale
    if sale.original_transaction_id:
        state.returned_quantity[sale.original_transaction_id] += -sale.quantity


def write_plan(session: Session, plan: ImportPlan) -> bool:
    """Phase 2: Writes products, aliases, and transactions in chunks of 1,000.
    
    Returns True if all rows inserted successfully. Returns False if a concurrent
    import inserted a conflicting ID, triggering a safe replan.
    """
    tables = (
        (Product, plan.products, ["sku"]),
        (ProductAlias, plan.aliases, ["sku", "alias_name"]),
        (Transaction, plan.transactions, ["transaction_id"]),
    )
    for model, rows, key_columns in tables:
        for chunk in chunked(rows):
            if insert_new_rows(session, model, chunk, key_columns) < len(chunk):
                return False
    return True


def insert_new_rows(
    session: Session, model: type[Base], rows: Sequence[dict], key_columns: list[str]
) -> int:
    """Executes a multi-row INSERT with ON CONFLICT DO NOTHING.
    
    Inserts an entire chunk (e.g. 1,000 rows) in a single database round-trip.
    Works seamlessly on both PostgreSQL and SQLite.
    """
    dialect = session.get_bind().dialect.name
    statement = (
        CONFLICT_SKIPPING_INSERTS[dialect](table_of(model))
        .values(list(rows))
        .on_conflict_do_nothing(index_elements=key_columns)
        .execution_options(preserve_rowcount=True)
    )
    return cast(CursorResult, session.execute(statement)).rowcount


def insert_in_chunks(session: Session, model: type[Base], rows: Sequence[dict]) -> None:
    """Utility to insert rows into a table in slices of INSERT_CHUNK_SIZE (1,000)."""
    for chunk in chunked(rows):
        session.execute(insert(table_of(model)).values(list(chunk)))


def table_of(model: type[Base]) -> Table:
    """Extracts the underlying SQLAlchemy Core Table from an ORM model."""
    return cast(Table, model.__table__)


def chunked[T](rows: Sequence[T]) -> Iterator[Sequence[T]]:
    """Yields successive slices of a list up to INSERT_CHUNK_SIZE (1,000)."""
    for start in range(0, len(rows), INSERT_CHUNK_SIZE):
        yield rows[start : start + INSERT_CHUNK_SIZE]


def refresh_conflicting_lookups(
    session: Session, state: ImportState, plan: ImportPlan
) -> None:
    """Re-queries database for keys that collided with a concurrent import."""
    planned_ids = {values["transaction_id"] for values in plan.transactions}
    planned_skus = {values["sku"] for values in [*plan.products, *plan.aliases]}
    state.existing_transaction_ids.update(
        transaction_id
        for (transaction_id,) in fetch_where_in(
            session,
            select(Transaction.transaction_id),
            Transaction.transaction_id,
            planned_ids,
        )
    )
    state.product_names.update(
        (sku, name)
        for sku, name in fetch_where_in(
            session, select(Product.sku, Product.name), Product.sku, planned_skus
        )
    )
    state.aliases.update(
        (sku, alias)
        for sku, alias in fetch_where_in(
            session,
            select(ProductAlias.sku, ProductAlias.alias_name),
            ProductAlias.sku,
            planned_skus,
        )
    )


def processing_order(rows: Sequence[CsvRow]) -> list[CsvRow]:
    """Orders rows in two passes: ALL sales first, then ALL returns.
    
    This ensures that even if a return row is physically placed above its
    original sale in the CSV, the original sale is validated first and the
    return can successfully find it.
    """
    sales_pass, returns_pass = [], []
    for row in rows:
        (returns_pass if is_return_row(row) else sales_pass).append(row)
    return sorted(sales_pass, key=chronological_key) + sorted(
        returns_pass, key=chronological_key
    )


def is_return_row(row: CsvRow) -> bool:
    """Checks whether a row has a negative quantity indicating a return."""
    try:
        return parse_quantity(row.values["quantity"]) < 0
    except SaleRuleViolation:
        return False


def chronological_key(row: CsvRow) -> tuple[bool, datetime, int]:
    """Sort key that orders transactions chronologically by sold_at timestamp."""
    try:
        return (False, parse_sold_at(row.values["sold_at"]), row.row_number)
    except SaleRuleViolation:
        return (True, datetime.max.replace(tzinfo=UTC), row.row_number)


def accept_row(
    values: dict[str, str], state: ImportState, clock: Callable[[], datetime]
) -> SaleRow:
    """Validates a single row against all business rules.
    
    Checks:
    - Duplicate ID within file
    - Duplicate ID already in database
    - Store code existence
    - Negative return checks against original sale
    - Cumulative return quantity limits
    """
    transaction_id = values["transaction_id"]
    if transaction_id in state.accepted_row_numbers:
        first_row = state.accepted_row_numbers[transaction_id]
        raise SaleRuleViolation(
            "DUPLICATE_IN_FILE",
            f"Transaction ID '{transaction_id}' already appears in row {first_row}.",
        )
    if transaction_id in state.existing_transaction_ids:
        raise SaleRuleViolation(
            "DUPLICATE_IN_DATABASE",
            f"Transaction ID '{transaction_id}' has already been imported.",
        )

    sale = parse_sale_row(values)
    if sale.store_code not in state.store_codes:
        raise SaleRuleViolation(
            "UNKNOWN_STORE", f"Store code '{sale.store_code}' does not exist."
        )
    original_id = sale.original_transaction_id
    validate_sale(
        sale,
        clock=clock,
        original=state.originals.get(original_id) if original_id else None,
        already_returned_quantity=state.returned_quantity[original_id]
        if original_id
        else 0,
    )
    return sale


def load_import_state(session: Session, rows: Sequence[CsvRow]) -> ImportState:
    """Pre-fetches all database entities needed to validate this file in bulk.
    
    Locks original sales using SELECT ... FOR UPDATE so concurrent imports cannot
    exceed remaining return quantities.
    """
    values = [row.values for row in rows]
    transaction_ids = {v["transaction_id"] for v in values}
    original_ids = {
        v["original_transaction_id"] for v in values if v.get("original_transaction_id")
    }
    skus = {v["sku"] for v in values}
    store_codes = {v["store_code"] for v in values}

    original_columns = select(
        Transaction.transaction_id,
        Transaction.store_code,
        Transaction.sku,
        Transaction.sold_at,
        Transaction.quantity,
        Transaction.unit_price_paise,
        Transaction.discount_paise,
        Transaction.payment_mode,
        Transaction.original_transaction_id,
    )
    returned_by_original = (
        select(Transaction.original_transaction_id, func.sum(-Transaction.quantity))
        .where(Transaction.quantity < 0)
        .group_by(Transaction.original_transaction_id)
    )

    originals = {
        row[0]: sale_row_from_database(row)
        for row in fetch_where_in(
            session,
            original_columns.order_by(Transaction.transaction_id).with_for_update(),
            Transaction.transaction_id,
            original_ids,
        )
    }
    returned_quantity = Counter(
        dict(
            fetch_where_in(
                session,
                returned_by_original,
                Transaction.original_transaction_id,
                original_ids,
            )
        )
    )

    return ImportState(
        existing_transaction_ids={
            transaction_id
            for (transaction_id,) in fetch_where_in(
                session,
                select(Transaction.transaction_id),
                Transaction.transaction_id,
                transaction_ids,
            )
        },
        store_codes={
            code
            for (code,) in fetch_where_in(
                session, select(Store.code), Store.code, store_codes
            )
        },
        product_names=dict(
            fetch_where_in(
                session, select(Product.sku, Product.name), Product.sku, skus
            )
        ),
        aliases={
            (sku, alias)
            for sku, alias in fetch_where_in(
                session,
                select(ProductAlias.sku, ProductAlias.alias_name),
                ProductAlias.sku,
                skus,
            )
        },
        originals=originals,
        returned_quantity=returned_quantity,
    )


def sale_row_from_database(row) -> SaleRow:
    """Converts a raw database tuple into a strongly-typed SaleRow dataclass."""
    sale = SaleRow(*row)
    if sale.sold_at.tzinfo is None:
        return replace(sale, sold_at=sale.sold_at.replace(tzinfo=UTC))
    return sale


def fetch_where_in(session: Session, statement, column, values: set[str]) -> list:
    """Executes a SELECT ... WHERE column IN (...) in slices of 1,000.
    
    Prevents exceeding PostgreSQL's limit on bind parameters.
    """
    ordered = sorted(values)
    results = []
    for start in range(0, len(ordered), LOOKUP_CHUNK_SIZE):
        chunk = ordered[start : start + LOOKUP_CHUNK_SIZE]
        results.extend(session.execute(statement.where(column.in_(chunk))).all())
    return results