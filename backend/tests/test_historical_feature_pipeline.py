"""Compare rolling features with the graph reference using small offline inputs."""

import gzip
import hashlib
import json
import random
import shutil
from dataclasses import asdict
from datetime import timedelta, timezone
from decimal import Decimal
from itertools import groupby
from uuid import uuid4

import pytest

from app.graph.in_memory import InMemoryGraphRepository
from app.graph.models import GraphFeatures, TransactionNode
from ml_training.build_graph_features import FEATURE_NAMES, build, main
from ml_training.historical_features import HistoricalFeatureBuilder
from ml_training.prepare_dataset import ATTRIBUTE_FIELDS, DatasetError, PROJECT_ROOT, REFERENCE_TIME


@pytest.fixture
def scratch():
    parent = PROJECT_ROOT / "data/test-graph-features"
    root = parent / uuid4().hex
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        assert root.resolve().parent == parent.resolve()
        shutil.rmtree(root)


def node(identifier=1, seconds=0, card="C1", device="D1", amount="10.00", currency="USD"):
    return TransactionNode(transaction_id=str(identifier), amount=amount, currency=currency,
                           card_token=card, device_token=device,
                           event_time=REFERENCE_TIME + timedelta(seconds=seconds))


def prepared(transaction, **changes):
    return {**transaction.model_dump(mode="json"),
            "TransactionDT": int((transaction.event_time - REFERENCE_TIME).total_seconds()),
            "TransactionAmt": str(transaction.amount), "isFraud": 0,
            "attributes": {field: None for field in ATTRIBUTE_FIELDS}, **changes}


def write_splits(root, splits):
    root.mkdir(parents=True, exist_ok=True)
    for name in ("train", "validation", "test"):
        with gzip.open(root / f"{name}.jsonl.gz", "wt", encoding="utf-8") as handle:
            for item in splits.get(name, []):
                handle.write(json.dumps(item) + "\n")


