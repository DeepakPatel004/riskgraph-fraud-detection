"""Identity shared by a dedicated Neptune population and its SQL source."""

import hashlib
import json
import os
from dataclasses import dataclass

from app.graph.repository import GraphRepository
from app.services.ingestion_gate import sqlite_source_path


@dataclass(frozen=True)
class GraphPopulation:
    graph: GraphRepository
    identifier: str
    verified: bool = False


def neptune_population_id(settings, engine) -> str | None:
    """Bind worker acknowledgements to source DB, endpoint and operator epoch.

    The graph must be dedicated to this SQL source. Rotating the population name
    after graph restore/replacement invalidates old acknowledgements.
    """
    source = sqlite_source_path(engine)
    if (settings.graph_sync_adapter != "neptune" or not settings.graph_population_name
            or not settings.neptune_endpoint or source is None):
        return None
    identity = [os.path.normcase(str(source)), settings.neptune_endpoint.lower(),
                settings.neptune_port, settings.graph_population_name]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()
