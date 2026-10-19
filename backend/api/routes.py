"""Import endpoints: upload sales CSV files, list batches, download row errors."""

import csv
import io
import logging
import math
import threading
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.database import SessionLocal, get_db
from app.models import ImportBatch, ImportRowError
from app.schemas import (
    BatchListResponse,
    BatchResponse,
    ImportUploadResponse,
    UploadedFileResult,
)
from app.services.csv_validator import INVALID, enqueue_upload, run_worker

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["Imports"])

RATE_LIMIT_WINDOW_SECONDS = 60.0
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
ERROR_CSV_COLUMNS = ("row_number", "column_name", "message", "transaction_id")
ERROR_CSV_CHUNK_ROWS = 500
# A spreadsheet runs cells starting with these as formulas; error rows contain
# text from uploaded files, so such values are prefixed with ' when exported.
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


class RateLimiter:
    """Allow at most max_requests per key in any sliding window of window_seconds.

    Kept in memory, so the limit applies per server process and resets on restart.
    """

    def __init__(
        self,
        max_requests: int,
        window_seconds: float = RATE_LIMIT_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._clock = clock
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()  # Sync endpoints run on several threads.

    def check(self, key: str) -> float | None:
        """Count a request for key. Returns None if allowed, else seconds to wait."""
        now = self._clock()
        with self._lock:
            recent = self._requests[key]
            while recent and recent[0] <= now - self.window_seconds:
                recent.popleft()
            if len(recent) >= self.max_requests:
                return recent[0] + self.window_seconds - now
            recent.append(now)
            return None


upload_rate_limiter = RateLimiter(get_settings().upload_rate_limit_per_minute)


def get_upload_rate_limiter() -> RateLimiter:
    return upload_rate_limiter


def enforce_upload_rate_limit(
    request: Request,
    limiter: Annotated[RateLimiter, Depends(get_upload_rate_limiter)],
) -> None:
    client = request.client.host if request.client else "unknown"
    wait_seconds = limiter.check(client)
    if wait_seconds is not None:
        retry_after = max(1, math.ceil(wait_seconds))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many uploads. Try again in {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)},
        )


@router.post(
    "/imports",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ImportUploadResponse,
    summary="Upload sales CSV files for import",
    description=(
        "Stages each file and returns at once; a background worker imports them. "
        "Poll each batch for its result. Returns 202 if at least one file was "
        "queued, 200 if every file was already uploaded, 400 if no file was usable."
    ),
    responses={
        200: {
            "model": ImportUploadResponse,
            "description": "All files were already uploaded.",
        },
        400: {"model": ImportUploadResponse, "description": "No file could be queued."},
        429: {"description": "Upload rate limit reached; see the Retry-After header."},
        503: {"description": "Database unavailable."},
    },
    dependencies=[Depends(enforce_upload_rate_limit)],
)
def upload_imports(
    files: Annotated[
        list[UploadFile], File(description="One or more sales CSV files.")
    ],
    background_tasks: BackgroundTasks,
    db: Annotated[Session, Depends(get_db)],
):
    settings = get_settings()
    if len(files) > settings.max_files_per_upload:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"At most {settings.max_files_per_upload} files per upload; "
            f"got {len(files)}.",
        )
    try:
        results = [
            stage_upload(db, file, settings.max_upload_size_bytes) for file in files
        ]
    except SQLAlchemyError:
        logger.exception("Could not stage uploaded files")
        # Files staged before the failure are kept; sending the same request
        # again reports them as ALREADY UPLOADED and stages the rest.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable. Please try again.",
        ) from None

    response = ImportUploadResponse(files=results)
    if response.queued_batch_ids:
        # Runs after the response is sent. Draining imports every staged batch;
        # several drains at once are safe because each batch is claimed atomically.
        background_tasks.add_task(run_worker, drain=True)
        return response
    no_file_usable = any(result.status == INVALID for result in results)
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST
        if no_file_usable
        else status.HTTP_200_OK,
        content=response.model_dump(mode="json"),
    )


