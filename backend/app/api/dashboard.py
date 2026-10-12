"""Thin dashboard routes: SQL only; restricted local demo without authentication."""

from datetime import date
from collections.abc import Callable

from fastapi import APIRouter, Depends
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.scoring_errors import ScoringAPIError
from app.core.database import get_db
from app.schemas.dashboard import AlertStatusCounts, DashboardSummary, RiskDistribution, TransactionTrends
from app.schemas.fraud_scoring import ScoringErrorResponse
from app.services.dashboard_service import DashboardDataError, DashboardRangeError, DashboardService


router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"],
                   responses={status: {"model": ScoringErrorResponse} for status in (422, 503, 500)})


def read_dashboard(db: Session, operation: Callable):
    try:
        return operation()
    except DashboardRangeError as error:
        raise ScoringAPIError(422, str(error), code="VALIDATION_ERROR") from None
    except SQLAlchemyError:
        db.rollback()
        raise ScoringAPIError(503, "Dashboard database is unavailable", code="DASHBOARD_DATABASE_UNAVAILABLE",
                              retryable=True, headers={"Retry-After": "2"}) from None
    except DashboardDataError:
        raise ScoringAPIError(500, "Dashboard data is inconsistent", code="DASHBOARD_DATA_INCONSISTENT") from None
    except Exception:
        db.rollback()
        raise ScoringAPIError(500, "Dashboard request failed", code="DASHBOARD_FAILED") from None


@router.get("/summary", response_model=DashboardSummary)
def summary(db: Session = Depends(get_db)):
    return read_dashboard(db, DashboardService(db).summary)


@router.get("/risk-distribution", response_model=RiskDistribution)
def risk_distribution(db: Session = Depends(get_db)):
    return read_dashboard(db, DashboardService(db).risk_distribution)


@router.get("/transaction-trends", response_model=TransactionTrends)
def transaction_trends(start_date: date | None = None, end_date: date | None = None, db: Session = Depends(get_db)):
    return read_dashboard(db, lambda: DashboardService(db).transaction_trends(start_date, end_date))


@router.get("/alert-status", response_model=AlertStatusCounts)
def alert_status(db: Session = Depends(get_db)):
    return read_dashboard(db, DashboardService(db).alert_status)
