"""Enrich prepared IEEE-CIS partitions with strictly historical graph features."""

import argparse
import gzip
import hashlib
import json
import shutil
import sqlite3
import sys
from collections import Counter
from contextlib import ExitStack
from dataclasses import asdict
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_EVEN
from itertools import groupby
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from pydantic import ValidationError

from backend.app.graph.models import TransactionNode
from ml_training.historical_features import HistoricalFeatureBuilder
from ml_training.prepare_dataset import ATTRIBUTE_FIELDS, DatasetError, PROJECT_ROOT, REFERENCE_TIME, integer


SPLITS = ("train", "validation", "test")
NODE_FIELDS = ("transaction_id", "amount", "currency", "card_token", "device_token", "event_time")
PREPARED_FIELDS = {*NODE_FIELDS, "TransactionDT", "TransactionAmt", "attributes", "isFraud"}
FEATURE_NAMES = ("card_txn_count_24h", "device_txn_count_24h", "card_unique_devices_7d",
                 "device_unique_cards_7d", "card_avg_amount_7d")


def validate_row(item: dict) -> TransactionNode:
    """Accept the Phase 3.1 schema only; labels are validated but never modeled."""
    if not isinstance(item, dict) or set(item) != PREPARED_FIELDS:
        raise DatasetError("Expected the original Phase 3.1 prepared schema")
    if type(item["TransactionDT"]) is not int or item["TransactionDT"] < 0:
        raise DatasetError("TransactionDT must be nonnegative integer seconds")
    if not isinstance(item["transaction_id"], str):
        raise DatasetError("transaction_id must be a canonical numeric string")
    identifier = integer(item["transaction_id"], "transaction_id")
    if str(identifier) != item["transaction_id"]:
        raise DatasetError("transaction_id must be a canonical numeric string")
    if type(item["isFraud"]) is not int or item["isFraud"] not in (0, 1):
        raise DatasetError("isFraud must be binary")
    if (not isinstance(item["attributes"], dict) or set(item["attributes"]) != set(ATTRIBUTE_FIELDS)
            or any(value is not None and not isinstance(value, str) for value in item["attributes"].values())):
        raise DatasetError("Unexpected transaction-level modeling attributes")
    if not isinstance(item["amount"], str) or not isinstance(item["TransactionAmt"], str):
        raise DatasetError("Prepared amounts must be Decimal strings")
    try:
        original_amount = Decimal(item["TransactionAmt"])
        if not original_amount.is_finite() or original_amount <= 0:
            raise ValueError
        node = TransactionNode(**{key: item[key] for key in NODE_FIELDS})
        if original_amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN) != node.amount:
            raise DatasetError("Prepared amount does not match rounded TransactionAmt")
        if node.event_time != REFERENCE_TIME + timedelta(seconds=item["TransactionDT"]):
            raise DatasetError("event_time does not match synthetic UTC TransactionDT mapping")
    except (ValidationError, ValueError, ArithmeticError, OverflowError) as exc:
        raise DatasetError("Invalid graph fields, amounts or synthetic UTC event_time") from exc
    return node


