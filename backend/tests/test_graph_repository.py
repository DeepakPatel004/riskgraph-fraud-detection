from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.graph.in_memory import InMemoryGraphRepository
from app.graph.models import (
    CardNode, DeviceNode, GraphFeatures, GraphNetwork, GraphRelationship,
    NodeRef, TransactionNode,
)
from app.graph.repository import GraphRepository
from tests.fake_neptune import fake_repository


CUTOFF = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)


def transaction(identifier="T1", card="CARD1", device="DEVICE1", *,
                event_time=CUTOFF - timedelta(hours=1), amount="10.00", currency="INR"):
    return TransactionNode(
        transaction_id=identifier, card_token=card, device_token=device,
        event_time=event_time, amount=amount, currency=currency,
    )


@pytest.fixture(params=["memory", "fake-neptune"])
def repository(request) -> GraphRepository:
    if request.param == "memory":
        yield InMemoryGraphRepository()
    else:
        with fake_repository() as adapter:
            yield adapter


def test_insertion_and_idempotent_upsert(repository):
    node = transaction()
    repository.upsert_transaction(node)
    expected = GraphNetwork(
        (node, CardNode("CARD1"), DeviceNode("DEVICE1")),
        (GraphRelationship(node.ref, NodeRef("Card", "CARD1"), "USES_CARD"),
         GraphRelationship(node.ref, NodeRef("Device", "DEVICE1"), "USES_DEVICE")),
    )
    assert repository.get_transaction_network("T1") == expected
    repository.upsert_transaction(node)
    repository.upsert_transaction(transaction())
    assert repository.get_transaction_network("T1") == expected
    assert repository.get_historical_features("CARD1", "DEVICE1", CUTOFF).card_txn_count_24h == 1


@pytest.mark.parametrize("shared", ["card", "device"])
def test_shared_tokens_connect_transactions(repository, shared):
    repository.upsert_transaction(transaction())
    repository.upsert_transaction(transaction(
        "T2", card="CARD1" if shared == "card" else "CARD2",
        device="DEVICE1" if shared == "device" else "DEVICE2",
    ))
    repository.upsert_transaction(transaction("UNRELATED", "OTHER", "OTHER"))
    network = repository.get_transaction_network("T1")
    assert len(network.nodes) == 5
    assert len(network.relationships) == 4
    assert {node.transaction_id for node in network.nodes if isinstance(node, TransactionNode)} == {"T1", "T2"}


def test_missing_device_has_only_card_edge(repository):
    repository.upsert_transaction(transaction(device=None))
    network = repository.get_transaction_network("T1")
    assert len(network.nodes) == 2
    assert len(network.relationships) == 1
    assert network.relationships[0].kind == "USES_CARD"
    assert not any(isinstance(node, DeviceNode) for node in network.nodes)
    assert repository.get_historical_features("CARD1", None, CUTOFF) == GraphFeatures(
        card_txn_count_24h=1, card_avg_amount_7d=Decimal("10"),
    )


def test_node_ids_are_namespaced(repository):
    repository.upsert_transaction(transaction("SAME", "SAME", "SAME"))
    network = repository.get_transaction_network("SAME")
    assert len({node.ref for node in network.nodes}) == 3
    assert len(network.relationships) == 2


def test_upsert_replaces_properties_and_old_edges(repository):
    repository.upsert_transaction(transaction())
    repository.upsert_transaction(transaction("T2", "CARD1", "DEVICE1"))
    replacement = transaction(card="NEW_CARD", device=None, amount="25.50")
    repository.upsert_transaction(replacement)
    assert repository.get_transaction_network("T1").nodes == (replacement, CardNode("NEW_CARD"))
    assert len(repository.get_transaction_network("T2").nodes) == 3
    assert repository.get_historical_features("CARD1", "DEVICE1", CUTOFF).card_txn_count_24h == 1
    assert repository.get_historical_features("NEW_CARD", None, CUTOFF).card_avg_amount_7d == Decimal("25.50")


def test_features_use_distinct_tokens_and_decimal_amounts(repository):
    for node in [
        transaction("LOWER7", "C1", "D1", event_time=CUTOFF - timedelta(days=7), amount="10"),
        transaction("OUTSIDE", "C1", "EXCLUDED", event_time=CUTOFF - timedelta(days=7, microseconds=1), amount="1000"),
        transaction("LOWER24", "C1", "D2", event_time=CUTOFF - timedelta(hours=24), amount="20"),
        transaction("BEFORE24", "C1", "D3", event_time=CUTOFF - timedelta(hours=24, microseconds=1), amount="30"),
        transaction("RECENT", "C1", "D2", amount="40"),
        transaction("OTHER_CARD", "C2", "D2", amount="50"),
        transaction("OTHER_CARD7", "C3", "D2", event_time=CUTOFF - timedelta(days=7), amount="70"),
        transaction("NO_DEVICE", "C1", None, amount="60"),
        transaction("EXACT", "C1", "EXCLUDED", event_time=CUTOFF, amount="1000"),
        transaction("FUTURE", "C1", "EXCLUDED", event_time=CUTOFF + timedelta(seconds=1), amount="1000"),
    ]:
        repository.upsert_transaction(node)
    assert repository.get_historical_features("C1", "D2", CUTOFF) == GraphFeatures(
        card_txn_count_24h=3, device_txn_count_24h=3,
        card_unique_devices_7d=3, device_unique_cards_7d=3,
        card_avg_amount_7d=Decimal("32"),
    )


