"""API endpoints: CSV imports (upload, history, row errors) and transaction search."""

import csv
import io
import logging
import math
import threading
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Callable, Iterator
from datetime import date, datetime
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
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app import clock
from app.config import get_settings
from app.database import SessionLocal, get_db
from app.logging_config import current_request_id, request_context
from app.models import ImportBatch, ImportRowError, Store, Transaction
from app.schemas import (
    BatchListResponse,
    BatchResponse,
    DashboardResponse,
    ImportUploadResponse,
    StoreImportResponse,
    StoreImportRowError,
    StoreResponse,
    TransactionListResponse,
    TransactionResponse,
    UploadedFileResult,
)
from app.services.analytics import build_dashboard, sold_within_ist_days
from app.services.csv_validator import (
    INVALID,
    CsvFileError,
    enqueue_upload,
    run_worker,
)
from app.services.import_worker import without_echo
from app.services.store_import import import_stores

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
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["files"],
                        "properties": {
                            "files": {
                                "type": "array",
                                "items": {"type": "string", "format": "binary"},
                                "description": "One or more sales CSV files.",
                            }
                        },
                    }
                }
            },
        }
    },
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
        # Runs after the response is sent, when this request's context is gone,
        # so the frozen test time (if any) and the request ID are handed over.
        background_tasks.add_task(
            import_in_background,
            response.queued_batch_ids,
            clock.frozen_now(),
            current_request_id(),
        )
        return response
    no_file_usable = any(result.status == INVALID for result in results)
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST
        if no_file_usable
        else status.HTTP_200_OK,
        content=response.model_dump(mode="json"),
    )


def import_in_background(
    batch_ids: list[uuid.UUID], frozen_now: datetime | None, request_id: str | None
) -> None:
    """Import staged files after the response has been sent.

    With a test clock, only this request's batches are imported, under that
    clock. Otherwise every staged batch is drained; several drains at once are
    safe because each batch is claimed atomically. Logs carry the request ID of
    the upload that started the import.
    """
    with request_context(request_id), clock.frozen_at(frozen_now), without_echo():
        run_worker(drain=True, batch_ids=batch_ids if frozen_now else None)


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


transactions_router = APIRouter(prefix="/api/v1", tags=["Transactions"])


@transactions_router.get(
    "/transactions",
    response_model=TransactionListResponse,
    summary="Search transactions",
    description=(
        "All filters are optional and combine with AND. Dates are IST calendar "
        "days and both ends are inclusive. Results are newest sale first."
    ),
)
def list_transactions(
    db: Annotated[Session, Depends(get_db)],
    store_code: Annotated[
        str | None,
        Query(description="Only this store (exact match).", examples=["ST01"]),
    ] = None,
    sku: Annotated[
        str | None,
        Query(description="Only this product (exact match).", examples=["SKU1"]),
    ] = None,
    date_from: Annotated[
        date | None, Query(description="First day to include (IST), YYYY-MM-DD.")
    ] = None,
    date_to: Annotated[
        date | None, Query(description="Last day to include (IST), YYYY-MM-DD.")
    ] = None,
    returns_only: Annotated[
        bool, Query(description="true: only returns. false: sales and returns.")
    ] = False,
    page: Annotated[int, Query(ge=1, description="Page number, starting at 1.")] = 1,
    limit: Annotated[
        int, Query(ge=1, le=MAX_PAGE_SIZE, description="Transactions per page.")
    ] = DEFAULT_PAGE_SIZE,
):
    check_date_range(date_from, date_to)
    # Built once and used by both queries, so total always matches the items.
    filters = transaction_filters(store_code, sku, date_from, date_to, returns_only)

    total = db.scalar(select(func.count()).select_from(Transaction).where(*filters))
    transactions = db.scalars(
        select(Transaction)
        .where(*filters)
        # transaction_id breaks ties so pages never overlap or skip rows.
        .order_by(Transaction.sold_at.desc(), Transaction.transaction_id.desc())
        .limit(limit)
        .offset((page - 1) * limit)
    ).all()
    return TransactionListResponse(
        items=[TransactionResponse.model_validate(t) for t in transactions],
        total=total,
        page=page,
        limit=limit,
    )


