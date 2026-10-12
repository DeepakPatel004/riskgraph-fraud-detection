"""SQL-only alert queries and investigation updates; no model or graph calls."""

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import FraudAlert
from app.schemas.alert import AlertUpdate, InvestigationStatus


def get_alert(db: Session, alert_id: str) -> FraudAlert:
    alert = db.get(FraudAlert, alert_id)
    if alert is None:
        raise HTTPException(404, "Alert not found")
    return alert


def list_alerts(db: Session, limit: int, offset: int, status: InvestigationStatus | None):
    """Newest first with a stable ID tie-breaker; total counts the filtered rows."""
    query = select(FraudAlert)
    count = select(func.count()).select_from(FraudAlert)
    if status is not None:
        query = query.where(FraudAlert.investigation_status == status)
        count = count.where(FraudAlert.investigation_status == status)
    rows = db.scalars(query.order_by(FraudAlert.created_at.desc(), FraudAlert.alert_id.desc())
                      .limit(limit).offset(offset)).all()
    return {"items": rows, "total": db.scalar(count) or 0, "limit": limit, "offset": offset}


def update_alert(db: Session, alert_id: str, data: AlertUpdate) -> FraudAlert:
    """Allow any of the three statuses, including reopening; same status is a no-op.

    SQLAlchemy's existing UTC onupdate default sets updated_at on actual changes.
    Concurrent operator updates use last-write-wins in this first local version.
    """
    alert = get_alert(db, alert_id)
    alert.investigation_status = data.investigation_status
    db.commit()
    db.refresh(alert)
    return alert


def alert_database_error(db: Session):
    """Never return SQL, credentials, or driver exception messages to callers."""
    db.rollback()
    raise HTTPException(500, "Alert database operation failed") from None
