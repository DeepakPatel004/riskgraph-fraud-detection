"""Restricted local demo endpoints: authentication must precede public deployment."""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.alert import AlertPage, AlertRead, AlertUpdate, InvestigationStatus
from app.services.alert_service import alert_database_error, get_alert, list_alerts, update_alert


router = APIRouter(prefix="/api/v1/alerts", tags=["alerts"])


@router.get("", response_model=AlertPage,
            description="Restricted local demo. Alerts flag HIGH predictions, not confirmed fraud.")
def all_alerts(limit: int = Query(default=20, ge=1, le=100),
               offset: int = Query(default=0, ge=0),
               investigation_status: InvestigationStatus | None = Query(default=None),
               db: Session = Depends(get_db)):
    try:
        return list_alerts(db, limit, offset, investigation_status)
    except SQLAlchemyError:
        alert_database_error(db)


@router.get("/{alert_id}", response_model=AlertRead)
def by_id(alert_id: str, db: Session = Depends(get_db)):
    try:
        return get_alert(db, alert_id)
    except SQLAlchemyError:
        alert_database_error(db)


@router.patch("/{alert_id}", response_model=AlertRead)
def update(alert_id: str, payload: AlertUpdate, db: Session = Depends(get_db)):
    try:
        return update_alert(db, alert_id, payload)
    except SQLAlchemyError:
        alert_database_error(db)


async def alert_validation_error(request, error):
    """Reject invalid input without echoing request values or exception context."""
    return JSONResponse(status_code=422, content={"detail": [
        {"loc": list(item["loc"]), "type": item["type"], "msg": item["msg"]}
        for item in error.errors()
    ]})
