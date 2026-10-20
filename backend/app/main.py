import logging
import time
from http import HTTPStatus
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.routes import analytics_router, stores_router, transactions_router
from api.routes import router as imports_router
from app.clock import TEST_CLOCK_HEADER, InvalidTestClock, frozen_at, parse_test_clock
from app.config import get_settings
from app.database import check_database_connection, get_db
from app.logging_config import (
    REQUEST_ID_HEADER,
    configure_logging,
    current_request_id,
    request_context,
    request_id_from_header,
)

configure_logging()

logger = logging.getLogger(__name__)
request_logger = logging.getLogger("app.requests")

app = FastAPI(
    title="KJBN Sales Dashboard API",
    description="Backend API for sales imports and dashboard analytics.",
    version="0.1.0",
)
app.include_router(imports_router)
app.include_router(transactions_router)
app.include_router(analytics_router)
app.include_router(stores_router)


# Error responses: every error, from any source, has the same JSON shape:
#   {"error": {"status": 404, "code": "not_found", "message": "...",
#              "request_id": "...", "details": [...]}}
# "details" is only present for validation errors.


def error_response(
    status_code: int,
    message: str,
    *,
    code: str | None = None,
    details: list[dict] | None = None,
    headers: dict[str, str] | None = None,
    request_id: str | None = None,
) -> JSONResponse:
    error = {
        "status": status_code,
        "code": code or status_code_name(status_code),
        "message": message,
        "request_id": request_id or current_request_id(),
    }
    if details is not None:
        error["details"] = details
    return JSONResponse(
        status_code=status_code, content={"error": error}, headers=headers
    )


def status_code_name(status_code: int) -> str:
    try:
        return (
            HTTPStatus(status_code).phrase.lower().replace(" ", "_").replace("-", "_")
        )
    except ValueError:
        return "error"


@app.exception_handler(StarletteHTTPException)
async def handle_http_exception(request: Request, exc: StarletteHTTPException):
    """Errors raised on purpose (404, 429, 503, ...), plus unknown routes and methods."""
    message = (
        exc.detail if isinstance(exc.detail, str) else status_code_name(exc.status_code)
    )
    return error_response(exc.status_code, message, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, exc: RequestValidationError):
    """400 for a body that isn't valid JSON at all; 422 for well-formed but invalid input."""
    errors = exc.errors()
    if any(error["type"] == "json_invalid" for error in errors):
        return error_response(
            status.HTTP_400_BAD_REQUEST,
            "Request body is not valid JSON.",
            code="malformed_json",
        )
    return error_response(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "Some request values are missing or invalid.",
        code="validation_error",
        # The submitted values ("input") are left out so responses never echo user data.
        details=[
            {
                "field": ".".join(str(part) for part in error["loc"]),
                "message": error["msg"],
                "type": error["type"],
            }
            for error in errors
        ],
    )


INTERNAL_ERROR_MESSAGE = "Something went wrong on our side. Please try again later."


@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, exc: Exception):
    """Last resort; correlate_and_log_request normally handles these first."""
    logger.error("Unhandled error", exc_info=exc)
    return error_response(status.HTTP_500_INTERNAL_SERVER_ERROR, INTERNAL_ERROR_MESSAGE)


@app.middleware("http")
async def apply_test_clock(request: Request, call_next):
    """Freeze app.clock.utc_now() for this request when X-Test-Clock is sent.

    Only honoured when ALLOW_TEST_CLOCK is on; otherwise the header is ignored.
    """
    header = request.headers.get(TEST_CLOCK_HEADER)
    if header is None or not get_settings().allow_test_clock:
        return await call_next(request)
    try:
        moment = parse_test_clock(header)
    except InvalidTestClock as error:
        return error_response(status.HTTP_400_BAD_REQUEST, str(error))
    with frozen_at(moment):
        return await call_next(request)


# Added after apply_test_clock, so it runs first (outermost): every response,
# including early rejections such as a bad X-Test-Clock, gets a request ID.
@app.middleware("http")
async def correlate_and_log_request(request: Request, call_next):
    """Tag the request with an ID, echo it back, and log one line when done.

    Only metadata is logged (method, path, status, duration), never bodies.
    Unexpected errors are caught here, while the request ID is still known:
    the full traceback goes to the log, and the client gets a plain 500.
    """
    request_id = request_id_from_header(request.headers.get(REQUEST_ID_HEADER))
    path = request.url.path
    started = time.perf_counter()
    with request_context(request_id, path):
        try:
            response = await call_next(request)
        except Exception:
            request_logger.exception(
                "Unhandled error",
                extra=request_log_fields(request, 500, started),
            )
            response = error_response(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                INTERNAL_ERROR_MESSAGE,
                request_id=request_id,
            )
            response.headers[REQUEST_ID_HEADER] = request_id
            return response
        response.headers[REQUEST_ID_HEADER] = request_id
        request_logger.info(
            "Request completed",
            extra=request_log_fields(request, response.status_code, started),
        )
        return response


# Added last, so it runs outermost: preflight requests are answered here, and
# every response, including 4xx/5xx errors, carries the CORS headers (without
# them the browser hides the real error behind a CORS failure).
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    allow_headers=[
        "Content-Type",
        "Authorization",
        REQUEST_ID_HEADER,
        TEST_CLOCK_HEADER,
    ],
    # Response headers the frontend's JavaScript may read.
    expose_headers=[REQUEST_ID_HEADER, "Retry-After", "Content-Disposition"],
    max_age=600,  # Browsers may cache a preflight answer for 10 minutes.
)


def request_log_fields(request: Request, status_code: int, started: float) -> dict:
    return {
        "method": request.method,
        "status_code": status_code,
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
    }


@app.get("/health", tags=["Health"])
def health_check():
    return {
        "status": "Ok",
        "message": "Health Checking",
    }


@app.get("/health/db", tags=["Health"])
def database_health_check(db: Annotated[Session, Depends(get_db)]):
    try:
        check_database_connection(db)
    except SQLAlchemyError:
        logger.exception("Database health check failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable",
        )
    return {"status": "Ok", "database": "connected"}