def transaction_filters(
    store_code: str | None,
    sku: str | None,
    date_from: date | None,
    date_to: date | None,
    returns_only: bool,
) -> list[ColumnElement[bool]]:
    """WHERE conditions for the filters that were given; none means everything."""
    filters = []
    if store_code is not None:
        filters.append(Transaction.store_code == store_code)
    if sku is not None:
        filters.append(Transaction.sku == sku)
    filters.extend(sold_within_ist_days(date_from, date_to))
    if returns_only:
        filters.append(Transaction.is_return.is_(True))
    return filters


def check_date_range(date_from: date | None, date_to: date | None) -> None:
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="date_from must be on or before date_to.",
        )


analytics_router = APIRouter(prefix="/api/v1/analytics", tags=["Analytics"])


@analytics_router.get(
    "/dashboard",
    response_model=DashboardResponse,
    summary="Dashboard analytics",
    description=(
        "Daily revenue, store revenue, top 10 products by net units, store return "
        "rates and payment split. Dates are IST calendar days, both inclusive; "
        "without dates, all transactions are used."
    ),
)
def get_dashboard(
    db: Annotated[Session, Depends(get_db)],
    date_from: Annotated[
        date | None, Query(description="First day to include (IST), YYYY-MM-DD.")
    ] = None,
    date_to: Annotated[
        date | None, Query(description="Last day to include (IST), YYYY-MM-DD.")
    ] = None,
):
    check_date_range(date_from, date_to)
    return build_dashboard(db, date_from, date_to)


stores_router = APIRouter(prefix="/api/v1", tags=["Stores"])


@stores_router.get(
    "/stores",
    response_model=list[StoreResponse],
    summary="List all stores",
    description="Every store, ordered by code; use it to fill store filters.",
)
def list_stores(db: Annotated[Session, Depends(get_db)]):
    return db.scalars(select(Store).order_by(Store.code)).all()


@stores_router.post(
    "/stores",
    response_model=StoreImportResponse,
    summary="Import stores from CSV (insert or update)",
    description=(
        "CSV columns: code, name, city (any order). A new code is inserted; an "
        "existing code gets its name and city updated. Bad rows are skipped and "
        "listed in errors; a bad file (header, encoding, type, size) is a 400."
    ),
    responses={
        400: {"description": "The file can't be used; nothing was imported."},
        429: {"description": "Upload rate limit reached; see the Retry-After header."},
        503: {"description": "Database unavailable; nothing was imported."},
    },
    dependencies=[Depends(enforce_upload_rate_limit)],
)
def import_stores_csv(
    file: Annotated[UploadFile, File(description="Stores CSV: code,name,city.")],
    db: Annotated[Session, Depends(get_db)],
):
    max_size_bytes = get_settings().max_upload_size_bytes
    # Never read more than one byte past the limit, so oversized bodies are cut off.
    content = file.file.read(max_size_bytes + 1)
    try:
        summary = import_stores(
            db, Path(file.filename or "").name or "stores.csv", content, max_size_bytes
        )
    except CsvFileError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
        ) from None
    except SQLAlchemyError:
        db.rollback()
        logger.exception("Could not import stores")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable; no stores were imported. Please try again.",
        ) from None
    return StoreImportResponse(
        rows_total=summary.rows_total,
        inserted=summary.inserted,
        updated=summary.updated,
        unchanged=summary.unchanged,
        rejected=len(summary.errors),
        errors=[
            StoreImportRowError(
                row_number=e.row_number, column_name=e.column_name, message=e.message
            )
            for e in summary.errors
        ],
    )