def prepared_rows(input_dir: Path, ids: sqlite3.Connection):
    """Stream existing splits in order; reject invalid ordering and duplicate IDs.

    Disk-backed IDs detect repeats even after their historical windows expire.
    A timestamp group crossing a partition boundary is rejected, not reassigned.
    """
    previous = None
    previous_split = None
    for split in SPLITS:
        with gzip.open(input_dir / f"{split}.jsonl.gz", "rt", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                try:
                    item = json.loads(line)
                    node = validate_row(item)
                    order = (item["TransactionDT"], int(item["transaction_id"]))
                    if previous is not None and order <= previous:
                        raise DatasetError("Rows must increase by TransactionDT and numeric transaction_id")
                    if previous is not None and previous_split != split and order[0] == previous[0]:
                        raise DatasetError("Equal timestamps cannot cross split boundaries")
                    ids.execute("INSERT INTO seen VALUES (?)", (int(item["transaction_id"]),))
                except (DatasetError, json.JSONDecodeError, sqlite3.IntegrityError) as exc:
                    reason = str(exc) if isinstance(exc, DatasetError) else "Invalid JSON or duplicate transaction_id"
                    raise DatasetError(f"{split}.jsonl.gz line {line_number}: {reason}") from exc
                previous, previous_split = order, split
                yield split, item, node


def distribution_update(stats: dict, value):
    value = Decimal(value)
    stats["count"] += 1
    stats["sum"] += value
    stats["min"] = value if stats["min"] is None else min(stats["min"], value)
    stats["max"] = value if stats["max"] is None else max(stats["max"], value)
    bucket = next((f"<= {upper}" for upper in (0, 1, 5, 10, 50, 100) if value <= upper), "> 100")
    stats["buckets"][bucket] += 1


def peak_memory_bytes() -> tuple[int | None, str]:
    """OS-measured process peak RSS/working set, including interpreter overhead."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class MemoryCounters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                    (name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize",
                    "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                    "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetCurrentProcess.restype = wintypes.HANDLE
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD]
            counters = MemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            if psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
                return counters.PeakWorkingSetSize, "Windows process peak working set"
        except (OSError, AttributeError):
            pass
    else:
        try:
            import resource
            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            return int(peak if sys.platform == "darwin" else peak * 1024), "OS process peak RSS"
        except (ImportError, OSError, AttributeError):
            pass
    return None, "Unavailable on this platform"


def build(input_dir: Path, output_dir: Path) -> dict:
    """Publish a complete new directory; never modify inputs or existing outputs.

    On failure only the uniquely named private sibling staging directory is
    removed. Existing artifacts are refused, even if they appear compatible.
    Dataset bytes are deterministic; measured duration/memory in the report vary.
    """
    started = perf_counter()
    input_dir, output_dir = Path(input_dir).resolve(), Path(output_dir).resolve()
    if output_dir.exists() or output_dir == input_dir or input_dir.is_relative_to(output_dir):
        raise DatasetError("Output location already exists or overlaps inputs; choose a new --output-dir")
    if any(not (input_dir / f"{split}.jsonl.gz").is_file() for split in SPLITS):
        raise DatasetError("Missing prepared train/validation/test.jsonl.gz files; run Phase 3.1 first or pass --input-dir")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = output_dir.parent / f".graph-features-{uuid4().hex}"
    staging.mkdir()
    try:
        state = HistoricalFeatureBuilder()
        distributions = {name: {"count": 0, "sum": Decimal(0), "min": None, "max": None,
                                "buckets": Counter()} for name in FEATURE_NAMES}
        counts = Counter()
        missing_devices = no_card_history = no_device_history = no_present_device_history = 0
        ids = sqlite3.connect(staging / "seen.sqlite3")
        try:
            ids.execute("CREATE TABLE seen (id INTEGER PRIMARY KEY)")
            with ExitStack() as stack:
                writers = {split: stack.enter_context(gzip.GzipFile(filename="", mode="wb", mtime=0,
                    fileobj=stack.enter_context((staging / f"{split}.jsonl.gz").open("xb")))) for split in SPLITS}
                stream = prepared_rows(input_dir, ids)
                stack.callback(stream.close)
                for _, group in groupby(stream, key=lambda record: record[1]["TransactionDT"]):
                    records = list(group)  # Memory bounded by active history + largest equal-time group.
                    features = state.process_group([node for _, _, node in records])
                    for (split, item, node), feature in zip(records, features):
                        values = asdict(feature)
                        for name, value in values.items():
                            distribution_update(distributions[name], value)
                        no_card_history += feature.card_avg_amount_7d == 0
                        no_device_history += feature.device_unique_cards_7d == 0
                        no_present_device_history += node.device_token is not None and feature.device_unique_cards_7d == 0
                        missing_devices += node.device_token is None
                        values["card_avg_amount_7d"] = str(feature.card_avg_amount_7d)
                        enriched = {**item, **values, "device_missing": int(node.device_token is None)}
                        writers[split].write((json.dumps(enriched, separators=(",", ":")) + "\n").encode())
                        counts[split] += 1
        finally:
            ids.close()
        (staging / "seen.sqlite3").unlink()
        total = sum(counts.values())
        if not total:
            raise DatasetError("Prepared partitions contain no transactions")
        feature_report = {}
        for name, stats in distributions.items():
            feature_report[name] = {"count": stats["count"], "min": str(stats["min"]),
                                    "max": str(stats["max"]), "mean": str(stats["sum"] / stats["count"]),
                                    "buckets": dict(stats["buckets"])}
        peak, memory_method = peak_memory_bytes()
        report = {
            "pipeline_version": 1, "processed_transactions": total,
            "rows_per_split": {split: counts[split] for split in SPLITS},
            "missing_device_percentage": missing_devices * 100 / total,
            "no_prior_card_history_percentage": no_card_history * 100 / total,
            "no_prior_device_history_percentage": no_device_history * 100 / total,
            "no_prior_present_device_history_percentage": no_present_device_history * 100 / (total - missing_devices) if total > missing_devices else None,
            "history_definition": "7-day history; all-row device percentage includes missing devices",
            "feature_distributions": feature_report,
            "integrity_checks": {"unique_transaction_ids": True, "chronological_order": True,
                "synthetic_utc_mapping": True, "equal_time_groups_within_split": True,
                "original_split_membership_preserved": True, "prepared_schema_valid": True},
            "output_sha256": {},
            "processing_seconds": perf_counter() - started,
            "approximate_peak_memory_bytes": peak, "memory_measurement": memory_method,
        }
        for split in SPLITS:
            with (staging / f"{split}.jsonl.gz").open("rb") as handle:
                report["output_sha256"][split] = hashlib.file_digest(handle, "sha256").hexdigest()
        with (staging / "report.json").open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
            handle.write("\n")
        if output_dir.exists():
            raise DatasetError("Output location appeared during processing; refusing to replace it")
        staging.rename(output_dir)
        return report
    finally:
        if staging.exists():
            assert staging.resolve().parent == output_dir.parent
            shutil.rmtree(staging)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=PROJECT_ROOT / "data/processed")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "data/graph_features")
    args = parser.parse_args(argv)
    try:
        report = build(args.input_dir, args.output_dir)
    except (DatasetError, ValueError, OSError, sqlite3.Error, EOFError) as exc:
        message = str(exc) if isinstance(exc, DatasetError) else f"Feature generation failed ({type(exc).__name__}); check local data, currencies, paths and disk space"
        print(message, file=sys.stderr)
        return 1
    print(json.dumps({key: report[key] for key in ("processed_transactions", "rows_per_split",
                     "processing_seconds", "approximate_peak_memory_bytes")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
