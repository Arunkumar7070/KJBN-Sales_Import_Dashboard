import gzip
import hashlib
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import BigInteger, create_engine, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.database import Base
from app.models import (
    ImportBatch,
    ImportPayload,
    ImportRowError,
    Product,
    ProductAlias,
    Store,
    Transaction,
)
from app.services import csv_validator
from app.services.csv_validator import (
    CsvRow,
    RowError,
    check_csv_file,
    enqueue_upload,
    main,
    processing_order,
    register_import_batch,
    run_next_batch,
    run_worker,
)

MAX_SIZE_BYTES = 1000
UTF8_BOM = b"\xef\xbb\xbf"

# Written out literally (not imported) so a change to the spec breaks the tests.
HEADER = [
    "transaction_id",
    "store_code",
    "sold_at",
    "sku",
    "product_name",
    "quantity",
    "unit_price",
    "discount",
    "payment_mode",
    "original_transaction_id",
]
ROW = [
    "T1",
    "ST01",
    "2026-10-17 09:00:00",
    "SKU1",
    "Masala Chai",
    "2",
    "100.00",
    "0.00",
    "UPI",
    "",
]
EXPECTED_ROW = dict(zip(HEADER, ROW, strict=True))
# 17:30 IST on the same day as ROW's sale, so every test row is in the past.
FIXED_NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


@compiles(BigInteger, "sqlite")
def bigint_as_sqlite_integer(type_, compiler, **kwargs):
    # Only "INTEGER PRIMARY KEY" auto-increments in SQLite (PostgreSQL uses
    # BIGSERIAL). SQLite INTEGER is 64-bit, so money values are unaffected.
    return "INTEGER"