@pytest.mark.parametrize("age, count24, count7", [
    (timedelta(days=7, microseconds=1), 0, 0),
    (timedelta(days=7), 0, 1),
    (timedelta(hours=24, microseconds=1), 0, 1),
    (timedelta(hours=24), 1, 1),
    (timedelta(microseconds=1), 1, 1),
    (timedelta(0), 0, 0),
    (timedelta(microseconds=-1), 0, 0),
])
def test_exact_window_boundaries(repository, age, count24, count7):
    repository.upsert_transaction(transaction(event_time=CUTOFF - age, amount="0.01"))
    assert repository.get_historical_features("CARD1", "DEVICE1", CUTOFF) == GraphFeatures(
        card_txn_count_24h=count24, device_txn_count_24h=count24,
        card_unique_devices_7d=count7, device_unique_cards_7d=count7,
        card_avg_amount_7d=Decimal("0.01") if count7 else Decimal("0"),
    )


def test_utc_offsets_and_query_cutoff(repository):
    event = datetime.fromisoformat("2026-10-10T10:30:00+05:30")
    node = transaction(event_time=event)
    assert node.event_time == datetime(2026, 10, 10, 5, tzinfo=timezone.utc)
    assert node.event_time.tzinfo is timezone.utc
    repository.upsert_transaction(node)
    offset_cutoff = datetime.fromisoformat("2026-10-10T01:01:00-04:00")
    utc_cutoff = datetime.fromisoformat("2026-10-10T05:01:00Z")
    assert repository.get_historical_features("CARD1", "DEVICE1", offset_cutoff) == repository.get_historical_features("CARD1", "DEVICE1", utc_cutoff)
    assert repository.get_historical_features("CARD1", "DEVICE1", utc_cutoff).card_txn_count_24h == 1
    assert repository.get_historical_features("CARD1", "DEVICE1", event) == GraphFeatures()


def test_naive_times_rejected(repository):
    with pytest.raises(ValidationError, match="must include a timezone"):
        transaction(event_time=CUTOFF.replace(tzinfo=None))
    with pytest.raises(ValueError, match="must include a timezone"):
        repository.get_historical_features("CARD1", None, CUTOFF.replace(tzinfo=None))


def test_empty_and_unknown_graph_data(repository):
    assert repository.get_transaction_network("MISSING") == GraphNetwork()
    assert repository.get_historical_features("MISSING", "MISSING", CUTOFF) == GraphFeatures()
    repository.upsert_transaction(transaction())
    assert repository.get_transaction_network("MISSING") == GraphNetwork()
    assert repository.get_historical_features("MISSING", "MISSING", CUTOFF) == GraphFeatures()


@pytest.mark.parametrize("limit", [1, 2, 3, 5, 50])
def test_bounded_connected_network(repository, limit):
    for number in range(60):
        repository.upsert_transaction(transaction(f"T{number:02}", "SHARED", f"D{number:02}"))
    network = repository.get_transaction_network("T00", limit=limit)
    assert len(network.nodes) == limit
    assert network.nodes[0].ref == NodeRef("Transaction", "T00")
    refs = {node.ref for node in network.nodes}
    assert all(edge.source in refs and edge.target in refs for edge in network.relationships)
    # Every node after the root has an edge to the selected component.
    assert len(network.relationships) >= len(network.nodes) - 1
    assert repository.get_transaction_network("T00", limit=limit) == network


def test_default_limit_and_insertion_independent_traversal():
    first, second = InMemoryGraphRepository(), InMemoryGraphRepository()
    nodes = [transaction(f"T{number:02}", "SHARED", "SHARED") for number in range(60)]
    for node in nodes:
        first.upsert_transaction(node)
    for node in reversed(nodes):
        second.upsert_transaction(node)
    network = first.get_transaction_network("T00")
    assert len(network.nodes) == 50
    assert network == second.get_transaction_network("T00")


@pytest.mark.parametrize("limit", [0, -1, 1.5, True])
def test_invalid_network_limit(repository, limit):
    with pytest.raises(ValueError, match="positive integer"):
        repository.get_transaction_network("MISSING", limit=limit)


def test_average_preserves_subcent_precision(repository):
    repository.upsert_transaction(transaction("T1", amount="0.01"))
    repository.upsert_transaction(transaction("T2", amount="0.02"))
    average = repository.get_historical_features("CARD1", None, CUTOFF).card_avg_amount_7d
    assert isinstance(average, Decimal)
    assert average == Decimal("0.015")


def test_mixed_currency_average_is_rejected(repository):
    repository.upsert_transaction(transaction("T1", currency="INR"))
    repository.upsert_transaction(transaction("T2", currency="USD"))
    with pytest.raises(ValueError, match="mixed currencies"):
        repository.get_historical_features("CARD1", None, CUTOFF)


def test_graph_input_excludes_risk_labels_and_is_immutable():
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        TransactionNode(**transaction().model_dump(), risk_score=0.99)
    with pytest.raises(ValidationError, match="frozen"):
        transaction().card_token = "CHANGED"


def test_repository_instances_are_isolated():
    first, second = InMemoryGraphRepository(), InMemoryGraphRepository()
    first.upsert_transaction(transaction())
    assert second.get_transaction_network("T1") == GraphNetwork()