def read_split(root, name):
    with gzip.open(root / f"{name}.jsonl.gz", "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]


def compare_reference(nodes):
    # Preload exact-time and future events into the reference: its historical
    # cutoff must still agree with the incremental past-only calculation.
    reference = InMemoryGraphRepository()
    for transaction in nodes:
        reference.upsert_transaction(transaction)
    rolling = HistoricalFeatureBuilder()
    for _, group in groupby(nodes, key=lambda transaction: transaction.event_time):
        group = list(group)
        actual = rolling.process_group(group)
        expected = [reference.get_historical_features(n.card_token, n.device_token, n.event_time) for n in group]
        assert [asdict(item) for item in actual] == [asdict(item) for item in expected]
    return rolling


def test_empty_history_and_same_time_exclusion():
    state = HistoricalFeatureBuilder()
    assert state.process_group([]) == []
    assert [asdict(item) for item in state.process_group([node(1), node(2, amount="20.00")])] == [asdict(GraphFeatures())] * 2
    features = state.process_group([node(3, 1)])[0]
    assert features.card_txn_count_24h == 2
    assert features.card_avg_amount_7d == Decimal("15.00")


@pytest.mark.parametrize("age, count24, count7", [
    (604801, 0, 0), (604800, 0, 1), (86401, 0, 1),
    (86400, 1, 1), (1, 1, 1), (0, 0, 0),
])
def test_precise_inclusive_lower_exclusive_upper_boundaries(age, count24, count7):
    state = HistoricalFeatureBuilder()
    current = node(2, 604801)
    prior = node(1, 604801 - age)
    if age:
        state.process_group([prior])
        features = state.process_group([current])[0]
    else:
        features = state.process_group([prior, current])[1]
    assert asdict(features) == asdict(GraphFeatures(
        card_txn_count_24h=count24, device_txn_count_24h=count24,
        card_unique_devices_7d=count7, device_unique_cards_7d=count7,
        card_avg_amount_7d=Decimal("10.00") if count7 else Decimal("0")))


def test_randomized_reference_equivalence_with_future_events():
    rng = random.Random(42)
    nodes = [node(i, rng.randrange(0, 20) * 86400, f"C{rng.randrange(6)}",
                  rng.choice([None, "D1", "D2", "D3"]), f"{rng.randrange(1, 10000) / 100:.2f}")
             for i in range(1, 301)]
    nodes.sort(key=lambda n: (n.event_time, int(n.transaction_id)))
    compare_reference(nodes)


def test_repeated_pairs_and_partial_expiration():
    state = compare_reference([node(1, 0), node(2, 1), node(3, 2, card="C2"),
                               node(4, 604801), node(5, 604802), node(6, 604803, device=None)])
    assert state.devices["D1"]["C1"] == 2  # New queries are added only after calculation.
    assert "C2" not in state.cards


def test_expiration_frees_inactive_maps_and_queues():
    state = HistoricalFeatureBuilder()
    state.process_group([node()])
    result = state.process_group([node(2, 604801, card="new", device=None)])[0]
    assert asdict(result) == asdict(GraphFeatures())
    assert set(state.cards) == {"new"}
    assert not state.devices and not state.device_counts_24h
    assert set(state.card_counts_24h) == {"new"}
    assert len(state.window_24h) == len(state.window_7d) == 1


def test_subcent_average_and_missing_devices():
    state = HistoricalFeatureBuilder()
    state.process_group([node(1, amount="0.01", device=None), node(2, amount="0.02", device=None)])
    features = state.process_group([node(3, 1, device=None)])[0]
    assert features.card_avg_amount_7d == Decimal("0.015")
    assert features.card_unique_devices_7d == features.device_unique_cards_7d == features.device_txn_count_24h == 0


def test_mixed_currency_matches_reference_rule():
    state = HistoricalFeatureBuilder()
    state.process_group([node(1, currency="USD"), node(2, currency="INR")])
    with pytest.raises(ValueError, match="mixed currencies"):
        state.process_group([node(3, 1)])
    # An unrelated card is still valid; expired mixed history is valid again.
    state.process_group([node(4, 2, card="other")])
    assert state.process_group([node(5, 604801)])[0].card_avg_amount_7d == 0


def test_current_currency_does_not_change_prior_currency_average():
    state = HistoricalFeatureBuilder()
    state.process_group([node(1, currency="USD")])
    assert state.process_group([node(2, 1, currency="INR")])[0].card_avg_amount_7d == Decimal("10.00")


def test_microsecond_boundary_precision_matches_reference():
    current = node(4, 604801)
    nodes = [
        node(1).model_copy(update={"event_time": current.event_time - timedelta(days=7, microseconds=1)}),
        node(2).model_copy(update={"event_time": current.event_time - timedelta(days=7)}),
        node(3).model_copy(update={"event_time": current.event_time - timedelta(hours=24, microseconds=1)}),
        current,
    ]
    compare_reference(nodes)


def test_utc_offsets_represent_equal_time_group():
    first = node(1)
    second = node(2).model_copy(update={"event_time": first.event_time.astimezone(timezone(timedelta(hours=5, minutes=30)))})
    state = compare_reference([first, second, node(3, 1)])
    assert state.last_time == REFERENCE_TIME + timedelta(seconds=1)


@pytest.mark.parametrize("invalid", ["same", "earlier", "multiple"])
def test_invalid_group_order_rejected(invalid):
    state = HistoricalFeatureBuilder()
    state.process_group([node(1, 10)])
    group = [node(2, 10 if invalid == "same" else 9)] if invalid != "multiple" else [node(2, 11), node(3, 12)]
    with pytest.raises(ValueError):
        state.process_group(group)


def test_pipeline_preserves_splits_attributes_and_history(scratch):
    splits = {"train": [prepared(node(1))], "validation": [prepared(node(2, 1), isFraud=1)],
              "test": [prepared(node(3, 2, device=None))]}
    source, output = scratch / "source", scratch / "output"
    write_splits(source, splits)
    original = {path.name: path.read_bytes() for path in source.iterdir()}
    report = build(source, output)
    assert report["rows_per_split"] == {"train": 1, "validation": 1, "test": 1}
    assert report["processed_transactions"] == 3
    assert report["missing_device_percentage"] == pytest.approx(100 / 3)
    for split, expected_count in zip(("train", "validation", "test"), (0, 1, 2)):
        enriched = read_split(output, split)[0]
        assert {key: enriched[key] for key in splits[split][0]} == splits[split][0]
        assert enriched["card_txn_count_24h"] == expected_count
        assert enriched["device_missing"] == (split == "test")
        assert set(enriched) - set(splits[split][0]) == {*FEATURE_NAMES, "device_missing"}
        assert report["output_sha256"][split] == hashlib.sha256((output / f"{split}.jsonl.gz").read_bytes()).hexdigest()
    assert original == {path.name: path.read_bytes() for path in source.iterdir()}
    assert all(report["integrity_checks"].values())
    assert report["processing_seconds"] > 0
    assert report["approximate_peak_memory_bytes"] is None or report["approximate_peak_memory_bytes"] > 0
    assert not list(output.glob("*.sqlite3"))


def test_labels_do_not_influence_features_and_outputs_are_reproducible(scratch):
    source = scratch / "source"
    splits = {"train": [prepared(node(1)), prepared(node(2, 1))], "validation": [prepared(node(3, 2))]}
    write_splits(source, splits)
    first, second = scratch / "first", scratch / "second"
    first_report, second_report = build(source, first), build(source, second)
    assert first_report["output_sha256"] == second_report["output_sha256"]
    for split in splits:
        for item in splits[split]:
            item["isFraud"] = 1
    write_splits(source, splits)
    build(source, scratch / "changed-labels")
    for split in ("train", "validation", "test"):
        assert [{key: item[key] for key in FEATURE_NAMES} for item in read_split(first, split)] == [
            {key: item[key] for key in FEATURE_NAMES} for item in read_split(scratch / "changed-labels", split)]


@pytest.mark.parametrize("problem", ["same-time-split", "reverse", "duplicate", "wrong-time", "naive", "label", "raw-field", "amount"])
def test_invalid_inputs_fail_without_publishing(scratch, problem):
    first, second = prepared(node(1)), prepared(node(2, 1))
    if problem == "same-time-split":
        second = prepared(node(2))
    elif problem == "reverse":
        first, second = second, first
    elif problem == "duplicate":
        second["transaction_id"] = "1"
    elif problem == "wrong-time":
        second["TransactionDT"] = 999
    elif problem == "naive":
        second["event_time"] = "2000-01-01T00:00:01"
    elif problem == "label":
        second["isFraud"] = 2
    elif problem == "raw-field":
        second["DeviceInfo"] = "private"
    elif problem == "amount":
        second["amount"] = "99.00"
    write_splits(scratch / "source", {"train": [first], "validation": [second]})
    with pytest.raises(DatasetError):
        build(scratch / "source", scratch / "output")
    assert not (scratch / "output").exists()
    assert not list(scratch.glob(".graph-features-*"))


def test_empty_partitions_and_missing_data(scratch, capsys):
    assert main(["--input-dir", str(scratch / "missing"), "--output-dir", str(scratch / "out")]) == 1
    assert "Phase 3.1" in capsys.readouterr().err
    write_splits(scratch / "source", {})
    with pytest.raises(DatasetError, match="no transactions"):
        build(scratch / "source", scratch / "out")
    write_splits(scratch / "source", {"test": [prepared(node())]})
    assert build(scratch / "source", scratch / "out")["rows_per_split"] == {"train": 0, "validation": 0, "test": 1}


def test_existing_artifacts_are_never_overwritten(scratch):
    source, output = scratch / "source", scratch / "output"
    write_splits(source, {"train": [prepared(node())]})
    output.mkdir()
    sentinel = output / "unrelated.txt"
    sentinel.write_text("keep", encoding="utf-8")
    with pytest.raises(DatasetError, match="already exists"):
        build(source, output)
    assert sentinel.read_text() == "keep"
    with pytest.raises(DatasetError, match="overlaps inputs"):
        build(source, source)
