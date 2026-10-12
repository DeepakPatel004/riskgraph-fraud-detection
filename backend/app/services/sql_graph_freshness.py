"""Conservative SQL history checks for one explicitly verified graph population."""

from contextlib import contextmanager
from datetime import timedelta

from sqlalchemy import or_, select

from app.models import GraphSyncJob, Transaction
from app.services.fraud_scoring_service import GraphFreshness
from app.services.graph_population import GraphPopulation


class SQLGraphFreshnessChecker:
    def __init__(self, session_factory, population: GraphPopulation, gate):
        self.session_factory = session_factory
        self.population = population
        self.gate = gate

    @contextmanager
    def guard(self, request):
        # SQL session closes BEFORE yielding to any external graph/model call.
        status = GraphFreshness.UNKNOWN
        if self.population.verified and self.population.identifier and self.gate.held:
            relevant = [Transaction.card_token == request.card_token]
            if request.device_token is not None:
                relevant.append(Transaction.device_token == request.device_token)
            incomplete = or_(
                GraphSyncJob.job_id.is_(None), GraphSyncJob.status != "SYNCED",
                GraphSyncJob.lease_token.is_not(None), Transaction.graph_sync_status != "SYNCED",
                GraphSyncJob.graph_population_id.is_(None),
                GraphSyncJob.graph_population_id != self.population.identifier,
                Transaction.currency != "USD",
            )
            with self.session_factory() as db:
                blocked = db.scalar(select(Transaction.transaction_id).outerjoin(
                    GraphSyncJob, GraphSyncJob.transaction_id == Transaction.transaction_id,
                ).where(
                    Transaction.event_time >= request.event_time - timedelta(days=7),
                    Transaction.event_time < request.event_time, or_(*relevant), incomplete,
                ).limit(1))
            status = GraphFreshness.STALE if blocked else GraphFreshness.FRESH
        yield status
