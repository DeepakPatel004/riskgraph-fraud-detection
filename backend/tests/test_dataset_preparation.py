"""Tiny synthetic CSVs only: no Kaggle, application DB, credentials or network."""

import csv
import gzip
import json
import shutil
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.graph.models import TransactionNode
from ml_training.prepare_dataset import (
    ATTRIBUTE_FIELDS, CARD_FIELDS, DEVICE_FIELDS, PROJECT_ROOT, REFERENCE_TIME,
    DatasetError, fingerprint, main, map_transaction, prepare,
)


@pytest.fixture
def scratch():
    # Normal mkdir avoids restrictive Windows permissions from pytest tmp_path.
    parent = PROJECT_ROOT / "data" / "test-preparation"
    root = parent / uuid4().hex
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        assert root.resolve().parent == parent.resolve()
        shutil.rmtree(root)


def row(identifier=1, seconds=0, **changes):
    return {"TransactionID": str(identifier), "TransactionDT": str(seconds),
            "TransactionAmt": "12.345", "isFraud": str(identifier % 2),
            "card1": "100", "card4": "visa", "ProductCD": "W", **changes}


def write_csv(path, records, headers):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        writer.writerows(records)


def run(root, transactions=None, identities=None):
    transactions = [row(i, i) for i in range(1, 21)] if transactions is None else transactions
    identities = [{"TransactionID": "1", "DeviceInfo": "synthetic-phone"}] if identities is None else identities
    headers = list(dict.fromkeys(field for item in transactions for field in item))
    write_csv(root / "raw/train_transaction.csv", transactions,
              headers or list(row()))
    identity_headers = list(dict.fromkeys(field for item in identities for field in item))
    write_csv(root / "raw/train_identity.csv", identities, identity_headers or ["TransactionID"])
    return prepare(root / "raw", root / "processed", root / "reports")


