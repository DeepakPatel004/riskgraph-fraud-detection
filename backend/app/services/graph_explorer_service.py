"""Read a bounded network only after SQL acknowledgement/population verification."""

from collections import deque
from collections.abc import Callable
import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.scoring_errors import ScoringAPIError
from app.graph.models import CardNode, DeviceNode, GraphNetwork, NodeRef, TransactionNode
from app.graph.neptune_client import NeptuneError
from app.models import GraphSyncJob, Transaction
from app.schemas.graph_explorer import ExplorerEdge, ExplorerNode, GraphExplorerResponse
from app.services.graph_population import GraphPopulation


def unavailable(code: str, *, retryable=False):
    raise ScoringAPIError(503, "Transaction graph is unavailable or unverified",
                          code=code, retryable=retryable,
                          headers={"Retry-After": "2"} if retryable else None)


def require_synced(transaction, job, population_id=None):
    if transaction.graph_sync_status in {"PENDING", "PROCESSING"}:
        unavailable("GRAPH_SYNC_PENDING", retryable=True)
    if transaction.graph_sync_status == "FAILED":
        unavailable("GRAPH_SYNC_FAILED")
    if (transaction.graph_sync_status != "SYNCED" or job is None or job.status != "SYNCED"
            or job.lease_token is not None or not job.graph_population_id):
        unavailable("GRAPH_SYNC_UNVERIFIED")
    if population_id is not None and job.graph_population_id != population_id:
        unavailable("GRAPH_POPULATION_MISMATCH")


def source_node(row) -> TransactionNode:
    return TransactionNode(
        transaction_id=row.transaction_id, amount=row.amount, currency=row.currency,
        card_token=row.card_token, device_token=row.device_token or None, event_time=row.event_time,
    )


def display_id(ref: NodeRef) -> str:
    """Stable namespaced SHA-256 IDs, matching the existing Neptune ID convention.

    Hashes hide original tokens but are pseudonyms, not anonymization or access
    control. Guessable source tokens remain susceptible to dictionary matching.
    """
    return f"{ref.kind}:" + hashlib.sha256(ref.identifier.encode("utf-8")).hexdigest()


def validate_network(network: GraphNetwork, transaction_id: str, limit: int):
    """Fail closed on malformed/disconnected data, rather than display a false graph."""
    seed = NodeRef("Transaction", transaction_id)
    if not isinstance(network, GraphNetwork) or not 1 <= len(network.nodes) <= limit:
        unavailable("GRAPH_INCONSISTENT")
    if any(not isinstance(node, (TransactionNode, CardNode, DeviceNode)) for node in network.nodes):
        unavailable("GRAPH_INCONSISTENT")
    nodes = {node.ref: node for node in network.nodes}
    if len(nodes) != len(network.nodes) or network.nodes[0].ref != seed:
        unavailable("GRAPH_INCONSISTENT")
    edges = set(network.relationships)
    if len(edges) != len(network.relationships):
        unavailable("GRAPH_INCONSISTENT")
    neighbors = {ref: set() for ref in nodes}
    for edge in edges:
        if edge.source not in nodes or edge.target not in nodes or edge.source.kind != "Transaction":
            unavailable("GRAPH_INCONSISTENT")
        node = nodes[edge.source]
        target = {"USES_CARD": NodeRef("Card", node.card_token),
                  "USES_DEVICE": NodeRef("Device", node.device_token)}.get(edge.kind)
        if target != edge.target:
            unavailable("GRAPH_INCONSISTENT")
        neighbors[edge.source].add(edge.target)
        neighbors[edge.target].add(edge.source)
    # Returned edges must include all expected relationships between selected nodes.
    # If below the cap, the traversal exhausted, so mandatory token nodes must exist.
    for node in network.nodes:
        if not isinstance(node, TransactionNode):
            continue
        expected = [(NodeRef("Card", node.card_token), "USES_CARD")]
        if node.device_token is not None:
            expected.append((NodeRef("Device", node.device_token), "USES_DEVICE"))
        for target, kind in expected:
            if target not in nodes:
                if len(nodes) < limit:
                    unavailable("GRAPH_INCONSISTENT")
            elif not any(edge.source == node.ref and edge.target == target and edge.kind == kind for edge in edges):
                unavailable("GRAPH_INCONSISTENT")
    seen, queue = {seed}, deque([seed])
    while queue:
        for ref in neighbors[queue.popleft()] - seen:
            seen.add(ref)
            queue.append(ref)
    if seen != set(nodes):
        unavailable("GRAPH_INCONSISTENT")


