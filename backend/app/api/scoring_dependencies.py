"""Lazy, fail-closed scoring wiring; override this factory in offline API tests."""

from functools import lru_cache

from fastapi import HTTPException


@lru_cache(maxsize=1)
def _graph(endpoint, port, timeout, iam, region, population_id):
    from app.graph.neptune import NeptuneGraphRepository
    from app.graph.neptune_client import NeptuneClient
    return NeptuneGraphRepository(NeptuneClient(
        endpoint, port=port, timeout=timeout, iam_auth_enabled=iam, region=region,
    ))


def get_scoring_factory():
    # Returning a function (rather than constructing services) lets replay work
    # even when the model/graph is temporarily offline or scoring is disabled.
    return build_scoring_service


def build_scoring_service(engine, gate):
    from sqlalchemy.orm import sessionmaker
    from app.core.config import get_settings
    from app.services.fraud_inference_service import get_fraud_inference_service
    from app.services.fraud_scoring_service import FraudScoringService
    from app.services.graph_population import GraphPopulation, neptune_population_id
    from app.services.sql_graph_freshness import SQLGraphFreshnessChecker
    settings = get_settings()
    identity = neptune_population_id(settings, engine)
    if (not settings.fraud_scoring_enabled or not settings.graph_population_verified
            or settings.scoring_api_workers != 1 or identity is None):
        raise HTTPException(503, "Scoring requires a verified Neptune population and single-host SQLite policy")
    try:
        graph = _graph(settings.neptune_endpoint, settings.neptune_port, settings.neptune_timeout,
                       settings.neptune_iam_auth_enabled, settings.neptune_region, identity)
    except ValueError:
        raise HTTPException(503, "Neptune scoring configuration is unavailable") from None
    population = GraphPopulation(graph, identity, verified=True)
    checker = SQLGraphFreshnessChecker(sessionmaker(bind=engine), population, gate)
    inference = (get_fraud_inference_service(settings.fraud_model_artifact_dir)
                 if settings.fraud_model_artifact_dir else get_fraud_inference_service())
    return FraudScoringService(population.graph, inference, checker)