def read(root, name):
    with gzip.open(root / "processed" / f"{name}.jsonl.gz", "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]


def all_rows(root):
    return [item for name in ("train", "validation", "test") for item in read(root, name)]


def test_left_join_preserves_transactions_and_profiles_missingness(scratch):
    report = run(scratch, identities=[{"TransactionID": "1", "DeviceInfo": "synthetic-phone"},
                                      {"TransactionID": "99", "DeviceInfo": "unmatched"}])
    items = all_rows(scratch)
    assert len(items) == report["transaction_rows"] == 20
    assert report["matched_identity_rows"] == 1
    assert report["missing_identity_rows"] == 19
    assert report["unmatched_identity_rows"] == 1
    assert items[0]["device_token"] is not None
    assert items[0]["device_token"] == fingerprint(
        "device", DEVICE_FIELDS, {"DeviceInfo": "synthetic-phone"}, "1")
    assert all(item["device_token"] is None for item in items[1:])
    assert report["class_counts"] == {"0": 10, "1": 10}
    assert report["rounded_amount_rows"] == 20
    assert report["prepared_types"]["event_time"] == "synthetic UTC ISO-8601"
    assert report["warnings"]  # Small fixtures cannot support meaningful metrics.


@pytest.mark.parametrize("table", ["transaction", "identity"])
def test_duplicate_keys_rejected_before_outputs(scratch, table):
    transactions = [row(1), row(1)] if table == "transaction" else [row(1)]
    identities = [{"TransactionID": "1"}] * (2 if table == "identity" else 1)
    with pytest.raises(DatasetError, match="duplicate TransactionID"):
        run(scratch, transactions, identities)
    assert not list((scratch / "processed").glob("*.jsonl.gz"))
    assert not list((scratch / "processed").glob("*.sqlite3"))


@pytest.mark.parametrize("value", ["", "2", "-1", "True", "0.5", "NaN"])
def test_invalid_labels_rejected(value):
    with pytest.raises(DatasetError, match="isFraud"):
        map_transaction(row(isFraud=value), {})


@pytest.mark.parametrize("value", ["", "abc", "-1", "0", "NaN", "Infinity", "0.001", "10000000000000000"])
def test_invalid_amounts_rejected(value):
    with pytest.raises(DatasetError, match="TransactionAmt"):
        map_transaction(row(TransactionAmt=value), {})


def test_amount_precision_and_graph_compatibility():
    item = map_transaction(row(), {})
    assert item["TransactionAmt"] == "12.345"
    assert item["amount"] == "12.34"  # Explicit decimal half-even rounding.
    assert Decimal(item["amount"]) == Decimal("12.34")
    node = TransactionNode(**{key: item[key] for key in (
        "transaction_id", "amount", "currency", "card_token", "device_token", "event_time")})
    assert node.event_time == REFERENCE_TIME


def test_deterministic_tokens_no_label_or_transaction_leakage():
    identity = {"DeviceInfo": "synthetic-phone", "DeviceType": "mobile"}
    a = map_transaction(row(1, isFraud="0"), identity)
    b = map_transaction(row(2, isFraud="1"), identity)
    assert a["card_token"] == b["card_token"]
    assert a["device_token"] == b["device_token"]
    assert a["card_token"] != fingerprint("card", CARD_FIELDS, {"card1": "other"}, "1")
    assert a["device_token"] != fingerprint("device", DEVICE_FIELDS, {"DeviceInfo": "other"}, "1")
    assert a["card_token"] != a["device_token"]
    assert not any(field in a for field in (*CARD_FIELDS, *DEVICE_FIELDS))
    assert a["attributes"] == {field: "W" if field == "ProductCD" else None for field in ATTRIBUTE_FIELDS}


def test_missing_identifiers_do_not_create_shared_fake_nodes():
    a = map_transaction({**row(1), "card1": "NaN", "card4": ""}, {})
    b = map_transaction({**row(2), "card1": "", "card4": "null"}, {})
    assert a["card_token"] != b["card_token"]
    assert a["device_token"] is b["device_token"] is None
    assert a["card_token"] == map_transaction({**row(1), "card1": "", "card4": ""}, {})["card_token"]


def test_temporal_order_utc_and_exact_splits(scratch):
    report = run(scratch, [row(i, i) for i in reversed(range(1, 21))])
    assert [item["transaction_id"] for item in all_rows(scratch)] == list(map(str, range(1, 21)))
    assert [report["partitions"][name]["rows"] for name in ("train", "validation", "test")] == [14, 3, 3]
    for item in all_rows(scratch):
        parsed = datetime.fromisoformat(item["event_time"])
        assert parsed.utcoffset() == timedelta(0)
        assert parsed == REFERENCE_TIME + timedelta(seconds=item["TransactionDT"])


def test_equal_times_stay_together_and_numeric_id_tiebreak(scratch):
    report = run(scratch, [row(i, i // 4) for i in reversed(range(1, 21))])
    seen = {}
    for name in ("train", "validation", "test"):
        for item in read(scratch, name):
            assert seen.setdefault(item["TransactionDT"], name) == name
    assert [int(item["transaction_id"]) for item in all_rows(scratch)] == list(range(1, 21))
    assert sum(stats["rows"] for stats in report["partitions"].values()) == 20


def test_one_timestamp_reports_empty_partition(scratch):
    report = run(scratch, [row(i, 0) for i in range(1, 6)])
    assert len([stats for stats in report["partitions"].values() if stats["rows"]]) == 1
    assert len(report["warnings"]) == 3


def test_missingness_and_null_attributes(scratch):
    report = run(scratch, [row(1, ProductCD=""), row(2, ProductCD="W")], [])
    profile = report["source_columns"]["transaction"]["ProductCD"]
    assert profile["missing_count"] == 1 and profile["missing_rate"] == 0.5
    assert all_rows(scratch)[0]["attributes"]["ProductCD"] is None


def test_reproducible_outputs_and_no_overwrite(scratch):
    first = scratch / "a"
    second = scratch / "b"
    assert run(first) == run(second)
    for name in ("train", "validation", "test"):
        assert (first / "processed" / f"{name}.jsonl.gz").read_bytes() == (second / "processed" / f"{name}.jsonl.gz").read_bytes()
    assert (first / "reports/profile.json").read_bytes() == (second / "reports/profile.json").read_bytes()
    with pytest.raises(DatasetError, match="Output already exists"):
        prepare(first / "raw", first / "processed", first / "reports")


def test_missing_data_has_download_instructions(scratch, capsys):
    assert main(["--raw-dir", str(scratch)]) == 1
    error = capsys.readouterr().err
    assert "train_transaction.csv" in error and "train_identity.csv" in error
    assert "kaggle.com" in error and "--raw-dir" in error


@pytest.mark.parametrize("field,value", [("TransactionDT", "-1"), ("TransactionDT", "1.5"),
                                       ("TransactionDT", "999999999999999999999"),
                                       ("TransactionDT", "99999999999999999"),
                                       ("TransactionID", ""), ("TransactionID", "abc")])
def test_invalid_time_and_id(field, value):
    with pytest.raises(DatasetError, match=field):
        map_transaction(row(**{field: value}), {})


def test_missing_required_column(scratch):
    bad = row()
    del bad["isFraud"]
    with pytest.raises(DatasetError, match="missing required columns.*isFraud"):
        run(scratch, [bad])


def test_empty_transactions(scratch):
    with pytest.raises(DatasetError, match="no transaction rows"):
        run(scratch, [])


def test_error_does_not_expose_raw_identifier(scratch, capsys):
    run_root = scratch / "invalid"
    with pytest.raises(DatasetError) as failure:
        run(run_root, [row(card1="secret-card", isFraud="secret-label")])
    assert "secret" not in str(failure.value)
    assert main(["--raw-dir", str(run_root / "raw"), "--processed-dir", str(run_root / "processed"),
                 "--reports-dir", str(run_root / "reports")]) == 1
    assert "secret" not in capsys.readouterr().err


def test_input_row_order_does_not_change_outputs(scratch):
    first, second = scratch / "a", scratch / "b"
    assert run(first, [row(i, i // 3) for i in range(1, 21)]) == run(
        second, [row(i, i // 3) for i in reversed(range(1, 21))])
    for name in ("train", "validation", "test"):
        assert (first / "processed" / f"{name}.jsonl.gz").read_bytes() == (second / "processed" / f"{name}.jsonl.gz").read_bytes()


@pytest.mark.parametrize("contents", [
    "TransactionID,TransactionID\n1,1\n",
    "TransactionID,DeviceInfo\n1\n",
    "TransactionID,DeviceInfo\n1,phone,unexpected\n",
])
def test_bad_identity_csv_rejected(scratch, contents):
    raw = scratch / "raw"
    write_csv(raw / "train_transaction.csv", [row()], list(row()))
    (raw / "train_identity.csv").write_text(contents, encoding="utf-8")
    with pytest.raises(DatasetError, match="duplicate headers|number of fields"):
        prepare(raw, scratch / "processed", scratch / "reports")


def test_profile_does_not_include_raw_fingerprint_values(scratch):
    report = run(scratch, [row(card1="private-card", ProductCD="private-product")],
                 [{"TransactionID": "1", "DeviceInfo": "private-device"}])
    assert "private-" not in json.dumps(report)
    assert "private-card" not in json.dumps(all_rows(scratch))
    assert "private-device" not in json.dumps(all_rows(scratch))
