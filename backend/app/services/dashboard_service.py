"""Read-only, portable SQL aggregates. Never load transaction/alert ORM rows."""

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import and_, case, func, select, true
from sqlalchemy.orm import Session

from app.models import FraudAlert, Transaction
from app.schemas.dashboard import (
    AlertStatusCounts, DailyTransactionCount, DashboardSummary, RiskDistribution, TransactionTrends,
)


DEFAULT_TREND_DAYS = 30
MAX_TREND_DAYS = 90


class DashboardRangeError(ValueError):
    """A date window cannot be served; message text is an application constant."""


class DashboardDataError(RuntimeError):
    """Stored state cannot be represented by the supported dashboard categories."""


def count_when(condition):
    # COUNT ignores NULL (the implicit ELSE), and returns zero for an empty table.
    return func.count(case((condition, 1)))


def transaction_counts_query():
    scored = Transaction.risk_score.is_not(None)
    return select(
        func.count().label("total_transactions"),
        count_when(scored).label("scored_transactions"),
        count_when(Transaction.risk_score.is_(None)).label("unscored_transactions"),
        count_when(and_(scored, Transaction.risk_level == "HIGH")).label("high_risk_transactions"),
        count_when(and_(scored, Transaction.risk_level == "LOW")).label("low_risk_transactions"),
    ).select_from(Transaction)


def alert_counts_query():
    return select(
        func.count().label("total_alerts"),
        count_when(FraudAlert.investigation_status == "OPEN").label("open_alerts"),
        count_when(FraudAlert.investigation_status == "IN_REVIEW").label("in_review_alerts"),
        count_when(FraudAlert.investigation_status == "CLOSED").label("closed_alerts"),
    ).select_from(FraudAlert)


def check_transaction_counts(counts):
    # Never guess LOW/HIGH for a score with a missing/unsupported saved label.
    if counts["high_risk_transactions"] + counts["low_risk_transactions"] != counts["scored_transactions"]:
        raise DashboardDataError("Dashboard data is inconsistent")


def check_alert_counts(counts):
    if sum(counts[key] for key in ("open_alerts", "in_review_alerts", "closed_alerts")) != counts["total_alerts"]:
        raise DashboardDataError("Dashboard data is inconsistent")


def trend_dates(start_date: date, end_date: date) -> list[date]:
    """Validate inclusive calendar dates before building any SQL bucket expressions."""
    if start_date > end_date:
        raise DashboardRangeError("start_date must not be after end_date")
    days = (end_date - start_date).days + 1
    if days > MAX_TREND_DAYS:
        raise DashboardRangeError("Date range must not exceed 90 days inclusive")
    if end_date == date.max:
        raise DashboardRangeError("end_date must be earlier than 9999-12-31")
    return [start_date + timedelta(days=i) for i in range(days)]


def daily_counts_query(dates: list[date]):
    """One SQL aggregate with at most 90 COUNT(CASE ...) UTC-day buckets.

    Avoid DATE()/date_trunc(), whose timezone behavior differs by database.
    The existing UTCDateTime binds each boundary appropriately for SQLite or
    PostgreSQL. Overall bounds permit the existing event_time index to be used.
    Only one aggregate row is returned; never fetch raw transactions into Python.
    """
    lower = datetime.combine(dates[0], datetime.min.time(), tzinfo=timezone.utc)
    upper = datetime.combine(dates[-1] + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    buckets = []
    for i, day in enumerate(dates):
        start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
        buckets.append(count_when(and_(Transaction.event_time >= start,
                                       Transaction.event_time < start + timedelta(days=1))).label(f"day_{i}"))
    return select(*buckets).select_from(Transaction).where(
        Transaction.event_time >= lower, Transaction.event_time < upper,
    )


class DashboardService:
    def __init__(self, db: Session):
        self.db = db

    def summary(self) -> DashboardSummary:
        # Join two one-row aggregates, never the underlying transaction/alert rows.
        # This avoids inflated totals and keeps this response in one SQL statement.
        transactions = transaction_counts_query().subquery()
        alerts = alert_counts_query().subquery()
        counts = self.db.execute(select(*transactions.c, *alerts.c).select_from(
            transactions.join(alerts, true()),
        )).mappings().one()
        check_transaction_counts(counts)
        check_alert_counts(counts)
        return DashboardSummary(**counts)

    def risk_distribution(self) -> RiskDistribution:
        counts = self.db.execute(transaction_counts_query()).mappings().one()
        check_transaction_counts(counts)
        return RiskDistribution(LOW=counts["low_risk_transactions"], HIGH=counts["high_risk_transactions"],
                                UNSCORED=counts["unscored_transactions"])

    def alert_status(self) -> AlertStatusCounts:
        counts = self.db.execute(alert_counts_query()).mappings().one()
        check_alert_counts(counts)
        return AlertStatusCounts(OPEN=counts["open_alerts"], IN_REVIEW=counts["in_review_alerts"],
                                 CLOSED=counts["closed_alerts"])

    def transaction_trends(self, start_date: date | None = None, end_date: date | None = None) -> TransactionTrends:
        """Both endpoints inclusive; partial dates select a bounded 30-day window.

        Only start: 30 days forward. Only end: up to 30 days backward. Neither:
        end at the latest stored UTC event date, or return null bounds/empty items
        if there are no events. No wall clock dependence or model/graph calls.
        """
        if start_date is None and end_date is None:
            latest = self.db.scalar(select(func.max(Transaction.event_time)))
            if latest is None:
                return TransactionTrends(start_date=None, end_date=None, items=[])
            end_date = latest.date()
        if start_date is None:
            start_date = date.fromordinal(max(date.min.toordinal(), end_date.toordinal() - DEFAULT_TREND_DAYS + 1))
        elif end_date is None:
            try:
                end_date = start_date + timedelta(days=DEFAULT_TREND_DAYS - 1)
            except OverflowError:
                raise DashboardRangeError("Date window exceeds supported calendar bounds") from None
        dates = trend_dates(start_date, end_date)
        counts = self.db.execute(daily_counts_query(dates)).one()
        return TransactionTrends(start_date=start_date, end_date=end_date,
                                 items=[DailyTransactionCount(date=day, transaction_count=count)
                                        for day, count in zip(dates, counts)])
