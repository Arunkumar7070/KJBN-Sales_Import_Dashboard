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
