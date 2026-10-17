import os

import pytest

from app.config import Settings
from app.csv_validator import check_csv_file, check_csv_files

MAX_SIZE_BYTES = 100
VALID_CONTENT = b"transaction_id,store_code\nT1,ST01\n"


def write_file(directory, name, content=VALID_CONTENT):
    path = directory / name
    path.write_bytes(content)
    return path


def assert_invalid(result, expected_message):
    assert result.is_valid is False
    assert result.message == expected_message


@pytest.mark.parametrize("name", ["sales.csv", "SALES.CSV"])
def test_valid_csv_passes(tmp_path, name):
    path = write_file(tmp_path, name)

    result = check_csv_file(path, MAX_SIZE_BYTES)

    assert result.is_valid is True
    assert result.message == "File passed basic checks."
    assert result.path == path


def test_file_exactly_at_size_limit_passes(tmp_path):
    path = write_file(tmp_path, "sales.csv", b"x" * MAX_SIZE_BYTES)

    assert check_csv_file(path, MAX_SIZE_BYTES).is_valid is True


def test_one_byte_file_passes(tmp_path):
    path = write_file(tmp_path, "sales.csv", b"x")

    assert check_csv_file(path, MAX_SIZE_BYTES).is_valid is True


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
    path = write_file(tmp_path, "sales.csv", b"")

    assert_invalid(check_csv_file(path, MAX_SIZE_BYTES), "File is empty.")


def test_file_one_byte_over_limit_fails(tmp_path):
    path = write_file(tmp_path, "sales.csv", b"x" * (MAX_SIZE_BYTES + 1))

    assert_invalid(
        check_csv_file(path, MAX_SIZE_BYTES),
        "File is 101 bytes, which exceeds the 100-byte limit.",
    )


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


# Encoding

UTF8_BOM = b"\xef\xbb\xbf"


def test_utf8_with_non_ascii_text_passes(tmp_path):
    content = "sku,product_name\nSKU1,Masala Chai ☕ ₹20\n".encode()
    path = write_file(tmp_path, "sales.csv", content)

    assert check_csv_file(path, MAX_SIZE_BYTES).is_valid is True


def test_utf8_with_bom_passes(tmp_path):
    path = write_file(tmp_path, "sales.csv", UTF8_BOM + VALID_CONTENT)

    assert check_csv_file(path, MAX_SIZE_BYTES).is_valid is True


def test_file_containing_only_a_bom_is_empty(tmp_path):
    path = write_file(tmp_path, "sales.csv", UTF8_BOM)

    assert_invalid(check_csv_file(path, MAX_SIZE_BYTES), "File is empty.")


@pytest.mark.parametrize(
    ("content", "bad_byte_position"),
    [
        (b"a,b\n1,\xff\n", 6),  # byte that never appears in UTF-8
        ("a,b\n1,café\n".encode("latin-1"), 9),  # Latin-1 export, e.g. from Excel
        (UTF8_BOM + b"a,\xff", 5),  # position counts the BOM bytes
    ],
)
def test_invalid_utf8_fails(tmp_path, content, bad_byte_position):
    path = write_file(tmp_path, "sales.csv", content)

    assert_invalid(
        check_csv_file(path, MAX_SIZE_BYTES),
        f"File is not valid UTF-8 (invalid byte at position {bad_byte_position}).",
    )


# Line endings and CSV syntax


@pytest.mark.parametrize(
    "content",
    [
        b"a,b\n1,2\n",  # LF
        b"a,b\r\n1,2\r\n",  # CRLF
        b"a,b\r\n1,2",  # CRLF, no trailing newline
        UTF8_BOM + b"a,b\r\n1,2\r\n",  # BOM + CRLF, typical Excel export
    ],
    ids=["lf", "crlf", "crlf-no-final-newline", "bom-crlf"],
)
def test_lf_and_crlf_line_endings_pass(tmp_path, content):
    path = write_file(tmp_path, "sales.csv", content)

    assert check_csv_file(path, MAX_SIZE_BYTES).is_valid is True


def test_well_formed_quoting_passes(tmp_path):
    content = b'a,b\r\n1,"comma, ""quotes"" and\r\nnewline"\r\n'
    path = write_file(tmp_path, "sales.csv", content)

    assert check_csv_file(path, MAX_SIZE_BYTES).is_valid is True


@pytest.mark.parametrize(
    "content",
    [
        b'a,b\n1,"unterminated\n',  # quote never closed
        b'a,b\n1,"closed"extra\n',  # text after the closing quote
    ],
    ids=["unclosed-quote", "text-after-closing-quote"],
)
def test_malformed_quoting_fails(tmp_path, content):
    path = write_file(tmp_path, "sales.csv", content)

    result = check_csv_file(path, MAX_SIZE_BYTES)

    assert result.is_valid is False
    assert result.message.startswith("Malformed CSV near line 2: ")


def test_size_limit_is_checked_before_encoding(tmp_path):
    path = write_file(tmp_path, "sales.csv", b"\xff" * (MAX_SIZE_BYTES + 1))

    assert_invalid(
        check_csv_file(path, MAX_SIZE_BYTES),
        "File is 101 bytes, which exceeds the 100-byte limit.",
    )
