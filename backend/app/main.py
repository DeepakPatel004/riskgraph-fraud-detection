from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse
from app.api.health import router as health_router
from app.api.transactions import router as transactions_router
from app.core.config import get_settings

app = FastAPI(title=get_settings().app_name, version="0.1.0")
app.include_router(health_router)
app.include_router(transactions_router)


@app.exception_handler(RequestValidationError)
async def validation_error(request, error):
    if request.url.path == "/api/v1/transactions/score":
        # Default validation errors echo rejected input, which may contain tokens.
        return JSONResponse(status_code=422, content={"detail": [
            {"loc": list(item["loc"]), "type": item["type"], "msg": item["msg"]}
            for item in error.errors()
        ]})
    return await request_validation_exception_handler(request, error)


@app.get("/")
def root():
    return {"message": "RiskGraph API", "docs": "/docs"}
