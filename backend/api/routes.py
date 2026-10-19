"""Import endpoints: upload sales CSV files to be imported in the background."""

import logging
import math
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.schemas import ImportUploadResponse, UploadedFileResult
from app.services.csv_validator import INVALID, enqueue_upload, run_worker

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["Imports"])

RATE_LIMIT_WINDOW_SECONDS = 60.0


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
