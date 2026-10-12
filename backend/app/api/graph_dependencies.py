"""Lazy explorer population factory; construction and module imports make no AWS calls."""

from app.api.scoring_errors import ScoringAPIError


def get_graph_population_factory():
    # Existence/sync checks happen before constructing the configured graph client.
    return build_graph_population


def build_graph_population(engine):
    from app.api.scoring_dependencies import _graph
    from app.core.config import get_settings
    from app.services.graph_population import GraphPopulation, neptune_population_id

    settings = get_settings()
    identity = neptune_population_id(settings, engine)
    if not settings.graph_population_verified or identity is None or settings.scoring_api_workers != 1:
        raise ScoringAPIError(503, "Graph explorer requires a verified configured population",
                              code="GRAPH_POPULATION_UNVERIFIED")
    try:
        graph = _graph(settings.neptune_endpoint, settings.neptune_port, settings.neptune_timeout,
                       settings.neptune_iam_auth_enabled, settings.neptune_region, identity)
    except (ValueError, ImportError):
        raise ScoringAPIError(503, "Graph explorer configuration is unavailable", code="GRAPH_CONFIGURATION_UNAVAILABLE") from None
    return GraphPopulation(graph, identity, verified=True)
