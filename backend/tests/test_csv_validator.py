import hashlib
import os

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.database import Base
from app.models import ImportBatch
from app.services import csv_validator
from app.services.csv_validator import (
    CsvRow,
    RowError,
    check_csv_file,
    main,
    register_import_batch,
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
    "2026-10-17T09:00:00Z",
    "SKU1",
    "Masala Chai",
    "2",
    "100.00",
    "0.00",
    "UPI",
    "",
]
EXPECTED_ROW = dict(zip(HEADER, ROW, strict=True))


@pytest.fixture(autouse=True)
def session_factory(monkeypatch):
    """Point the validator at a throwaway SQLite database; Neon is never touched."""
    # autocommit=False makes sqlite3 handle SAVEPOINTs the way SQLAlchemy expects.
    engine = create_engine("sqlite://", connect_args={"autocommit": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(csv_validator, "SessionLocal", factory)
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
        "Rows: 1 valid, 0 invalid.\n"
        f"Created import batch {batch.id} with status QUEUED.\n"
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


@pytest.mark.parametrize("argv", [[], ["a.csv", "b.csv"]], ids=["none", "two"])
def test_cli_requires_exactly_one_path(argv):
    with pytest.raises(SystemExit) as excinfo:
        main(argv)

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
        f"'sales.csv' (batch {batch.id}, status QUEUED).\n"
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
    assert err == (
        f"ERROR {path}: could not record the import batch (database unavailable).\n"
    )


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


def test_cli_lists_invalid_rows_and_still_creates_batch(
    tmp_path, capsys, session_factory
):
    rows = [ROW, row_with(transaction_id="T2", sku=""), ROW[:9]]
    path = write_file(tmp_path, "sales.csv", csv_bytes(rows=rows))

    exit_code = main([str(path)])

    out = capsys.readouterr().out.splitlines()
    assert exit_code == 0
    assert out[1:4] == [
        "Rows: 1 valid, 2 invalid.",
        "Row 3: Missing required value(s): sku.",
        "Row 4: Row has 9 column(s); expected 10.",
    ]
    assert out[4].startswith("Created import batch")
    assert only_batch(session_factory).status == "QUEUED"
