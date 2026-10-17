import hashlib

import pytest

from app.inspect_csv_files import main

CONTENT = b"transaction_id,store_code\nT1,ST01\n"
CONTENT_SHA256 = hashlib.sha256(CONTENT).hexdigest()


@pytest.fixture
def csv_files(tmp_path):
    paths = []
    for name in ("a.csv", "b.CSV", "c.csv"):
        path = tmp_path / name
        path.write_bytes(CONTENT)
        paths.append(str(path))
    return paths


def test_valid_files_print_their_sha256(csv_files, capsys):
    exit_code = main(csv_files)

    output = capsys.readouterr()
    assert exit_code == 0
    assert output.out.count(CONTENT_SHA256) == 3
    assert output.err == ""


def test_invalid_files_are_skipped_and_valid_ones_still_hashed(
    csv_files, tmp_path, capsys
):
    text_file = tmp_path / "notes.txt"
    text_file.write_bytes(CONTENT)
    missing = tmp_path / "missing.csv"

    exit_code = main([csv_files[0], str(text_file), str(missing), str(tmp_path)])

    output = capsys.readouterr()
    assert exit_code == 1
    assert output.out.count(CONTENT_SHA256) == 1
    assert "notes.txt: not a .csv file" in output.err
    assert "missing.csv: file does not exist" in output.err
    assert "not a regular file" in output.err


@pytest.mark.parametrize("file_count", [2, 5])
def test_wrong_number_of_files_is_a_usage_error(csv_files, file_count):
    paths = (csv_files * 2)[:file_count]

    with pytest.raises(SystemExit) as excinfo:
        main(paths)

    assert excinfo.value.code == 2