@pytest.fixture(autouse=True)
def session_factory(monkeypatch):
    """Point the validator at a throwaway SQLite database; Neon is never touched."""
    # autocommit=False makes sqlite3 handle SAVEPOINTs the way SQLAlchemy expects.
    engine = create_engine("sqlite://", connect_args={"autocommit": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        session.add_all(
            [
                Store(code="ST01", name="Store One", city="Chennai"),
                Store(code="ST02", name="Store Two", city="Madurai"),
            ]
        )
        session.commit()
    monkeypatch.setattr(csv_validator, "SessionLocal", factory)
    monkeypatch.setattr(csv_validator, "utc_now", lambda: FIXED_NOW)
    yield factory
    engine.dispose()


def all_batches(session_factory):
    with session_factory() as session:
        return session.scalars(select(ImportBatch)).all()


def only_batch(session_factory):
    batches = all_batches(session_factory)
    assert len(batches) == 1
    return batches[0]


def csv_bytes(header=HEADER, rows=(ROW,), newline="\n"):
    lines = [",".join(header), *(",".join(row) for row in rows)]
    return (newline.join(lines) + newline).encode()


VALID_CONTENT = csv_bytes()
HEADER_LINE = csv_bytes(rows=())


def write_file(directory, name, content=VALID_CONTENT):
    path = directory / name
    path.write_bytes(content)
    return path


def check(directory, content, max_size_bytes=MAX_SIZE_BYTES):
    return check_csv_file(write_file(directory, "sales.csv", content), max_size_bytes)


def assert_invalid(result, expected_message):
    assert result.is_valid is False
    assert result.message == expected_message
    assert result.rows == ()
    assert result.sha256 is None


# Basic file checks


@pytest.mark.parametrize("name", ["sales.csv", "SALES.CSV"])
def test_valid_csv_passes(tmp_path, name):
    path = write_file(tmp_path, name)

    result = check_csv_file(path, MAX_SIZE_BYTES)

    assert result.is_valid is True
    assert result.message == "File passed all checks."
    assert result.path == path


def test_file_exactly_at_size_limit_passes(tmp_path):
    assert check(tmp_path, VALID_CONTENT, len(VALID_CONTENT)).is_valid is True


def test_file_one_byte_over_limit_fails(tmp_path):
    limit = len(VALID_CONTENT) - 1

    assert_invalid(
        check(tmp_path, VALID_CONTENT, limit),
        f"File is {limit + 1} bytes, which exceeds the {limit}-byte limit.",
    )


def test_missing_file_fails(tmp_path):
    result = check_csv_file(tmp_path / "missing.csv", MAX_SIZE_BYTES)

    assert_invalid(result, "File does not exist.")


def test_directory_fails(tmp_path):
    directory = tmp_path / "folder.csv"
    directory.mkdir()

    assert_invalid(
        check_csv_file(directory, MAX_SIZE_BYTES), "Path is not a regular file."
    )


@pytest.mark.parametrize("name", ["sales.txt", "sales.csv.bak", "sales"])
def test_wrong_extension_fails(tmp_path, name):
    path = write_file(tmp_path, name)

    assert_invalid(
        check_csv_file(path, MAX_SIZE_BYTES), "File must have a .csv extension."
    )


def test_empty_file_fails(tmp_path):
    assert_invalid(check(tmp_path, b""), "File is empty.")


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root can read files regardless of permissions",
)
def test_unreadable_file_fails(tmp_path):
    path = write_file(tmp_path, "sales.csv")
    path.chmod(0o000)
    try:
        result = check_csv_file(path, MAX_SIZE_BYTES)
    finally:
        path.chmod(0o600)

    assert_invalid(result, "File cannot be read (Permission denied).")


def test_size_limit_setting_has_default_and_rejects_non_positive_values():
    assert Settings(database_url="sqlite://", _env_file=None).max_upload_size_bytes == (
        10 * 1024 * 1024
    )
    with pytest.raises(ValueError):
        Settings(database_url="sqlite://", max_upload_size_bytes=0, _env_file=None)


def test_size_limit_is_checked_before_encoding(tmp_path):
    assert_invalid(
        check(tmp_path, b"\xff" * 11, max_size_bytes=10),
        "File is 11 bytes, which exceeds the 10-byte limit.",
    )


# Encoding


def test_utf8_with_non_ascii_text_passes(tmp_path):
    row = [*ROW[:4], "Masala Chai ☕ ₹20", *ROW[5:]]

    result = check(tmp_path, csv_bytes(rows=[row]))

    assert result.is_valid is True
    assert result.rows[0].values["product_name"] == "Masala Chai ☕ ₹20"


def test_utf8_with_bom_passes_and_bom_is_not_part_of_first_header(tmp_path):
    result = check(tmp_path, UTF8_BOM + VALID_CONTENT)

    assert result.is_valid is True
    assert result.rows == (CsvRow(2, EXPECTED_ROW),)


def test_file_containing_only_a_bom_is_empty(tmp_path):
    assert_invalid(check(tmp_path, UTF8_BOM), "File is empty.")


@pytest.mark.parametrize(
    ("content", "bad_byte_position"),
    [
        # A byte that never appears in UTF-8.
        (HEADER_LINE + b"T1,\xff\n", len(HEADER_LINE) + 3),
        # Latin-1 export, e.g. from Excel.
        (HEADER_LINE + "T1,café\n".encode("latin-1"), len(HEADER_LINE) + 6),
        # The position counts the BOM bytes.
        (UTF8_BOM + HEADER_LINE + b"\xff", 3 + len(HEADER_LINE)),
    ],
    ids=["invalid-byte", "latin-1", "after-bom"],
)
def test_invalid_utf8_fails(tmp_path, content, bad_byte_position):
    assert_invalid(
        check(tmp_path, content),
        f"File is not valid UTF-8 (invalid byte at position {bad_byte_position}).",
    )


# Line endings and CSV syntax


@pytest.mark.parametrize(
    "content",
    [
        csv_bytes(newline="\n"),
        csv_bytes(newline="\r\n"),
        csv_bytes(newline="\r\n").removesuffix(b"\r\n"),
        UTF8_BOM + csv_bytes(newline="\r\n"),
    ],
    ids=["lf", "crlf", "crlf-no-final-newline", "bom-crlf"],
)
def test_lf_and_crlf_line_endings_pass(tmp_path, content):
    result = check(tmp_path, content)

    assert result.is_valid is True
    assert result.rows == (CsvRow(2, EXPECTED_ROW),)


def test_well_formed_quoting_passes(tmp_path):
    row = [*ROW[:4], '"comma, ""quotes"" and\r\nnewline"', *ROW[5:]]

    result = check(tmp_path, csv_bytes(rows=[row], newline="\r\n"))

    assert result.is_valid is True
    assert result.rows[0].values["product_name"] == 'comma, "quotes" and\r\nnewline'


@pytest.mark.parametrize(
    "bad_product_name",
    ['"unterminated', '"closed"extra'],
    ids=["unclosed-quote", "text-after-closing-quote"],
)
def test_malformed_quoting_fails(tmp_path, bad_product_name):
    row = [*ROW[:4], bad_product_name, *ROW[5:]]

    result = check(tmp_path, csv_bytes(rows=[row]))

    assert result.is_valid is False
    assert result.message.startswith("Malformed CSV near line 2: ")
    assert result.rows == ()


# Header


def test_valid_header_maps_rows_by_name(tmp_path):
    result = check(tmp_path, VALID_CONTENT)

    assert result.rows == (CsvRow(2, EXPECTED_ROW),)
    assert result.rows[0].values["original_transaction_id"] == ""


def test_reordered_columns_map_values_by_header_name(tmp_path):
    reordered = csv_bytes(header=HEADER[::-1], rows=[ROW[::-1]])

    result = check(tmp_path, reordered)

    assert result.is_valid is True
    assert result.rows == (CsvRow(2, EXPECTED_ROW),)


def test_header_only_file_is_valid_with_no_rows(tmp_path):
    result = check(tmp_path, HEADER_LINE)

    assert result.is_valid is True
    assert result.rows == ()


def test_blank_first_line_means_header_is_missing(tmp_path):
    assert_invalid(check(tmp_path, b"\r\n" + VALID_CONTENT), "Header row is missing.")


def test_missing_column_is_rejected(tmp_path):
    header = [name for name in HEADER if name != "product_name"]

    assert_invalid(
        check(tmp_path, csv_bytes(header=header, rows=())),
        "Invalid header: missing column(s): product_name.",
    )


def test_extra_column_is_rejected(tmp_path):
    assert_invalid(
        check(tmp_path, csv_bytes(header=[*HEADER, "customer_id"], rows=())),
        "Invalid header: unexpected column(s): 'customer_id'.",
    )


def test_duplicate_column_is_rejected(tmp_path):
    assert_invalid(
        check(tmp_path, csv_bytes(header=[*HEADER, "sku"], rows=())),
        "Invalid header: duplicate column(s): sku.",
    )


def header_with_sku_named(wrong_name):
    return [wrong_name if name == "sku" else name for name in HEADER]


@pytest.mark.parametrize(
    "wrong_name",
    ["SKU", "Sku", " sku", "sku "],
    ids=["upper-case", "title-case", "leading-space", "trailing-space"],
)
def test_wrong_case_or_spacing_is_rejected_with_hint(tmp_path, wrong_name):
    header = header_with_sku_named(wrong_name)

    assert_invalid(
        check(tmp_path, csv_bytes(header=header, rows=())),
        "Invalid header: missing column(s): sku; "
        f"unexpected column(s): '{wrong_name}' (expected 'sku').",
    )


def test_misspelled_column_is_rejected(tmp_path):
    header = header_with_sku_named("skus")

    assert_invalid(
        check(tmp_path, csv_bytes(header=header, rows=())),
        "Invalid header: missing column(s): sku; unexpected column(s): 'skus'.",
    )


def test_blank_column_name_is_rejected(tmp_path):
    header = ["" if name == "discount" else name for name in HEADER]

    assert_invalid(
        check(tmp_path, csv_bytes(header=header, rows=())),
        "Invalid header: missing column(s): discount; "
        "blank column name(s) at position(s): 8.",
    )


def test_trailing_comma_in_header_is_a_blank_column(tmp_path):
    assert_invalid(
        check(tmp_path, csv_bytes(header=[*HEADER, ""], rows=())),
        "Invalid header: blank column name(s) at position(s): 11.",
    )


def test_bad_header_rejects_whole_file_even_with_valid_rows(tmp_path):
    header = ["SKU" if name == "sku" else name for name in HEADER]

    result = check(tmp_path, csv_bytes(header=header, rows=[ROW, ROW]))

    assert result.is_valid is False
    assert result.rows == ()


# SHA-256 hash


def test_valid_file_hash_matches_original_bytes(tmp_path):
    result = check(tmp_path, VALID_CONTENT)

    assert result.sha256 == hashlib.sha256(VALID_CONTENT).hexdigest()


def test_hash_covers_bom_bytes(tmp_path):
    with_bom = UTF8_BOM + VALID_CONTENT

    result = check(tmp_path, with_bom)

    assert result.sha256 == hashlib.sha256(with_bom).hexdigest()
    assert result.sha256 != hashlib.sha256(VALID_CONTENT).hexdigest()


def test_hash_differs_for_crlf_and_lf_versions(tmp_path):
    lf = check(tmp_path, csv_bytes(newline="\n"))
    crlf = check(tmp_path, csv_bytes(newline="\r\n"))

    assert lf.rows == crlf.rows
    assert lf.sha256 != crlf.sha256


# Command line


def test_cli_prints_hash_for_valid_file(tmp_path, capsys, session_factory):
    path = write_file(tmp_path, "sales.csv")

    exit_code = main([str(path)])

    output = capsys.readouterr()
    batch = only_batch(session_factory)
    assert exit_code == 0
    assert output.out == (
        f"{hashlib.sha256(VALID_CONTENT).hexdigest()}  {path}\n"
        f"Batch {batch.id} COMPLETED: 1 rows, 1 accepted, 0 rejected.\n"
    )
    assert output.err == ""


@pytest.mark.parametrize(
    ("name", "content", "expected_message"),
    [
        ("sales.txt", VALID_CONTENT, "File must have a .csv extension."),
        ("sales.csv", b"", "File is empty."),
        ("sales.csv", HEADER_LINE + b"\xff", "File is not valid UTF-8"),
        ("sales.csv", HEADER_LINE + b'"unterminated\n', "Malformed CSV near line 2"),
        ("sales.csv", csv_bytes(header=HEADER[:-1]), "Invalid header"),
    ],
    ids=["extension", "empty", "encoding", "syntax", "header"],
)
def test_cli_rejects_invalid_file_without_hash(
    tmp_path, capsys, name, content, expected_message
):
    path = write_file(tmp_path, name, content)

    exit_code = main([str(path)])

    output = capsys.readouterr()
    assert exit_code == 1
    assert output.out == ""
    assert output.err.startswith(f"INVALID {path}: {expected_message}")


def test_cli_rejects_missing_file(tmp_path, capsys):
    path = tmp_path / "missing.csv"

    assert main([str(path)]) == 1
    assert capsys.readouterr().err == f"INVALID {path}: File does not exist.\n"


def test_cli_requires_at_least_one_path():
    with pytest.raises(SystemExit) as excinfo:
        main([])

    assert excinfo.value.code == 2


# Import batch registration

HASH_A = "a" * 64
HASH_B = "b" * 64


def test_new_hash_creates_queued_batch(session_factory):
    with session_factory() as session:
        registration = register_import_batch(session, "sales.csv", HASH_A)
        session.commit()

    batch = only_batch(session_factory)
    assert registration.is_new is True
    assert registration.batch.id == batch.id
    assert (batch.filename, batch.file_sha256, batch.status) == (
        "sales.csv",
        HASH_A,
        "QUEUED",
    )


def test_same_hash_returns_existing_batch_without_creating_another(session_factory):
    with session_factory() as session:
        first = register_import_batch(session, "sales.csv", HASH_A)
        session.commit()

    with session_factory() as session:
        second = register_import_batch(session, "renamed copy.csv", HASH_A)
        session.commit()

    batch = only_batch(session_factory)
    assert second.is_new is False
    assert second.batch.id == first.batch.id == batch.id
    assert batch.filename == "sales.csv"


def test_session_stays_usable_after_a_duplicate(session_factory):
    with session_factory() as session:
        register_import_batch(session, "a.csv", HASH_A)
        duplicate = register_import_batch(session, "a-again.csv", HASH_A)
        other = register_import_batch(session, "b.csv", HASH_B)
        session.commit()

    assert duplicate.is_new is False
    assert other.is_new is True
    assert sorted(b.file_sha256 for b in all_batches(session_factory)) == [
        HASH_A,
        HASH_B,
    ]


def test_other_integrity_errors_are_not_reported_as_duplicates(session_factory):
    with session_factory() as session, pytest.raises(IntegrityError):
        register_import_batch(session, None, HASH_A)

    assert all_batches(session_factory) == []


# Command line + database


def test_cli_reports_already_uploaded_file(tmp_path, capsys, session_factory):
    original = write_file(tmp_path, "sales.csv")
    copy = write_file(tmp_path, "copy.csv")
    assert main([str(original)]) == 0
    batch = only_batch(session_factory)
    capsys.readouterr()

    exit_code = main([str(copy)])

    output = capsys.readouterr()
    assert exit_code == 1
    assert output.err == (
        f"ALREADY UPLOADED {copy}: this file was already uploaded as "
        f"'sales.csv' (batch {batch.id}, status COMPLETED).\n"
    )
    assert only_batch(session_factory).id == batch.id


def test_cli_creates_no_batch_for_invalid_file(tmp_path, session_factory):
    path = write_file(tmp_path, "sales.csv", csv_bytes(header=HEADER[:-1]))

    assert main([str(path)]) == 1
    assert all_batches(session_factory) == []


def test_cli_reports_database_failure_cleanly(tmp_path, capsys, monkeypatch):
    def unavailable():
        raise OperationalError("connect", {}, Exception("password=secret"))

    monkeypatch.setattr(csv_validator, "SessionLocal", unavailable)
    path = write_file(tmp_path, "sales.csv")

    exit_code = main([str(path)])

    err = capsys.readouterr().err
    assert exit_code == 1
    assert err == (f"ERROR {path}: could not import the file (database unavailable).\n")


# Row-by-row validation

REQUIRED_FIELDS = [name for name in HEADER if name != "original_transaction_id"]


def row_with(**changes):
    return [changes.get(name, value) for name, value in zip(HEADER, ROW, strict=True)]


def check_rows(tmp_path, rows, header=HEADER):
    result = check(tmp_path, csv_bytes(header=header, rows=rows))
    assert result.is_valid is True  # Row problems never fail the whole file.
    return result


def test_valid_row_is_kept_with_its_csv_row_number(tmp_path):
    result = check_rows(tmp_path, [ROW])

    assert result.rows == (CsvRow(2, EXPECTED_ROW),)
    assert result.row_errors == ()


def test_empty_original_transaction_id_is_allowed_for_normal_sale(tmp_path):
    result = check_rows(tmp_path, [row_with(original_transaction_id="")])

    assert result.row_errors == ()
    assert result.rows[0].values["original_transaction_id"] == ""


def test_filled_original_transaction_id_is_kept(tmp_path):
    result = check_rows(tmp_path, [row_with(original_transaction_id="T0")])

    assert result.rows[0].values["original_transaction_id"] == "T0"


@pytest.mark.parametrize(
    "field_count",
    [1, 9, 11, 12],
    ids=["one-field", "one-too-few", "one-too-many", "two-too-many"],
)
def test_row_without_exactly_ten_fields_is_rejected(tmp_path, field_count):
    record = (ROW + ["extra", "extra"])[:field_count]

    result = check_rows(tmp_path, [record])

    assert result.rows == ()
    assert result.row_errors == (
        RowError(2, "__row__", f"Row has {field_count} column(s); expected 10."),
    )


@pytest.mark.parametrize("field", REQUIRED_FIELDS)
def test_each_empty_required_field_is_rejected(tmp_path, field):
    result = check_rows(tmp_path, [row_with(**{field: ""})])

    assert result.rows == ()
    assert result.row_errors == (
        RowError(
            2,
            field,
            f"Missing required value(s): {field}.",
            transaction_id=None if field == "transaction_id" else "T1",
        ),
    )


@pytest.mark.parametrize("blank", [" ", "   ", "\t"], ids=["space", "spaces", "tab"])
def test_whitespace_only_required_value_counts_as_empty(tmp_path, blank):
    result = check_rows(tmp_path, [row_with(sku=blank)])

    assert result.row_errors == (
        RowError(2, "sku", "Missing required value(s): sku.", transaction_id="T1"),
    )


def test_all_missing_fields_are_listed_and_first_is_the_column(tmp_path):
    result = check_rows(tmp_path, [row_with(quantity="", sku="", payment_mode="")])

    assert result.row_errors == (
        RowError(
            2,
            "sku",
            "Missing required value(s): sku, quantity, payment_mode.",
            transaction_id="T1",
        ),
    )


def test_completely_empty_values_row_is_rejected(tmp_path):
    result = check_rows(tmp_path, [[""] * 10])

    error = result.row_errors[0]
    assert (error.row_number, error.column_name, error.transaction_id) == (
        2,
        "transaction_id",
        None,
    )
    assert error.message == f"Missing required value(s): {', '.join(REQUIRED_FIELDS)}."


def test_processing_continues_after_invalid_rows(tmp_path):
    rows = [
        row_with(transaction_id="T1"),  # row 2: valid
        row_with(transaction_id="T2", sku=""),  # row 3: missing sku
        ROW[:9],  # row 4: 9 fields
        row_with(transaction_id="T5"),  # row 5: valid
        row_with(transaction_id="T6", store_code=""),  # row 6: missing store
    ]

    result = check_rows(tmp_path, rows)

    assert [(row.row_number, row.values["transaction_id"]) for row in result.rows] == [
        (2, "T1"),
        (5, "T5"),
    ]
    assert [(e.row_number, e.column_name) for e in result.row_errors] == [
        (3, "sku"),
        (4, "__row__"),
        (6, "store_code"),
    ]


def test_file_with_only_invalid_rows_is_still_a_valid_file(tmp_path):
    result = check_rows(tmp_path, [row_with(sku=""), ROW[:3]])

    assert result.rows == ()
    assert len(result.row_errors) == 2
    assert result.sha256 is not None


def test_blank_lines_are_skipped_but_keep_row_numbering(tmp_path):
    content = HEADER_LINE + b"\n" + csv_bytes(header=[], rows=[ROW]).lstrip(b"\n")

    result = check(tmp_path, content)

    assert result.rows == (CsvRow(3, EXPECTED_ROW),)
    assert result.row_errors == ()


def test_row_numbers_count_records_not_lines_in_multiline_fields(tmp_path):
    multiline = row_with(transaction_id="T1", product_name='"Masala\nChai"')
    broken = row_with(transaction_id="T2", sku="")

    result = check_rows(tmp_path, [multiline, broken])

    assert result.rows[0].row_number == 2
    assert result.rows[0].values["product_name"] == "Masala\nChai"
    assert result.row_errors[0].row_number == 3


def test_missing_value_is_found_by_name_in_reordered_columns(tmp_path):
    reordered_row = row_with(sku="")[::-1]

    result = check_rows(tmp_path, [reordered_row], header=HEADER[::-1])

    assert result.row_errors == (
        RowError(2, "sku", "Missing required value(s): sku.", transaction_id="T1"),
    )


def test_cli_lists_invalid_rows_and_still_imports_valid_ones(
    tmp_path, capsys, session_factory
):
    rows = [ROW, row_with(transaction_id="T2", sku=""), ROW[:9]]
    path = write_file(tmp_path, "sales.csv", csv_bytes(rows=rows))

    exit_code = main([str(path)])

    out = capsys.readouterr().out.splitlines()
    batch = only_batch(session_factory)
    assert exit_code == 0
    assert out[1:] == [
        f"Batch {batch.id} COMPLETED: 3 rows, 1 accepted, 2 rejected.",
        "Row 3 [sku]: Missing required value(s): sku.",
        "Row 4 [__row__]: Row has 9 column(s); expected 10.",
    ]


# Importing rows into the database


def import_csv(tmp_path, rows, name="sales.csv"):
    path = write_file(tmp_path, name, csv_bytes(rows=rows))
    return main([str(path)])


def batch_named(session_factory, filename):
    with session_factory() as session:
        return session.scalars(
            select(ImportBatch).where(ImportBatch.filename == filename)
        ).one()


def saved_transactions(session_factory):
    with session_factory() as session:
        return {t.transaction_id: t for t in session.scalars(select(Transaction))}


def saved_row_errors(session_factory, batch):
    with session_factory() as session:
        errors = session.scalars(
            select(ImportRowError)
            .where(ImportRowError.batch_id == batch.id)
            .order_by(ImportRowError.row_number)
        )
        return [
            (e.row_number, e.column_name, e.message, e.transaction_id) for e in errors
        ]


def saved_products(session_factory):
    with session_factory() as session:
        products = {p.sku: p.name for p in session.scalars(select(Product))}
        aliases = sorted(
            (a.sku, a.alias_name) for a in session.scalars(select(ProductAlias))
        )
        return products, aliases


def naive_utc(value):
    # SQLite drops tzinfo on read; the stored value is UTC.
    return value.replace(tzinfo=None)


def test_valid_sale_is_saved_with_converted_values(tmp_path, session_factory):
    row = row_with(unit_price="99.99", discount="12.50")

    assert import_csv(tmp_path, [row]) == 0

    saved = saved_transactions(session_factory)["T1"]
    batch = only_batch(session_factory)
    assert naive_utc(saved.sold_at) == datetime(2026, 10, 17, 3, 30)  # 09:00 IST
    assert (saved.quantity, saved.unit_price_paise, saved.discount_paise) == (
        2,
        9999,
        1250,
    )
    assert saved.line_total_paise == 2 * 9999 - 1250
    assert saved.is_return is False
    assert (saved.store_code, saved.sku, saved.product_name) == (
        "ST01",
        "SKU1",
        "Masala Chai",
    )
    assert (saved.payment_mode, saved.original_transaction_id) == ("UPI", None)
    assert saved.batch_id == batch.id


def test_batch_counts_and_status_after_import(tmp_path, session_factory):
    rows = [
        row_with(transaction_id="T1"),
        row_with(transaction_id="T2", payment_mode="CHEQUE"),
        ROW[:9],
        row_with(transaction_id="T4"),
    ]

    assert import_csv(tmp_path, rows) == 0

    batch = only_batch(session_factory)
    assert batch.status == "COMPLETED"
    assert (batch.rows_total, batch.rows_accepted, batch.rows_rejected) == (4, 2, 2)
    assert batch.attempt_count == 1
    assert naive_utc(batch.started_at) == naive_utc(FIXED_NOW)
    assert naive_utc(batch.completed_at) == naive_utc(FIXED_NOW)
    assert batch.failure_reason is None
    assert sorted(saved_transactions(session_factory)) == ["T1", "T4"]


def test_rejected_rows_are_saved_in_import_row_errors(tmp_path, session_factory):
    rows = [
        ROW,
        row_with(transaction_id="T2", sku=""),
        ROW[:9],
        row_with(transaction_id="T4", quantity="0"),
    ]

    import_csv(tmp_path, rows)

    assert saved_row_errors(session_factory, only_batch(session_factory)) == [
        (3, "sku", "Missing required value(s): sku.", "T2"),
        (4, "__row__", "Row has 9 column(s); expected 10.", None),
        (5, "quantity", "quantity must not be zero.", "T4"),
    ]


def test_all_rows_rejected_still_completes_batch(tmp_path, session_factory):
    import_csv(tmp_path, [row_with(store_code="NOPE")])

    batch = only_batch(session_factory)
    assert batch.status == "COMPLETED"
    assert (batch.rows_total, batch.rows_accepted, batch.rows_rejected) == (1, 0, 1)
    assert saved_transactions(session_factory) == {}


# Duplicate transaction IDs


def test_duplicate_id_in_file_keeps_first_and_rejects_later(tmp_path, session_factory):
    rows = [
        row_with(transaction_id="T1", quantity="2"),
        row_with(transaction_id="T1", quantity="7"),
        row_with(transaction_id="T1", quantity="9"),
    ]

    import_csv(tmp_path, rows)

    saved = saved_transactions(session_factory)
    assert list(saved) == ["T1"]
    assert saved["T1"].quantity == 2
    duplicate_message = "Transaction ID 'T1' already appears in row 2."
    assert saved_row_errors(session_factory, only_batch(session_factory)) == [
        (3, "transaction_id", duplicate_message, "T1"),
        (4, "transaction_id", duplicate_message, "T1"),
    ]


def test_later_copy_is_accepted_when_first_copy_was_rejected(tmp_path, session_factory):
    rows = [
        row_with(transaction_id="T1", unit_price="abc"),
        row_with(transaction_id="T1", unit_price="100.00"),
    ]

    import_csv(tmp_path, rows)

    assert saved_transactions(session_factory)["T1"].unit_price_paise == 10000
    errors = saved_row_errors(session_factory, only_batch(session_factory))
    assert [(row, column) for row, column, _, _ in errors] == [(2, "unit_price")]


def test_id_already_in_database_is_rejected(tmp_path, session_factory):
    import_csv(tmp_path, [row_with(transaction_id="T1")], name="day1.csv")

    import_csv(
        tmp_path,
        [row_with(transaction_id="T1", quantity="5"), row_with(transaction_id="T2")],
        name="day2.csv",
    )

    day2 = batch_named(session_factory, "day2.csv")
    saved = saved_transactions(session_factory)
    assert saved["T1"].quantity == 2  # The original from day1 is untouched.
    assert saved["T1"].batch_id == batch_named(session_factory, "day1.csv").id
    assert saved["T2"].batch_id == day2.id
    assert saved_row_errors(session_factory, day2) == [
        (2, "transaction_id", "Transaction ID 'T1' has already been imported.", "T1")
    ]
    assert (day2.rows_accepted, day2.rows_rejected) == (1, 1)


# Sales rules applied during import


@pytest.mark.parametrize(
    ("changes", "column", "message"),
    [
        (
            {"sold_at": "2026-10-17T09:00:00"},
            "sold_at",
            "sold_at must be YYYY-MM-DD HH:MM:SS (IST); got '2026-10-17T09:00:00'.",
        ),
        ({"sold_at": "2026-10-17 17:30:01"}, "sold_at", "sold_at is in the future."),
        (
            {"quantity": "1.5"},
            "quantity",
            "quantity must be a whole number; got '1.5'.",
        ),
        ({"unit_price": "-1"}, "unit_price", "unit_price must not be negative."),
        (
            {"discount": "200.01"},
            "discount",
            "discount (₹200.01) exceeds the line amount (₹200.00).",
        ),
        (
            {"payment_mode": "upi"},
            "payment_mode",
            "payment_mode must be CASH, UPI or CARD; got 'upi'.",
        ),
        ({"store_code": "ST99"}, "store_code", "Store code 'ST99' does not exist."),
        (
            {"original_transaction_id": "T0"},
            "original_transaction_id",
            "Only returns (negative quantity) may set original_transaction_id.",
        ),
    ],
    ids=[
        "bad-timestamp-format",
        "future-timestamp",
        "non-integer-quantity",
        "negative-price",
        "discount-over-amount",
        "lowercase-payment-mode",
        "unknown-store",
        "sale-with-original",
    ],
)
def test_rule_violations_are_rejected_with_column_and_message(
    tmp_path, session_factory, changes, column, message
):
    import_csv(tmp_path, [row_with(**changes)])

    assert saved_transactions(session_factory) == {}
    assert saved_row_errors(session_factory, only_batch(session_factory)) == [
        (2, column, message, "T1")
    ]


@pytest.mark.parametrize(
    "changes",
    [
        {"sold_at": "2026-10-17 17:30:00"},  # exactly now in IST
        {"discount": "200.00"},  # exactly the line amount
        {"discount": "0"},
        {"unit_price": "0"},
    ],
    ids=["sold-at-now", "max-discount", "zero-discount", "free-item"],
)
def test_boundary_values_are_accepted(tmp_path, session_factory, changes):
    import_csv(tmp_path, [row_with(**changes)])

    assert list(saved_transactions(session_factory)) == ["T1"]


# An hour after ROW's 09:00 sale, so returns pass the strictly-after rule.
LATER = "2026-10-17 10:00:00"


def test_return_of_sale_in_same_file_is_saved(tmp_path, session_factory):
    rows = [
        row_with(transaction_id="T1", quantity="5"),
        row_with(
            transaction_id="R1",
            sold_at=LATER,
            quantity="-2",
            original_transaction_id="T1",
        ),
    ]

    import_csv(tmp_path, rows)

    saved = saved_transactions(session_factory)["R1"]
    assert saved.is_return is True
    assert saved.original_transaction_id == "T1"
    assert saved.line_total_paise == -2 * 10000


OVER_RETURN_MESSAGE = (
    "Returning 1 would bring total returns to 6, more than the 5 originally sold."
)


def test_cumulative_returns_across_files_cannot_exceed_sale(tmp_path, session_factory):
    import_csv(
        tmp_path,
        [
            row_with(transaction_id="T1", quantity="5"),
            row_with(
                transaction_id="R1",
                sold_at=LATER,
                quantity="-3",
                original_transaction_id="T1",
            ),
        ],
        name="day1.csv",
    )

    import_csv(
        tmp_path,
        [
            row_with(
                transaction_id="R2",
                sold_at=LATER,
                quantity="-2",
                original_transaction_id="T1",
            ),
            row_with(
                transaction_id="R3",
                sold_at=LATER,
                quantity="-1",
                original_transaction_id="T1",
            ),
        ],
        name="day2.csv",
    )

    assert "R2" in saved_transactions(session_factory)  # 3 + 2 = 5, exactly the sale
    assert saved_row_errors(
        session_factory, batch_named(session_factory, "day2.csv")
    ) == [
        (
            3,
            "quantity",
            OVER_RETURN_MESSAGE,
            "R3",
        )
    ]


@pytest.mark.parametrize(
    ("return_changes", "column", "message"),
    [
        (
            {"original_transaction_id": "T404"},
            "original_transaction_id",
            "Original transaction 'T404' not found.",
        ),
        (
            {"original_transaction_id": ""},
            "original_transaction_id",
            "A return (negative quantity) must set original_transaction_id.",
        ),
        (
            {"store_code": "ST02"},
            "store_code",
            "Return store 'ST02' does not match the original sale's store 'ST01'.",
        ),
        (
            {"sku": "SKU2"},
            "sku",
            "Return SKU 'SKU2' does not match the original sale's SKU 'SKU1'.",
        ),
    ],
    ids=["unknown-original", "no-original", "different-store", "different-sku"],
)
def test_invalid_returns_are_rejected(
    tmp_path, session_factory, return_changes, column, message
):
    return_row = row_with(
        transaction_id="R1", sold_at=LATER, quantity="-1", original_transaction_id="T1"
    )
    return_row = [
        return_changes.get(name, value)
        for name, value in zip(HEADER, return_row, strict=True)
    ]

    import_csv(tmp_path, [row_with(transaction_id="T1"), return_row])

    assert "R1" not in saved_transactions(session_factory)
    assert saved_row_errors(session_factory, only_batch(session_factory)) == [
        (3, column, message, "R1")
    ]


def a_return(transaction_id, sold_at, quantity="-1", original="T1", **changes):
    return row_with(
        transaction_id=transaction_id,
        sold_at=sold_at,
        quantity=quantity,
        original_transaction_id=original,
        **changes,
    )


def test_return_listed_before_its_sale_is_accepted(tmp_path, session_factory):
    rows = [
        a_return("R1", "2026-10-17 11:00:00"),  # row 2
        row_with(transaction_id="T1", sold_at="2026-10-17 09:00:00"),  # row 3
    ]

    import_csv(tmp_path, rows)

    saved = saved_transactions(session_factory)
    batch = only_batch(session_factory)
    assert sorted(saved) == ["R1", "T1"]
    assert saved["R1"].original_transaction_id == "T1"
    assert saved_row_errors(session_factory, batch) == []
    assert (batch.rows_accepted, batch.rows_rejected) == (2, 0)


def before_sale_message(return_time, sale_time):
    return (
        f"Return sold_at ({return_time}) must be after the original "
        f"sale's sold_at ({sale_time})."
    )


def test_return_at_same_time_as_sale_is_rejected_as_before_sale(
    tmp_path, session_factory
):
    # The sale is processed first (sales before returns at equal times), so the
    # return finds it and fails the strictly-after rule, not "not found".
    same_time = "2026-10-17 09:00:00"
    rows = [
        a_return("R1", same_time),
        row_with(transaction_id="T1", sold_at=same_time),
    ]

    import_csv(tmp_path, rows)

    assert list(saved_transactions(session_factory)) == ["T1"]
    assert saved_row_errors(session_factory, only_batch(session_factory)) == [
        (
            2,
            "sold_at",
            before_sale_message("2026-10-17 09:00:00", "2026-10-17 09:00:00"),
            "R1",
        )
    ]


def test_return_before_sale_from_earlier_file_is_rejected(tmp_path, session_factory):
    import_csv(tmp_path, [row_with(transaction_id="T1")], name="day1.csv")

    import_csv(tmp_path, [a_return("R1", "2026-10-17 08:00:00")], name="day2.csv")

    assert list(saved_transactions(session_factory)) == ["T1"]
    errors = saved_row_errors(session_factory, batch_named(session_factory, "day2.csv"))
    assert errors == [
        (
            2,
            "sold_at",
            before_sale_message("2026-10-17 08:00:00", "2026-10-17 09:00:00"),
            "R1",
        )
    ]


def test_errors_keep_original_csv_row_numbers_after_sorting(tmp_path, session_factory):
    rows = [
        a_return("R1", "2026-10-17 11:00:00"),  # row 2: valid
        row_with(transaction_id="T1", sold_at="2026-10-17 09:00:00"),  # row 3
        a_return("R2", "2026-10-17 12:00:00", quantity="-5"),  # row 4: over-return
        row_with(transaction_id="T9", sold_at="2026-10-17 08:00:00", sku=""),  # row 5
        row_with(  # row 6: earliest time, bad payment mode
            transaction_id="T6", sold_at="2026-10-17 07:00:00", payment_mode="CHEQUE"
        ),
        row_with(transaction_id="T7", sold_at="not a date"),  # row 7
    ]

    import_csv(tmp_path, rows)

    errors = saved_row_errors(session_factory, only_batch(session_factory))
    assert [(row, column, tid) for row, column, _, tid in errors] == [
        (4, "quantity", "R2"),
        (5, "sku", "T9"),
        (6, "payment_mode", "T6"),
        (7, "sold_at", "T7"),
    ]
    assert sorted(saved_transactions(session_factory)) == ["R1", "T1"]


def test_return_timed_before_its_sale_in_same_file_is_rejected_as_before_sale(
    tmp_path, session_factory
):
    # Pass 1 saves the sale first, so the return finds it and gets the precise
    # error instead of "not found".
    rows = [
        a_return("R1", "2026-10-17 08:59:59"),
        row_with(transaction_id="T1", sold_at="2026-10-17 09:00:00"),
    ]

    import_csv(tmp_path, rows)

    assert list(saved_transactions(session_factory)) == ["T1"]
    assert saved_row_errors(session_factory, only_batch(session_factory)) == [
        (
            2,
            "sold_at",
            before_sale_message("2026-10-17 08:59:59", "2026-10-17 09:00:00"),
            "R1",
        )
    ]


def test_returns_listed_above_their_sales_are_accepted_in_second_pass(
    tmp_path, session_factory
):
    rows = [
        a_return("R1", "2026-10-17 11:00:00", original="T1"),  # row 2
        a_return("R2", "2026-10-17 11:30:00", quantity="-2", original="T2"),  # row 3
        a_return("R3", "2026-10-17 12:00:00", quantity="-9", original="T2"),  # row 4
        row_with(transaction_id="T1", sold_at="2026-10-17 09:00:00"),  # row 5
        row_with(transaction_id="T2", sold_at="2026-10-17 10:00:00", quantity="3"),
    ]

    import_csv(tmp_path, rows)

    saved = saved_transactions(session_factory)
    batch = only_batch(session_factory)
    assert sorted(saved) == ["R1", "R2", "T1", "T2"]
    assert (
        saved["R1"].original_transaction_id,
        saved["R2"].original_transaction_id,
    ) == (
        "T1",
        "T2",
    )
    assert (batch.rows_total, batch.rows_accepted, batch.rows_rejected) == (5, 4, 1)
    # R3 would bring returns on T2 to 11 of 3; the error names its CSV row.
    assert saved_row_errors(session_factory, batch) == [
        (
            4,
            "quantity",
            (
                "Returning 9 would bring total returns to 11, "
                "more than the 3 originally sold."
            ),
            "R3",
        )
    ]


def test_processing_order_runs_sales_then_returns_and_keeps_row_numbers():
    def csv_row(row_number, transaction_id, sold_at, quantity):
        values = dict(EXPECTED_ROW)
        values.update(transaction_id=transaction_id, sold_at=sold_at, quantity=quantity)
        return CsvRow(row_number, values)

    rows = [
        csv_row(2, "R1", "2026-10-17 11:00:00", "-1"),
        csv_row(3, "T2", "2026-10-17 10:00:00", "1"),
        csv_row(4, "BAD", "not a date", "1"),
        csv_row(5, "T1", "2026-10-17 09:00:00", "1"),
        csv_row(6, "R0", "2026-10-17 10:30:00", "-1"),
        csv_row(7, "Q", "2026-10-17 08:00:00", "lots"),
    ]

    ordered = processing_order(rows)

    assert [(row.row_number, row.values["transaction_id"]) for row in ordered] == [
        (7, "Q"),  # Pass 1: sales (and unreadable quantities), oldest first
        (5, "T1"),
        (3, "T2"),
        (4, "BAD"),  # unreadable sold_at goes last in its pass
        (6, "R0"),  # Pass 2: returns, oldest first
        (2, "R1"),
    ]
    assert sorted(ordered, key=lambda row: row.row_number) == rows  # Rows unchanged.


def test_chronologically_first_duplicate_is_kept(tmp_path, session_factory):
    rows = [
        row_with(transaction_id="T1", sold_at="2026-10-17 10:00:00", quantity="7"),
        row_with(transaction_id="T1", sold_at="2026-10-17 09:00:00", quantity="2"),
    ]

    import_csv(tmp_path, rows)

    assert saved_transactions(session_factory)["T1"].quantity == 2
    assert saved_row_errors(session_factory, only_batch(session_factory)) == [
        (
            2,
            "transaction_id",
            "Transaction ID 'T1' already appears in row 3.",
            "T1",
        )
    ]


# Products and aliases


def test_new_sku_creates_product_and_new_names_become_aliases(
    tmp_path, session_factory
):
    rows = [
        row_with(transaction_id="T1", product_name="Masala Chai"),
        row_with(transaction_id="T2", product_name="Chai Masala"),
        row_with(transaction_id="T3", product_name="Chai Masala"),
        row_with(transaction_id="T4", product_name="Masala Chai"),
    ]

    import_csv(tmp_path, rows)

    products, aliases = saved_products(session_factory)
    assert products == {"SKU1": "Masala Chai"}
    assert aliases == [("SKU1", "Chai Masala")]
    assert saved_transactions(session_factory)["T2"].product_name == "Chai Masala"


def test_existing_product_gets_alias_from_a_later_file(tmp_path, session_factory):
    import_csv(tmp_path, [row_with(transaction_id="T1")], name="day1.csv")
    import_csv(
        tmp_path,
        [row_with(transaction_id="T2", product_name="Tea (Masala)")],
        name="day2.csv",
    )

    assert saved_products(session_factory) == (
        {"SKU1": "Masala Chai"},
        [("SKU1", "Tea (Masala)")],
    )


def test_rejected_row_does_not_create_product(tmp_path, session_factory):
    import_csv(tmp_path, [row_with(sku="SKU9", store_code="NOPE")])

    assert saved_products(session_factory) == ({}, [])


# Failure and retry


def test_database_failure_saves_nothing_and_marks_batch_failed(
    tmp_path, capsys, session_factory, monkeypatch
):
    def broken_lookup(*args, **kwargs):
        raise OperationalError("SELECT", {}, Exception("connection lost"))

    monkeypatch.setattr(csv_validator, "load_import_state", broken_lookup)

    exit_code = import_csv(tmp_path, [ROW, row_with(transaction_id="T2", sku="")])

    batch = only_batch(session_factory)
    assert exit_code == 1
    assert batch.status == "FAILED"
    assert batch.failure_reason == (
        "Database error while saving rows; nothing was saved. "
        "Upload the same file again to retry."
    )
    assert batch.attempt_count == 1
    assert saved_transactions(session_factory) == {}
    assert saved_row_errors(session_factory, batch) == []
    assert f"Batch {batch.id} FAILED:" in capsys.readouterr().err


def test_uploading_a_failed_file_again_retries_it(
    tmp_path, session_factory, monkeypatch
):
    real_lookup = csv_validator.load_import_state

    def broken_lookup(*args, **kwargs):
        raise OperationalError("SELECT", {}, Exception("connection lost"))

    monkeypatch.setattr(csv_validator, "load_import_state", broken_lookup)
    import_csv(tmp_path, [ROW])
    monkeypatch.setattr(csv_validator, "load_import_state", real_lookup)

    exit_code = import_csv(tmp_path, [ROW])

    batch = only_batch(session_factory)
    assert exit_code == 0
    assert batch.status == "COMPLETED"
    assert batch.attempt_count == 2
    assert batch.failure_reason is None
    assert list(saved_transactions(session_factory)) == ["T1"]


def test_batch_already_processing_is_not_imported_again(
    tmp_path, capsys, session_factory
):
    path = write_file(tmp_path, "sales.csv")
    with session_factory() as session:
        session.add(
            ImportBatch(
                filename="sales.csv",
                file_sha256=hashlib.sha256(VALID_CONTENT).hexdigest(),
                status="PROCESSING",
            )
        )
        session.commit()

    exit_code = main([str(path)])

    assert exit_code == 1
    assert "status PROCESSING" in capsys.readouterr().err
    assert saved_transactions(session_factory) == {}


def test_queued_batch_that_never_ran_is_processed_on_upload(tmp_path, session_factory):
    path = write_file(tmp_path, "sales.csv")
    with session_factory() as session:
        session.add(
            ImportBatch(
                filename="sales.csv",
                file_sha256=hashlib.sha256(VALID_CONTENT).hexdigest(),
                status="QUEUED",
            )
        )
        session.commit()

    assert main([str(path)]) == 0

    batch = only_batch(session_factory)
    assert (batch.status, batch.attempt_count) == ("COMPLETED", 1)
    assert list(saved_transactions(session_factory)) == ["T1"]


# Rows committed by another import after this import's lookups


def test_stale_lookup_conflict_rejects_only_that_row(
    tmp_path, session_factory, monkeypatch
):
    # day1 commits SHARED and creates SKU1. day2's lookups are then made stale,
    # as if day1 committed between day2's lookup and its INSERTs.
    import_csv(tmp_path, [row_with(transaction_id="SHARED")], name="day1.csv")
    real_load = csv_validator.load_import_state

    def stale_load(session, rows):
        state = real_load(session, rows)
        state.existing_transaction_ids.clear()
        state.product_names.clear()
        return state

    monkeypatch.setattr(csv_validator, "load_import_state", stale_load)

    exit_code = import_csv(
        tmp_path,
        [
            row_with(transaction_id="SHARED", quantity="7"),
            row_with(transaction_id="NEW_B"),
        ],
        name="day2.csv",
    )

    day2 = batch_named(session_factory, "day2.csv")
    saved = saved_transactions(session_factory)
    assert exit_code == 0
    assert saved["SHARED"].quantity == 2  # day1's row is untouched.
    assert saved["NEW_B"].batch_id == day2.id  # Kept despite the conflict.
    assert saved_products(session_factory) == ({"SKU1": "Masala Chai"}, [])
    assert (day2.status, day2.rows_accepted, day2.rows_rejected) == (
        "COMPLETED",
        1,
        1,
    )
    assert saved_row_errors(session_factory, day2) == [
        (
            2,
            "transaction_id",
            "Transaction ID 'SHARED' has already been imported.",
            "SHARED",
        )
    ]


# Several files in one run


def summary_lines(stdout):
    lines = stdout.splitlines()
    return lines[lines.index("") + 1 :]  # The summary follows a blank line.


def test_multiple_valid_files_are_all_imported(tmp_path, capsys, session_factory):
    day1 = write_file(
        tmp_path, "day1.csv", csv_bytes(rows=[row_with(transaction_id="T1")])
    )
    day2 = write_file(
        tmp_path, "day2.csv", csv_bytes(rows=[row_with(transaction_id="T2")])
    )

    exit_code = main([str(day1), str(day2)])

    batch1 = batch_named(session_factory, "day1.csv")
    batch2 = batch_named(session_factory, "day2.csv")
    assert exit_code == 0
    assert sorted(saved_transactions(session_factory)) == ["T1", "T2"]
    assert summary_lines(capsys.readouterr().out) == [
        "Summary: 2 files: 2 imported.",
        f"  IMPORTED  {day1}: batch {batch1.id}: 1 accepted, 0 rejected.",
        f"  IMPORTED  {day2}: batch {batch2.id}: 1 accepted, 0 rejected.",
    ]


def test_bad_and_duplicate_files_are_reported_and_the_rest_imported(
    tmp_path, capsys, session_factory
):
    good1 = write_file(
        tmp_path, "good1.csv", csv_bytes(rows=[row_with(transaction_id="T1")])
    )
    bad_header = write_file(tmp_path, "bad.csv", csv_bytes(header=HEADER[:-1]))
    same_as_good1 = write_file(tmp_path, "copy.csv", good1.read_bytes())
    good2 = write_file(
        tmp_path, "good2.csv", csv_bytes(rows=[row_with(transaction_id="T2")])
    )

    exit_code = main([str(good1), str(bad_header), str(same_as_good1), str(good2)])

    output = capsys.readouterr()
    batch1 = batch_named(session_factory, "good1.csv")
    batch2 = batch_named(session_factory, "good2.csv")
    assert exit_code == 1  # Not every file imported.
    assert sorted(saved_transactions(session_factory)) == ["T1", "T2"]
    assert len(all_batches(session_factory)) == 2  # No batch for bad or copy.
    assert f"INVALID {bad_header}: Invalid header" in output.err
    assert f"ALREADY UPLOADED {same_as_good1}" in output.err
    assert summary_lines(output.out) == [
        "Summary: 4 files: 2 imported, 1 invalid, 1 already uploaded.",
        f"  IMPORTED          {good1}: batch {batch1.id}: 1 accepted, 0 rejected.",
        (
            f"  INVALID           {bad_header}: Invalid header: missing column(s): "
            "original_transaction_id."
        ),
        (
            f"  ALREADY UPLOADED  {same_as_good1}: this file was already uploaded as "
            f"'good1.csv' (batch {batch1.id}, status COMPLETED)."
        ),
        f"  IMPORTED          {good2}: batch {batch2.id}: 1 accepted, 0 rejected.",
    ]


def test_same_file_twice_in_one_run_is_imported_once(tmp_path, capsys, session_factory):
    path = write_file(tmp_path, "sales.csv")

    exit_code = main([str(path), str(path)])

    assert exit_code == 1
    assert list(saved_transactions(session_factory)) == ["T1"]
    assert summary_lines(capsys.readouterr().out)[0] == (
        "Summary: 2 files: 1 imported, 1 already uploaded."
    )


def test_unexpected_error_in_one_file_does_not_stop_the_others(
    tmp_path, capsys, session_factory, monkeypatch
):
    broken = write_file(
        tmp_path, "broken.csv", csv_bytes(rows=[row_with(transaction_id="T1")])
    )
    good = write_file(
        tmp_path, "good.csv", csv_bytes(rows=[row_with(transaction_id="T2")])
    )
    real_check = csv_validator.check_csv_file

    def crash_on_broken(path, max_size_bytes):
        if path.name == "broken.csv":
            raise RuntimeError("disk on fire")
        return real_check(path, max_size_bytes)

    monkeypatch.setattr(csv_validator, "check_csv_file", crash_on_broken)

    exit_code = main([str(broken), str(good)])

    output = capsys.readouterr()
    assert exit_code == 1
    assert list(saved_transactions(session_factory)) == ["T2"]
    assert "disk on fire" not in output.err  # Details go to the log only.
    assert summary_lines(output.out)[:2] == [
        "Summary: 2 files: 1 imported, 1 error.",
        f"  ERROR     {broken}: unexpected error (details are in the log)",
    ]


def test_database_down_reports_every_file_without_crashing(
    tmp_path, capsys, monkeypatch
):
    def unavailable():
        raise OperationalError("connect", {}, Exception("password=secret"))

    monkeypatch.setattr(csv_validator, "SessionLocal", unavailable)
    first = write_file(
        tmp_path, "a.csv", csv_bytes(rows=[row_with(transaction_id="T1")])
    )
    second = write_file(
        tmp_path, "b.csv", csv_bytes(rows=[row_with(transaction_id="T2")])
    )

    exit_code = main([str(first), str(second)])

    output = capsys.readouterr()
    assert exit_code == 1
    assert "password" not in output.out + output.err
    assert summary_lines(output.out)[0] == "Summary: 2 files: 2 error."


def test_single_file_prints_no_summary(tmp_path, capsys):
    main([str(write_file(tmp_path, "sales.csv"))])

    assert "Summary:" not in capsys.readouterr().out


# Staged uploads (import_payloads) and the background worker


def enqueue(session_factory, content=VALID_CONTENT, filename="sales.csv"):
    with session_factory() as session:
        return enqueue_upload(session, filename, content, MAX_SIZE_BYTES)


def staged_payloads(session_factory):
    with session_factory() as session:
        return session.scalars(select(ImportPayload)).all()


def failing_lookups(times):
    """A load_import_state that raises a database error on its first `times` calls."""
    real_load = csv_validator.load_import_state
    calls = {"count": 0}

    def load(session, rows):
        calls["count"] += 1
        if calls["count"] <= times:
            raise OperationalError("SELECT", {}, Exception("connection lost"))
        return real_load(session, rows)

    return load


def test_enqueue_stores_gzipped_bytes_with_a_queued_batch(session_factory):
    outcome = enqueue(session_factory)

    batch = only_batch(session_factory)
    [payload] = staged_payloads(session_factory)
    assert (outcome.status, outcome.detail) == ("QUEUED", f"batch {batch.id} queued.")
    assert (batch.status, batch.attempt_count, batch.filename) == (
        "QUEUED",
        0,
        "sales.csv",
    )
    assert batch.file_sha256 == hashlib.sha256(VALID_CONTENT).hexdigest()
    assert payload.batch_id == batch.id
    assert gzip.decompress(payload.csv_gzip) == VALID_CONTENT
    assert payload.received_size_bytes == len(VALID_CONTENT)
    assert saved_transactions(session_factory) == {}  # Nothing imported yet.


@pytest.mark.parametrize(
    ("filename", "content", "message"),
    [
        ("sales.csv", b"x" * (MAX_SIZE_BYTES + 1), "exceeds the 1000-byte limit"),
        ("sales.csv", b"", "File is empty."),
        ("sales.txt", VALID_CONTENT, "File must have a .csv extension."),
    ],
    ids=["too-large", "empty", "wrong-extension"],
)
def test_enqueue_rejects_unusable_uploads_without_storing(
    session_factory, filename, content, message
):
    outcome = enqueue(session_factory, content, filename)

    assert outcome.status == "INVALID"
    assert message in outcome.detail
    assert all_batches(session_factory) == []
    assert staged_payloads(session_factory) == []


def test_enqueue_same_file_twice_stages_it_once(session_factory):
    enqueue(session_factory)

    outcome = enqueue(session_factory, filename="copy.csv")

    assert outcome.status == "ALREADY UPLOADED"
    assert len(staged_payloads(session_factory)) == 1


def test_worker_imports_staged_batch_and_deletes_its_payload(session_factory):
    rows = [ROW, row_with(transaction_id="T2", payment_mode="CHEQUE")]
    enqueue(session_factory, csv_bytes(rows=rows))

    outcome = run_next_batch()

    batch = only_batch(session_factory)
    assert outcome.status == "IMPORTED"
    assert (batch.status, batch.attempt_count) == ("COMPLETED", 1)
    assert (batch.rows_accepted, batch.rows_rejected) == (1, 1)
    assert list(saved_transactions(session_factory)) == ["T1"]
    assert saved_row_errors(session_factory, batch)[0][:2] == (3, "payment_mode")
    assert staged_payloads(session_factory) == []


def test_worker_has_nothing_to_do_without_staged_payloads(session_factory):
    assert run_next_batch() is None
    enqueue(session_factory)
    run_next_batch()

    assert run_next_batch() is None


def test_worker_rejects_bad_header_and_deletes_payload(session_factory):
    enqueue(session_factory, csv_bytes(header=HEADER[:-1]))

    outcome = run_next_batch()

    batch = only_batch(session_factory)
    assert outcome.status == "INVALID"
    assert batch.status == "REJECTED_HEADER"
    assert batch.failure_reason == (
        "Invalid header: missing column(s): original_transaction_id."
    )
    assert naive_utc(batch.completed_at) == naive_utc(FIXED_NOW)
    assert staged_payloads(session_factory) == []


@pytest.mark.parametrize(
    ("content", "reason_start"),
    [
        (HEADER_LINE + b"\xff\n", "File is not valid UTF-8"),
        (HEADER_LINE + b'"unterminated\n', "Malformed CSV near line 2"),
    ],
    ids=["bad-encoding", "bad-csv-syntax"],
)
def test_worker_fails_unreadable_file_and_deletes_payload(
    session_factory, content, reason_start
):
    enqueue(session_factory, content)

    run_next_batch()

    batch = only_batch(session_factory)
    assert batch.status == "FAILED"
    assert batch.failure_reason.startswith(reason_start)
    assert staged_payloads(session_factory) == []


def test_worker_fails_corrupt_payload(session_factory):
    enqueue(session_factory)
    with session_factory() as session:
        session.scalars(select(ImportPayload)).one().csv_gzip = b"not gzip"
        session.commit()

    run_next_batch()

    batch = only_batch(session_factory)
    assert (batch.status, batch.failure_reason) == ("FAILED", "Staged file is corrupt.")
    assert staged_payloads(session_factory) == []


def test_database_error_keeps_payload_and_next_run_retries(
    session_factory, monkeypatch
):
    monkeypatch.setattr(csv_validator, "load_import_state", failing_lookups(times=1))
    enqueue(session_factory)

    first = run_next_batch()

    batch = only_batch(session_factory)
    assert first.status == "FAILED"
    assert (batch.status, batch.attempt_count) == ("FAILED", 1)
    assert batch.failure_reason == (
        "Database error on attempt 1 of 3; nothing was saved. The worker will retry."
    )
    assert saved_transactions(session_factory) == {}
    assert len(staged_payloads(session_factory)) == 1  # Kept for the retry.

    second = run_next_batch()

    batch = only_batch(session_factory)
    assert second.status == "IMPORTED"
    assert (batch.status, batch.attempt_count) == ("COMPLETED", 2)
    assert list(saved_transactions(session_factory)) == ["T1"]
    assert staged_payloads(session_factory) == []


def test_worker_gives_up_after_max_attempts_and_reupload_requeues(
    session_factory, monkeypatch
):
    monkeypatch.setattr(csv_validator, "load_import_state", failing_lookups(times=3))
    enqueue(session_factory)

    outcomes = [run_next_batch() for _ in range(4)]

    batch = only_batch(session_factory)
    assert [o.status if o else None for o in outcomes] == [
        "FAILED",
        "FAILED",
        "FAILED",
        None,  # Payload removed after the last attempt: nothing left to do.
    ]
    assert (batch.status, batch.attempt_count) == ("FAILED", 3)
    assert batch.failure_reason == (
        "Database error on all 3 attempts; nothing was saved. "
        "Upload the same file again to retry."
    )
    assert staged_payloads(session_factory) == []

    requeued = enqueue(session_factory)

    assert requeued.status == "QUEUED"
    assert requeued.detail == f"batch {batch.id} queued again for retry."
    assert run_next_batch().status == "IMPORTED"
    assert list(saved_transactions(session_factory)) == ["T1"]


def test_worker_takes_oldest_upload_first(session_factory):
    enqueue(
        session_factory, csv_bytes(rows=[row_with(transaction_id="NEWER")]), "b.csv"
    )
    enqueue(
        session_factory, csv_bytes(rows=[row_with(transaction_id="OLDER")]), "a.csv"
    )
    with session_factory() as session:
        for batch in session.scalars(select(ImportBatch)):
            hours_ago = 1 if batch.filename == "b.csv" else 2
            batch.uploaded_at = FIXED_NOW - timedelta(hours=hours_ago)
        session.commit()

    run_next_batch()

    assert list(saved_transactions(session_factory)) == ["OLDER"]


@pytest.mark.parametrize(
    ("started_minutes_ago", "reclaimed"),
    [(61, True), (59, False)],
    ids=["stale-after-an-hour", "still-running"],
)
def test_processing_batch_is_reclaimed_only_when_stale(
    session_factory, started_minutes_ago, reclaimed
):
    enqueue(session_factory)
    with session_factory() as session:  # As if a worker claimed it, then died.
        batch = session.scalars(select(ImportBatch)).one()
        batch.status = "PROCESSING"
        batch.attempt_count = 1
        batch.started_at = FIXED_NOW - timedelta(minutes=started_minutes_ago)
        session.commit()

    outcome = run_next_batch()

    batch = only_batch(session_factory)
    if reclaimed:
        assert outcome.status == "IMPORTED"
        assert (batch.status, batch.attempt_count) == ("COMPLETED", 2)
    else:
        assert outcome is None
        assert batch.status == "PROCESSING"
        assert len(staged_payloads(session_factory)) == 1


def test_run_worker_drain_processes_everything_then_stops(session_factory):
    enqueue(session_factory, csv_bytes(rows=[row_with(transaction_id="T1")]), "a.csv")
    enqueue(session_factory, csv_bytes(header=HEADER[:-1]), "bad.csv")
    enqueue(session_factory, csv_bytes(rows=[row_with(transaction_id="T2")]), "b.csv")

    outcomes = run_worker(drain=True)

    assert sorted(o.status for o in outcomes) == ["IMPORTED", "IMPORTED", "INVALID"]
    assert sorted(saved_transactions(session_factory)) == ["T1", "T2"]
    assert staged_payloads(session_factory) == []


def test_run_worker_drain_stops_cleanly_when_database_is_down(monkeypatch, capsys):
    def unavailable():
        raise OperationalError("connect", {}, Exception("password=secret"))

    monkeypatch.setattr(csv_validator, "SessionLocal", unavailable)

    outcomes = run_worker(drain=True)

    assert [o.status for o in outcomes] == ["ERROR"]
    assert "password" not in capsys.readouterr().err


def test_cli_queue_then_worker_once(tmp_path, capsys, session_factory):
    day1 = write_file(
        tmp_path, "day1.csv", csv_bytes(rows=[row_with(transaction_id="T1")])
    )
    day2 = write_file(
        tmp_path, "day2.csv", csv_bytes(rows=[row_with(transaction_id="T2")])
    )

    assert main(["--queue", str(day1), str(day2)]) == 0
    queued_output = capsys.readouterr().out
    assert "Summary: 2 files: 2 queued." in queued_output
    assert saved_transactions(session_factory) == {}
    assert len(staged_payloads(session_factory)) == 2

    assert main(["--worker", "--once"]) == 0

    assert "Summary: 2 files: 2 imported." in capsys.readouterr().out
    assert sorted(saved_transactions(session_factory)) == ["T1", "T2"]
    assert staged_payloads(session_factory) == []
    assert {b.status for b in all_batches(session_factory)} == {"COMPLETED"}


def test_cli_queue_reports_invalid_file_and_queues_the_rest(
    tmp_path, capsys, session_factory
):
    good = write_file(tmp_path, "good.csv")
    wrong_type = write_file(tmp_path, "notes.txt")

    exit_code = main(["--queue", str(good), str(wrong_type)])

    assert exit_code == 1
    assert len(staged_payloads(session_factory)) == 1
    assert "Summary: 2 files: 1 queued, 1 invalid." in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [["--worker", "a.csv"], ["--once", "a.csv"], ["--queue"], ["--queue", "--worker"]],
    ids=[
        "worker-with-paths",
        "once-without-worker",
        "queue-without-paths",
        "both-modes",
    ],
)
def test_cli_rejects_invalid_flag_combinations(argv):
    with pytest.raises(SystemExit) as excinfo:
        main(argv)

    assert excinfo.value.code == 2


def test_cli_worker_stops_cleanly_on_ctrl_c(monkeypatch, capsys):
    def interrupted(**kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(csv_validator, "run_worker", interrupted)

    assert main(["--worker"]) == 0
    assert capsys.readouterr().out == "Worker stopped.\n"
