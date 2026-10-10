"""Offline-tested Neptune Database adapter. Requires custom IDs (engine >=1.2.0.2)."""

from collections import deque
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import json

from pydantic import ValidationError

from app.graph.models import (
    CardNode, DeviceNode, GraphFeatures, GraphNetwork, GraphNode,
    GraphRelationship, NodeRef, TransactionNode, as_utc,
)
from app.graph.neptune_client import NeptuneClient, NeptuneResponseError


PAGE_SIZE = 100


def node_id(ref: NodeRef) -> str:
    """Namespaced, deterministic Neptune custom ID; never interpolate tokens."""
    return f"{ref.kind}:" + hashlib.sha256(ref.identifier.encode("utf-8")).hexdigest()


def edge_id(source: NodeRef, target: NodeRef, kind: str) -> str:
    encoded = json.dumps([node_id(source), kind, node_id(target)]).encode("utf-8")
    return "Relationship:" + hashlib.sha256(encoded).hexdigest()


def utc_text(value: datetime) -> str:
    """Fixed-width UTC strings sort chronologically and retain microseconds."""
    return as_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


UPSERT = """// riskgraph:upsert
MERGE (t:Transaction {`~id`: $transaction_node_id})
SET t.node_kind = 'Transaction', t.node_key = $transaction_id,
    t.transaction_id = $transaction_id, t.card_token = $card_token,
    t.device_token = $device_token, t.amount = $amount,
    t.currency = $currency, t.event_time = $event_time
WITH t
OPTIONAL MATCH (t)-[old:USES_CARD|USES_DEVICE]->()
WHERE (type(old) = 'USES_CARD' AND id(old) <> $card_edge_id)
   OR (type(old) = 'USES_DEVICE' AND ($device_edge_id IS NULL OR id(old) <> $device_edge_id))
DELETE old
WITH DISTINCT t
MERGE (c:Card {`~id`: $card_node_id})
SET c.node_kind = 'Card', c.node_key = $card_token, c.card_token = $card_token
MERGE (t)-[:USES_CARD {`~id`: $card_edge_id}]->(c)
"""

DEVICE_UPSERT = """
WITH t
MERGE (d:Device {`~id`: $device_node_id})
SET d.node_kind = 'Device', d.node_key = $device_token, d.device_token = $device_token
MERGE (t)-[:USES_DEVICE {`~id`: $device_edge_id}]->(d)
"""

SEED = """// riskgraph:seed
MATCH (t:Transaction {`~id`: $node_id}) RETURN t AS node LIMIT 1
"""

NEIGHBORS = """// riskgraph:neighbors
MATCH (s {`~id`: $node_id})-[:USES_CARD|USES_DEVICE]-(n)
WHERE NOT (id(n) IN $excluded_ids)
  AND (n.node_kind > $after_kind OR (n.node_kind = $after_kind AND n.node_key > $after_key))
RETURN DISTINCT n AS node ORDER BY n.node_kind, n.node_key LIMIT 100
"""

EDGES = """// riskgraph:edges
UNWIND $selected_ids AS source_id
MATCH (t:Transaction {`~id`: source_id})-[r:USES_CARD|USES_DEVICE]->(n)
WHERE id(n) IN $selected_ids AND id(r) > $after_id
RETURN id(r) AS edge_id, t.transaction_id AS transaction_id,
       type(r) AS relationship_kind, n.node_kind AS target_kind, n.node_key AS target_key
ORDER BY edge_id LIMIT 100
"""

# Only trusted labels/relationship types vary between these two static queries.
CARD_HISTORY = """// riskgraph:card_history
MATCH (t:Transaction)-[:USES_CARD]->(c:Card {`~id`: $token_node_id})
WHERE t.event_time >= $lower AND t.event_time < $cutoff AND t.transaction_id > $after_id
RETURN DISTINCT t AS node ORDER BY t.transaction_id LIMIT 100
"""
DEVICE_HISTORY = """// riskgraph:device_history
MATCH (t:Transaction)-[:USES_DEVICE]->(d:Device {`~id`: $token_node_id})
WHERE t.event_time >= $lower AND t.event_time < $cutoff AND t.transaction_id > $after_id
RETURN DISTINCT t AS node ORDER BY t.transaction_id LIMIT 100
"""


def read_node(raw: dict) -> GraphNode:
    """Decode Neptune's documented node JSON, selecting only domain fields."""
    try:
        properties = raw["~properties"]
        kind = properties["node_kind"]
        if kind not in raw["~labels"] or raw["~entityType"] != "node":
            raise ValueError
        if kind == "Transaction":
            node = TransactionNode(**{key: properties[key] for key in (
                "transaction_id", "amount", "currency", "card_token", "event_time",
            )}, device_token=properties.get("device_token"))
        elif kind == "Card":
            node = CardNode(properties["card_token"])
        elif kind == "Device":
            node = DeviceNode(properties["device_token"])
        else:
            raise ValueError
        if (not isinstance(node.ref.identifier, str) or not node.ref.identifier
                or properties["node_key"] != node.ref.identifier or raw["~id"] != node_id(node.ref)):
            raise ValueError
        return node
    except (KeyError, TypeError, ValueError, ValidationError):
        raise NeptuneResponseError("Neptune returned an invalid graph node") from None