class GraphExplorerService:
    """No writes, no inference, and no open SQL transaction during graph I/O.

    Check returned SQL transaction acknowledgements again after the graph read.
    This detects state changes, but is not a cross-database snapshot guarantee.
    """

    def __init__(self, population_factory: Callable[..., GraphPopulation]):
        self.population_factory = population_factory

    def explore(self, db: Session, transaction_id: str, limit: int = 50) -> GraphExplorerResponse:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("Explorer limit must be an integer between 1 and 100")
        row = db.execute(select(Transaction, GraphSyncJob).outerjoin(
            GraphSyncJob, GraphSyncJob.transaction_id == Transaction.transaction_id,
        ).where(Transaction.transaction_id == transaction_id)).one_or_none()
        if row is None:
            raise ScoringAPIError(404, "Transaction not found", code="TRANSACTION_NOT_FOUND")
        transaction, job = row
        require_synced(transaction, job)
        acknowledged_population = job.graph_population_id
        expected_seed = source_node(transaction)
        engine = db.get_bind()
        db.rollback()  # End the read, never hold SQL locks across a graph request.
        population = self.population_factory(engine)
        if not population.verified or not population.identifier:
            unavailable("GRAPH_POPULATION_UNVERIFIED")
        if acknowledged_population != population.identifier:
            unavailable("GRAPH_POPULATION_MISMATCH")
        try:
            network = population.graph.get_transaction_network(transaction_id, limit=limit)
        except (NeptuneError, TimeoutError, ConnectionError, OSError):
            unavailable("GRAPH_UNAVAILABLE", retryable=True)
        validate_network(network, transaction_id, limit)
        if network.nodes[0] != expected_seed:
            unavailable("GRAPH_INCONSISTENT")
        transaction_nodes = {node.transaction_id: node for node in network.nodes if isinstance(node, TransactionNode)}
        rows = db.execute(select(Transaction, GraphSyncJob).outerjoin(
            GraphSyncJob, GraphSyncJob.transaction_id == Transaction.transaction_id,
        ).where(Transaction.transaction_id.in_(transaction_nodes))).all()
        if len(rows) != len(transaction_nodes):
            unavailable("GRAPH_INCONSISTENT")
        for transaction, job in rows:
            require_synced(transaction, job, population.identifier)
            if source_node(transaction) != transaction_nodes[transaction.transaction_id]:
                unavailable("GRAPH_INCONSISTENT")
        db.rollback()
        nodes = [ExplorerNode(
            id=display_id(node.ref), type=node.ref.kind,
            label=node.transaction_id if isinstance(node, TransactionNode) else
                  f"{node.ref.kind} {display_id(node.ref).split(':')[1][:8]}",
            properties={"amount": format(node.amount, ".2f"), "currency": node.currency}
                       if isinstance(node, TransactionNode) else {},
        ) for node in network.nodes]
        edges = sorted((ExplorerEdge(source=display_id(edge.source), target=display_id(edge.target), type=edge.kind)
                        for edge in network.relationships), key=lambda edge: (edge.source, edge.type, edge.target))
        return GraphExplorerResponse(transaction_id=transaction_id, nodes=nodes, edges=edges,
                                     node_count=len(nodes), edge_count=len(edges),
                                     truncated=False if len(nodes) < limit else None)
