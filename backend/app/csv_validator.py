"""Basic file-level checks for uploaded sales CSV files.

Only checks the file itself: existence, extension, size and readability.

"""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FileCheckResult:
    path: Path
    is_valid: bool
    message: str


def check_csv_file(path: Path, max_size_bytes: int) -> FileCheckResult:
    def fail(message: str) -> FileCheckResult:
        return FileCheckResult(path, is_valid=False, message=message)

    if not path.exists():
        return fail("File does not exist.")
    if not path.is_file():
        return fail("Path is not a regular file.")
    if path.suffix.lower() != ".csv":
        return fail("File must have a .csv extension.")

    try:
        size_bytes = path.stat().st_size
        with path.open("rb") as file:
            file.read(1)
    except OSError as error:
        return fail(f"File cannot be read ({error.strerror or 'unknown error'}).")

    if size_bytes == 0:
        return fail("File is empty.")
    if size_bytes > max_size_bytes:
        return fail(
            f"File is {size_bytes} bytes, which exceeds the {max_size_bytes}-byte limit."
        )

    return FileCheckResult(path, is_valid=True, message="File passed basic checks.")


def check_csv_files(paths: list[Path], max_size_bytes: int) -> list[FileCheckResult]:
    return [check_csv_file(path, max_size_bytes) for path in paths]
