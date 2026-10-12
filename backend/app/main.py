from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from app.api.scoring_errors import scoring_http_error, scoring_validation_error
from app.api.health import router as health_router
from app.api.alerts import router as alerts_router, alert_validation_error
from app.api.transactions import router as transactions_router
from app.core.config import get_settings

app = FastAPI(title=get_settings().app_name, version="0.1.0")
app.include_router(health_router)
app.include_router(transactions_router)
app.include_router(alerts_router)


@app.exception_handler(RequestValidationError)
async def validation_error(request, error):
    if request.url.path == "/api/v1/alerts" or request.url.path.startswith("/api/v1/alerts/"):
        return await alert_validation_error(request, error)
    return await scoring_validation_error(request, error)


app.add_exception_handler(StarletteHTTPException, scoring_http_error)


@app.get("/")
def root():
    return {"message": "RiskGraph API", "docs": "/docs"}
