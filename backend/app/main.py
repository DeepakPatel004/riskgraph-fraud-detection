from fastapi import FastAPI
from app.api.health import router as health_router
from app.api.transactions import router as transactions_router
from app.core.config import get_settings

app = FastAPI(title=get_settings().app_name, version="0.1.0")
app.include_router(health_router)
app.include_router(transactions_router)


@app.get("/")
def root():
    return {"message": "RiskGraph API", "docs": "/docs"}
