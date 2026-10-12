from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from app.api.scoring_errors import scoring_http_error, scoring_validation_error
from app.api.health import router as health_router
from app.api.transactions import router as transactions_router
from app.core.config import get_settings

app = FastAPI(title=get_settings().app_name, version="0.1.0")
app.include_router(health_router)
app.include_router(transactions_router)


app.add_exception_handler(RequestValidationError, scoring_validation_error)
app.add_exception_handler(StarletteHTTPException, scoring_http_error)


@app.get("/")
def root():
    return {"message": "RiskGraph API", "docs": "/docs"}