def stage_upload(
    db: Session, upload: UploadFile, max_size_bytes: int
) -> UploadedFileResult:
    # Keep only the base name: a client could send "../../x.csv" as the filename.
    filename = Path(upload.filename or "").name or "upload.csv"
    if upload.size is not None and upload.size > max_size_bytes:
        return UploadedFileResult(
            filename=filename,
            status=INVALID,
            batch_id=None,
            message=f"File is {upload.size} bytes, which exceeds the "
            f"{max_size_bytes}-byte limit.",
        )
    # Never read more than one byte past the limit, so oversized bodies are cut off.
    content = upload.file.read(max_size_bytes + 1)
    outcome = enqueue_upload(db, filename, content, max_size_bytes)
    return UploadedFileResult(
        filename=filename,
        status=outcome.status,
        batch_id=outcome.batch_id,
        message=outcome.detail,
    )


@router.get(
    "/imports",
    response_model=BatchListResponse,
    summary="List import batches, newest first",
)
def list_imports(
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[
        int, Query(ge=1, le=MAX_PAGE_SIZE, description="Batches per page.")
    ] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0, description="Batches to skip.")] = 0,
):
    total = db.scalar(select(func.count()).select_from(ImportBatch))
    batches = db.scalars(
        select(ImportBatch)
        # id breaks ties so pages never overlap or skip batches.
        .order_by(ImportBatch.uploaded_at.desc(), ImportBatch.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return BatchListResponse(
        items=[BatchResponse.model_validate(batch) for batch in batches],
        total=total,
        limit=limit,
        offset=offset,
    )


def get_session_factory() -> sessionmaker[Session]:
    return SessionLocal


@router.get(
    "/imports/{batch_id}/errors.csv",
    summary="Download a batch's rejected rows as CSV",
    description=(
        "Columns: row_number, column_name, message, transaction_id, ordered by "
        "row number. A batch with no rejected rows returns just the header row."
    ),
    response_class=StreamingResponse,
    responses={
        200: {"content": {"text/csv": {}}, "description": "The rejected rows."},
        404: {"description": "Batch not found."},
    },
)
def download_import_errors(
    batch_id: uuid.UUID,
    db: Annotated[Session, Depends(get_db)],
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
):
    # Checked before streaming starts: once the body is streaming, the status
    # code can no longer change to 404.
    if db.get(ImportBatch, batch_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found."
        )
    return StreamingResponse(
        error_csv_chunks(session_factory, batch_id),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="batch-{batch_id}-errors.csv"'
        },
    )


def error_csv_chunks(
    session_factory: sessionmaker[Session], batch_id: uuid.UUID
) -> Iterator[str]:
    """Yield the CSV in chunks without loading every error row at once.

    Uses its own session: the request's session may be closed before the
    response body has finished streaming.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    # The BOM makes Excel read the file as UTF-8 (product names may contain ₹).
    buffer.write("\ufeff")
    writer.writerow(ERROR_CSV_COLUMNS)
    with session_factory() as session:
        rows = session.execute(
            select(
                ImportRowError.row_number,
                ImportRowError.column_name,
                ImportRowError.message,
                ImportRowError.transaction_id,
            )
            .where(ImportRowError.batch_id == batch_id)
            .order_by(ImportRowError.row_number)
            .execution_options(yield_per=ERROR_CSV_CHUNK_ROWS)
        )
        for partition in rows.partitions():
            for row_number, column_name, message, transaction_id in partition:
                writer.writerow(
                    [
                        row_number,
                        safe_cell(column_name),
                        safe_cell(message),
                        safe_cell(transaction_id or ""),
                    ]
                )
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate()
    if buffer.getvalue():  # Header only, when there were no rows.
        yield buffer.getvalue()


def safe_cell(value: str) -> str:
    return f"'{value}" if value.startswith(FORMULA_PREFIXES) else value
