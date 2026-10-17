"""Print the SHA-256 hash of 3-4 CSV files.

Usage (from backend/):
    python -m app.inspect_csv_files a.csv b.csv c.csv [d.csv]

Files that are missing, are not regular files, lack a .csv extension, or
cannot be read are skipped with a message. File contents are not parsed.
"""

import argparse
import hashlib
import sys
from pathlib import Path

MIN_FILES = 3
MAX_FILES = 4
CHUNK_SIZE_BYTES = 1024 * 1024


def sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(CHUNK_SIZE_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def find_problem(path: Path) -> str | None:
    if not path.exists():
        return "file does not exist"
    if not path.is_file():
        return "not a regular file"
    if path.suffix.lower() != ".csv":
        return "not a .csv file"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print the SHA-256 hash of 3-4 CSV files."
    )
    parser.add_argument("paths", nargs="+", type=Path, help="CSV file paths")
    args = parser.parse_args(argv)

    if not MIN_FILES <= len(args.paths) <= MAX_FILES:
        parser.error(f"expected {MIN_FILES}-{MAX_FILES} files, got {len(args.paths)}")

    skipped = 0
    for path in args.paths:
        problem = find_problem(path)
        if problem is None:
            try:
                print(f"{sha256_of_file(path)}  {path}")
                continue
            except OSError as error:
                problem = f"cannot be read ({error.strerror})"
        print(f"SKIPPED {path}: {problem}", file=sys.stderr)
        skipped += 1

    return 1 if skipped else 0


if __name__ == "__main__":
    sys.exit(main())
