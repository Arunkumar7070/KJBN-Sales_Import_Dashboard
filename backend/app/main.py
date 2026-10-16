
from fastapi import FastAPI

app = FastAPI(
    title="KJBN Sales Dashboard API",
    description="Backend API for sales imports and dashboard analytics.",
    version="0.1.0",
)


@app.get("/health", tags=["Health"])
def health_check():
    return {
        "status": "Ok",
        "message": "Health Checking",
    }
