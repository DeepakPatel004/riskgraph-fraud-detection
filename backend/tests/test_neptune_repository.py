from datetime import timedelta
from decimal import Decimal

import httpx
import pytest

from app.graph.in_memory import InMemoryGraphRepository
from app.graph.models import NodeRef
from app.graph.neptune import (
    CARD_HISTORY, DEVICE_HISTORY, DEVICE_UPSERT, UPSERT,
    NeptuneGraphRepository, edge_id, node_id, utc_text,
)
from app.graph.neptune_client import NeptuneClient, NeptuneResponseError
from tests.fake_neptune import FakeNeptune, fake_repository
from tests.test_graph_repository import CUTOFF, transaction


def test_write_query_generation_and_parameterization():
    """Assert generated text/values; this does not execute the query in Neptune."""
    server = FakeNeptune()
    with fake_repository(server) as adapter:
        node = transaction("ID'&", "CARD'&", "DEVICE'&", amount="1234567890123456.78")
        adapter.upsert_transaction(node)
        query, p = server.calls[-1]
        assert query == UPSERT + DEVICE_UPSERT
        assert "DELETE old" in query
        assert "WITH DISTINCT t" in query
        assert "MERGE (t)-[:USES_CARD {`~id`: $card_edge_id}]" in query
        assert "MERGE (t)-[:USES_DEVICE {`~id`: $device_edge_id}]" in query
        assert p["amount"] == "1234567890123456.78"
        assert p["event_time"] == utc_text(node.event_time)
        assert p["card_edge_id"] == edge_id(node.ref, NodeRef("Card", node.card_token), "USES_CARD")
        for value in [node.transaction_id, node.card_token, node.device_token, p["amount"], p["event_time"]]:
            assert value not in query


def test_repeated_and_changed_writes_with_fake_state():
    server = FakeNeptune()
    with fake_repository(server) as adapter:
        adapter.upsert_transaction(transaction())
        before = dict(server.edges)
        adapter.upsert_transaction(transaction())
        assert server.edges == before
        assert len(server.nodes) == 3
        adapter.upsert_transaction(transaction(card="NEW", device="NEW"))
        assert len(server.edges) == 2
        assert not set(before).intersection(server.edges)
        assert len(adapter.get_transaction_network("T1").nodes) == 3
        adapter.upsert_transaction(transaction(card="NEW", device=None))
        assert len(server.edges) == 1
        query, p = next((q, p) for q, p in reversed(server.calls) if q.startswith("// riskgraph:upsert"))
        assert query == UPSERT
        assert "MERGE (d:Device" not in query
        assert p["device_node_id"] is p["device_edge_id"] is None
        assert len(adapter.get_transaction_network("T1").nodes) == 2


def test_deterministic_ids_are_namespaced():
    refs = [NodeRef(kind, "same-token") for kind in ["Transaction", "Card", "Device"]]
    assert len({node_id(ref) for ref in refs}) == 3
    assert node_id(refs[0]) == node_id(refs[0])
    assert "same-token" not in node_id(refs[0])


def test_paged_histories_and_traversal_match_memory():
    server, memory = FakeNeptune(), InMemoryGraphRepository()
    with fake_repository(server) as adapter:
        for number in reversed(range(240)):
            node = transaction(f"T{number:03}", "SHARED", f"D{number:03}",
                               amount="0.01" if number % 2 else "0.02")
            adapter.upsert_transaction(node)
            memory.upsert_transaction(node)
        server.calls.clear()
        features = adapter.get_historical_features("SHARED", "D000", CUTOFF)
        assert features == memory.get_historical_features("SHARED", "D000", CUTOFF)
        assert features.card_avg_amount_7d == Decimal("0.015")
        assert len([q for q, _ in server.calls if q == CARD_HISTORY]) == 3
        assert len([q for q, _ in server.calls if q == DEVICE_HISTORY]) == 1
        server.calls.clear()
        assert adapter.get_transaction_network("T000", limit=151) == memory.get_transaction_network("T000", limit=151)
        assert len([q for q, _ in server.calls if q.startswith("// riskgraph:edges")]) == 2
        for query, _ in server.calls:
            assert "LIMIT 100" in query or "LIMIT 1" in query
            assert "*" not in query  # No variable-length traversal.


def test_historical_query_bounds_and_no_device_query():
    server = FakeNeptune()
    with fake_repository(server) as adapter:
        adapter.get_historical_features("MISSING", None, CUTOFF)
    assert len(server.calls) == 1
    query, p = server.calls[0]
    assert query == CARD_HISTORY
    assert "t.event_time >= $lower AND t.event_time < $cutoff" in query
    assert p["lower"] == utc_text(CUTOFF - timedelta(days=7))
    assert p["cutoff"] == utc_text(CUTOFF)
    assert "MISSING" not in query


@pytest.mark.parametrize("raw", [None, {}, {"~properties": {}}, {"~labels": []}])
def test_malformed_node_responses_are_safe(raw):
    with httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"results": [{"node": raw}]}),
    )) as http:
        adapter = NeptuneGraphRepository(NeptuneClient("neptune.invalid", iam_auth_enabled=False, http_client=http))
        with pytest.raises(NeptuneResponseError, match="invalid graph node"):
            adapter.get_transaction_network("T1")


def test_bad_history_pagination_is_rejected():
    server = FakeNeptune()
    original = server.execute
    def bad_order(operation, params):
        rows = original(operation, params)
        return list(reversed(rows)) if operation == "card_history" else rows
    server.execute = bad_order
    with fake_repository(server) as adapter:
        adapter.upsert_transaction(transaction("A"))
        adapter.upsert_transaction(transaction("B"))
        with pytest.raises(NeptuneResponseError, match="pagination"):
            adapter.get_historical_features("CARD1", None, CUTOFF)


def test_out_of_window_history_is_rejected():
    server = FakeNeptune()
    original = server.execute
    def bad_window(operation, params):
        if operation == "card_history":
            return [{"node": next(iter(server.nodes.values()))}]
        return original(operation, params)
    server.execute = bad_window
    with fake_repository(server) as adapter:
        adapter.upsert_transaction(transaction(event_time=CUTOFF))
        with pytest.raises(NeptuneResponseError, match="outside the historical query"):
            adapter.get_historical_features("CARD1", None, CUTOFF)


def test_oversized_page_is_rejected():
    server = FakeNeptune()
    original = server.execute
    def oversized(operation, params):
        rows = original(operation, params)
        return rows * 101 if operation == "card_history" else rows
    server.execute = oversized
    with fake_repository(server) as adapter:
        adapter.upsert_transaction(transaction())
        with pytest.raises(NeptuneResponseError, match="page size"):
            adapter.get_historical_features("CARD1", None, CUTOFF)


def test_network_rejects_edges_outside_selected_nodes():
    server = FakeNeptune()
    original = server.execute
    def bad_edge(operation, params):
        rows = original(operation, params)
        if operation == "edges" and rows:
            rows[0]["target_key"] = "NOT_SELECTED"
        return rows
    server.execute = bad_edge
    with fake_repository(server) as adapter:
        adapter.upsert_transaction(transaction())
        with pytest.raises(NeptuneResponseError, match="invalid graph relationship"):
            adapter.get_transaction_network("T1")
