"""Import stores from a CSV file: insert new store codes, update existing ones.

The file must have exactly the columns code, name, city (any order). It goes
through the same file checks as sales CSVs (extension, size, UTF-8 with an
optional BOM, CSV syntax, header). Each data row needs all three values
(surrounding spaces are trimmed); bad rows, and repeats of a code earlier in
the file, are reported and skipped while the valid rows are still imported.

Valid rows are written with INSERT ... ON CONFLICT (code) DO UPDATE in one
transaction, so concurrent imports can never fail on a duplicate code, and
stores whose name and city are unchanged are not written at all.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from app.models import Store
from app.services.csv_validator import (
    WHOLE_ROW,
    CsvFileError,
    check_name_and_size,
    decode_utf8,
    fetch_where_in,
    find_header_problem,
    parse_records,
)

STORE_COLUMNS = ("code", "name", "city")
UPSERT_CHUNK_ROWS = 500  # 3 bind parameters per row stays far below any limit.


@dataclass(frozen=True)
class StoreRowError:
    row_number: int
    column_name: str
    message: str


@dataclass
class StoreImportSummary:
    rows_total: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    errors: list[StoreRowError] = field(default_factory=list)


def import_stores(
    session: Session, filename: str, content: bytes, max_size_bytes: int
) -> StoreImportSummary:
    """Validate the file and upsert its valid rows; commits.

    Raises CsvFileError if the file as a whole is unusable (nothing is written).
    """
    check_name_and_size(Path(filename), len(content), max_size_bytes)
    header, *records = parse_records(decode_utf8(content))
    header_problem = find_header_problem(header, required=STORE_COLUMNS)
    if header_problem:
        raise CsvFileError(header_problem)

    summary = StoreImportSummary()
    stores = valid_stores(header, records, summary)

    existing = {
        code: (name, city)
        for code, name, city in fetch_where_in(
            session, select(Store.code, Store.name, Store.city), Store.code, set(stores)
        )
    }
    changed = []
    for code, (name, city) in stores.items():
        if code not in existing:
            summary.inserted += 1
        elif existing[code] != (name, city):
            summary.updated += 1
        else:
            summary.unchanged += 1
            continue
        changed.append({"code": code, "name": name, "city": city})

    upsert_stores(session, changed)
    session.commit()
    return summary


def valid_stores(
    header: list[str], records: list[list[str]], summary: StoreImportSummary
) -> dict[str, tuple[str, str]]:
    """Map code -> (name, city) for valid rows; errors go into summary."""
    stores: dict[str, tuple[str, str]] = {}
    first_row_of: dict[str, int] = {}
    for row_number, record in enumerate(records, start=2):  # The header is row 1.
        if not record:
            continue  # Blank line.
        summary.rows_total += 1
        error = row_error(header, record, row_number, first_row_of)
        if error:
            summary.errors.append(error)
            continue
        values = {
            name: value.strip() for name, value in zip(header, record, strict=True)
        }
        stores[values["code"]] = (values["name"], values["city"])
        first_row_of[values["code"]] = row_number
    return stores


def row_error(
    header: list[str],
    record: list[str],
    row_number: int,
    first_row_of: dict[str, int],
) -> StoreRowError | None:
    if len(record) != len(header):
        return StoreRowError(
            row_number,
            WHOLE_ROW,
            f"Row has {len(record)} column(s); expected {len(header)}.",
        )
    values = {name: value.strip() for name, value in zip(header, record, strict=True)}
    missing = [name for name in STORE_COLUMNS if not values[name]]
    if missing:
        return StoreRowError(
            row_number, missing[0], f"Missing required value(s): {', '.join(missing)}."
        )
    code = values["code"]
    if code in first_row_of:
        return StoreRowError(
            row_number,
            "code",
            f"Store code '{code}' already appears in row {first_row_of[code]}.",
        )
    return None


def upsert_stores(session: Session, rows: Sequence[dict[str, str]]) -> None:
    dialect = session.get_bind().dialect.name
    insert = {"postgresql": postgresql.insert, "sqlite": sqlite.insert}[dialect]
    for start in range(0, len(rows), UPSERT_CHUNK_ROWS):
        statement = insert(Store).values(list(rows[start : start + UPSERT_CHUNK_ROWS]))
        session.execute(
            statement.on_conflict_do_update(
                index_elements=[Store.code],
                set_={"name": statement.excluded.name, "city": statement.excluded.city},
            )
        )