class NeptuneGraphRepository:
    """Same public graph behavior, with single-query writes and paged reads.

    Custom IDs prevent intentional duplicates, but MERGE is not concurrency
    control. Serialize writes per transaction ID; competing writes may fail and
    need reconciliation. Do not blindly retry unknown write outcomes. Multi-query
    reads require stable data for a coherent view (no cross-request snapshot).
    """

    def __init__(self, client: NeptuneClient):
        self.client = client

    def upsert_transaction(self, transaction: TransactionNode) -> None:
        card_ref = NodeRef("Card", transaction.card_token)
        device_ref = (NodeRef("Device", transaction.device_token)
                      if transaction.device_token is not None else None)
        parameters = transaction.model_dump(mode="json")
        parameters.update(
            event_time=utc_text(transaction.event_time),
            transaction_node_id=node_id(transaction.ref), card_node_id=node_id(card_ref),
            card_edge_id=edge_id(transaction.ref, card_ref, "USES_CARD"),
            device_node_id=node_id(device_ref) if device_ref else None,
            device_edge_id=edge_id(transaction.ref, device_ref, "USES_DEVICE") if device_ref else None,
        )
        self.client.execute(UPSERT + (DEVICE_UPSERT if device_ref else ""), parameters)
        # Unused old token nodes may remain, but are unreachable from transactions.

    def _node_pages(self, query: str, parameters: dict, *, neighbor_order: bool = False):
        after = ("", "") if neighbor_order else ""
        while True:
            cursor = {"after_kind": after[0], "after_key": after[1]} if neighbor_order else {"after_id": after}
            rows = self.client.execute(query, parameters | cursor)
            if len(rows) > PAGE_SIZE:
                raise NeptuneResponseError("Neptune exceeded the requested page size")
            for row in rows:
                node = read_node(row.get("node"))
                if not neighbor_order and not isinstance(node, TransactionNode):
                    raise NeptuneResponseError("Neptune history contained a non-transaction node")
                key = (node.ref.kind, node.ref.identifier) if neighbor_order else node.transaction_id
                if key <= after:
                    raise NeptuneResponseError("Neptune returned an invalid pagination order")
                after = key
                yield node
            if len(rows) < PAGE_SIZE:
                return

    def get_historical_features(self, card_token: str, device_token: str | None,
                                event_time: datetime) -> GraphFeatures:
        cutoff = as_utc(event_time)
        lower = cutoff - timedelta(days=7)
        lower24 = cutoff - timedelta(hours=24)
        bounds = {"lower": utc_text(lower), "cutoff": utc_text(cutoff)}
        count24, card_count, total = 0, 0, Decimal("0")
        devices, currencies = set(), set()
        for node in self._node_pages(CARD_HISTORY, bounds | {"token_node_id": node_id(NodeRef("Card", card_token))}):
            if node.card_token != card_token or not lower <= node.event_time < cutoff:
                raise NeptuneResponseError("Neptune returned data outside the historical query")
            count24 += node.event_time >= lower24
            card_count += 1
            total += node.amount
            currencies.add(node.currency)
            if node.device_token is not None:
                devices.add(node.device_token)
        if len(currencies) > 1:
            raise ValueError("Cannot average mixed currencies in card history")
        device_count24, cards = 0, set()
        if device_token is not None:
            for node in self._node_pages(DEVICE_HISTORY, bounds | {"token_node_id": node_id(NodeRef("Device", device_token))}):
                if node.device_token != device_token or not lower <= node.event_time < cutoff:
                    raise NeptuneResponseError("Neptune returned data outside the historical query")
                device_count24 += node.event_time >= lower24
                cards.add(node.card_token)
        return GraphFeatures(count24, device_count24, len(devices), len(cards),
                             total / card_count if card_count else Decimal("0"))

    def get_transaction_network(self, transaction_id: str, limit: int = 50) -> GraphNetwork:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        seed_ref = NodeRef("Transaction", transaction_id)
        rows = self.client.execute(SEED, {"node_id": node_id(seed_ref)})
        if not rows:
            return GraphNetwork()
        seed = read_node(rows[0].get("node"))
        if len(rows) != 1 or seed.ref != seed_ref:
            raise NeptuneResponseError("Neptune returned an invalid network root")
        selected = {seed.ref: seed}
        queue = deque([seed.ref])
        while queue and len(selected) < limit:
            current = queue.popleft()
            parameters = {"node_id": node_id(current),
                          "excluded_ids": [node_id(ref) for ref in selected]}
            for node in self._node_pages(NEIGHBORS, parameters, neighbor_order=True):
                if node.ref in selected:
                    raise NeptuneResponseError("Neptune returned an excluded network node")
                selected[node.ref] = node
                queue.append(node.ref)
                if len(selected) == limit:
                    break
        edges = set()
        after = ""
        while True:
            rows = self.client.execute(EDGES, {"selected_ids": [node_id(ref) for ref in selected], "after_id": after})
            if len(rows) > PAGE_SIZE:
                raise NeptuneResponseError("Neptune exceeded the requested page size")
            for row in rows:
                try:
                    source = NodeRef("Transaction", row["transaction_id"])
                    target = NodeRef(row["target_kind"], row["target_key"])
                    kind = row["relationship_kind"]
                    if (source not in selected or target not in selected
                            or (target.kind, kind) not in {("Card", "USES_CARD"), ("Device", "USES_DEVICE")}
                            or row["edge_id"] != edge_id(source, target, kind) or row["edge_id"] <= after):
                        raise ValueError
                    after = row["edge_id"]
                    edges.add(GraphRelationship(source, target, kind))
                except (KeyError, TypeError, ValueError):
                    raise NeptuneResponseError("Neptune returned an invalid graph relationship") from None
            if len(rows) < PAGE_SIZE:
                break
        return GraphNetwork(tuple(selected.values()), tuple(sorted(
            edges, key=lambda edge: (edge.source.identifier, edge.kind, edge.target.identifier),
        )))
