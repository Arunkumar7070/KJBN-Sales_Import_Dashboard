import logging
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from api.routes import analytics_router, transactions_router
from api.routes import router as imports_router
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
