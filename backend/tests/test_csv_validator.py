import os

import pytest

from app.config import Settings
from app.csv_validator import check_csv_file, check_csv_files

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


# Basic file checks


@pytest.mark.parametrize("name", ["sales.csv", "SALES.CSV"])
def test_valid_csv_passes(tmp_path, name):
    path = write_file(tmp_path, name)

    result = check_csv_file(path, MAX_SIZE_BYTES)

    assert result.is_valid is True
    assert result.message == "File passed basic checks."
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


def test_each_file_gets_its_own_result(tmp_path):
    valid = write_file(tmp_path, "valid.csv")
    empty = write_file(tmp_path, "empty.csv", b"")
    missing = tmp_path / "missing.csv"

    results = check_csv_files([valid, empty, missing], MAX_SIZE_BYTES)

    assert [(result.path, result.is_valid) for result in results] == [
        (valid, True),
        (empty, False),
        (missing, False),
    ]


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
    assert result.rows[0]["product_name"] == "Masala Chai ☕ ₹20"


def test_utf8_with_bom_passes_and_bom_is_not_part_of_first_header(tmp_path):
    result = check(tmp_path, UTF8_BOM + VALID_CONTENT)

    assert result.is_valid is True
    assert result.rows == (EXPECTED_ROW,)


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
    assert result.rows == (EXPECTED_ROW,)


def test_well_formed_quoting_passes(tmp_path):
    row = [*ROW[:4], '"comma, ""quotes"" and\r\nnewline"', *ROW[5:]]

    result = check(tmp_path, csv_bytes(rows=[row], newline="\r\n"))

    assert result.is_valid is True
    assert result.rows[0]["product_name"] == 'comma, "quotes" and\r\nnewline'


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

    assert result.rows == (EXPECTED_ROW,)
    assert result.rows[0]["original_transaction_id"] == ""


def test_reordered_columns_map_values_by_header_name(tmp_path):
    reordered = csv_bytes(header=HEADER[::-1], rows=[ROW[::-1]])

    result = check(tmp_path, reordered)

    assert result.is_valid is True
    assert result.rows == (EXPECTED_ROW,)


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
