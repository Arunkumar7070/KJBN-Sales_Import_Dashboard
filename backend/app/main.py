import logging
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from api.routes import analytics_router, stores_router, transactions_router
from api.routes import router as imports_router
from app.clock import TEST_CLOCK_HEADER, InvalidTestClock, frozen_at, parse_test_clock
from app.config import get_settings
from app.database import check_database_connection, get_db

logger = logging.getLogger(__name__)

app = FastAPI(
    title="KJBN Sales Dashboard API",
    description="Backend API for sales imports and dashboard analytics.",
    version="0.1.0",
)
app.include_router(imports_router)
app.include_router(transactions_router)
app.include_router(analytics_router)
app.include_router(stores_router)


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
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST, content={"detail": str(error)}
        )
    with frozen_at(moment):
        return await call_next(request)


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
