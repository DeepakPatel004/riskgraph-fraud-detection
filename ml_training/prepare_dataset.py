"""Stream IEEE-CIS CSVs through a private disk workspace, never the API database."""

import argparse
import csv
import gzip
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from pathlib import Path
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REFERENCE_TIME = datetime(2000, 1, 1, tzinfo=timezone.utc)
CARD_FIELDS = tuple(f"card{i}" for i in range(1, 7))
DEVICE_FIELDS = ("DeviceInfo", "DeviceType", "id_30", "id_31", "id_33")
ATTRIBUTE_FIELDS = ("ProductCD",) + tuple(f"M{i}" for i in range(1, 10))
REQUIRED = {"TransactionID", "TransactionDT", "TransactionAmt", "isFraud"}
MISSING = {"", "nan", "null", "none", "na", "n/a"}
MIN_CLASS_COUNT = 20  # Diagnostic threshold, not a guarantee of reliable metrics.


class DatasetError(ValueError):
    """Safe validation error: identifies the column/row, never raw values."""


def clean(value: str | None) -> str | None:
    value = value.strip() if value is not None else ""
    return None if value.lower() in MISSING else value


def integer(value: str | None, column: str) -> int:
    """Identifiers and relative seconds are nonnegative, signed-64-bit integers."""
    value = clean(value)
    if value is None or not value.isascii() or not value.isdecimal():
        raise DatasetError(f"{column} must be a nonnegative integer")
    # Bound the conversion before int(), including adversarial very long CSV cells.
    value = value.lstrip("0") or "0"
    if len(value) > 19:
        raise DatasetError(f"{column} exceeds the supported integer range")
    result = int(value)
    if result > 2**63 - 1:
        raise DatasetError(f"{column} exceeds the supported integer range")
    return result


def fingerprint(kind: str, fields: tuple[str, ...], row: dict, fallback: str) -> str | None:
    """Versioned SHA-256 of ordered (field, value) pairs, independent of labels.

    All-missing cards get a per-transaction token; all-missing devices stay null.
    Hashing is pseudonymization, not encryption or proof of a real identity.
    """
    values = [(field, clean(row.get(field))) for field in fields]
    if not any(value is not None for _, value in values):
        if kind == "device":
            return None
        values = [("missing_card_transaction_id", fallback)]
    payload = json.dumps(["ieee-v1", kind, values], separators=(",", ":"), ensure_ascii=True)
    return f"{kind}_" + hashlib.sha256(payload.encode()).hexdigest()


def map_transaction(row: dict, identity: dict) -> dict:
    identifier = str(integer(row.get("TransactionID"), "TransactionID"))
    seconds = integer(row.get("TransactionDT"), "TransactionDT")
    try:
        event_time = REFERENCE_TIME + timedelta(seconds=seconds)
    except OverflowError as exc:
        raise DatasetError("TransactionDT exceeds the supported datetime range") from exc
    if clean(row.get("isFraud")) not in {"0", "1"}:
        raise DatasetError("isFraud must be binary (0 or 1)")
    try:
        source_amount = Decimal(clean(row.get("TransactionAmt")) or "NaN")
        if not source_amount.is_finite() or not 0 < source_amount < Decimal("1e16"):
            raise DatasetError("TransactionAmt must be finite, positive and fit Numeric(18,2)")
        amount = source_amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
        if not 0 < amount < Decimal("1e16"):
            raise DatasetError("TransactionAmt rounds outside the positive Numeric(18,2) range")
    except InvalidOperation as exc:
        raise DatasetError("TransactionAmt must be a valid decimal amount") from exc
    return {
        "transaction_id": identifier,
        "amount": str(amount),
        "TransactionAmt": str(source_amount),
        "currency": "USD",
        "TransactionDT": seconds,
        "event_time": event_time.isoformat().replace("+00:00", "Z"),
        "card_token": fingerprint("card", CARD_FIELDS, row, identifier),
        "device_token": fingerprint("device", DEVICE_FIELDS, identity, identifier),
        "isFraud": int(row["isFraud"].strip()),
        "attributes": {field: clean(row.get(field)) for field in ATTRIBUTE_FIELDS},
    }


@contextmanager
def csv_rows(path: Path, required: set[str]):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = reader.fieldnames or []
        if len(headers) != len(set(headers)) or not required.issubset(headers):
            raise DatasetError(f"{path.name}: duplicate headers or missing required columns: "
                               + ", ".join(sorted(required - set(headers))))
        yield headers, reader


def observe(row: dict, headers: list[str], missing: Counter) -> None:
    if None in row or any(row.get(field) is None for field in headers):
        raise DatasetError("CSV row has a different number of fields than its header")
    missing.update(field for field in headers if clean(row[field]) is None)


def column_profile(headers: list[str], missing: Counter, count: int) -> dict:
    # CSV has no native types; do not pretend to infer types from a small sample.
    return {field: {"source_type": "CSV text", "missing_count": missing[field],
                    "missing_rate": missing[field] / count if count else None}
            for field in headers}


def split_boundaries(db: sqlite3.Connection, count: int) -> tuple[int, int]:
    """Choose group ends nearest 70% and 85%; ties prefer the earlier end.

    Empty partitions are possible for very few timestamp groups and are reported.
    TransactionID orders within a group but never separates equal-time events.
    """
    ends = [0]
    for (size,) in db.execute("SELECT count(*) FROM transactions GROUP BY seconds ORDER BY seconds"):
        ends.append(ends[-1] + size)
    return tuple(min(ends, key=lambda end: (abs(end * 100 - target * count), end))
                 for target in (70, 85))


def prepare(raw_dir: Path, processed_dir: Path, reports_dir: Path) -> dict:
    """Validate everything before output; refuse to overwrite any existing output.

    Memory use is bounded by one CSV row plus timestamp-group counts. SQLite's
    private workspace performs the validated left join and chronological sort.
    It is always removed and never shares a connection with the backend.
    """
    raw_dir, processed_dir, reports_dir = map(Path, (raw_dir, processed_dir, reports_dir))
    inputs = [raw_dir / f"train_{name}.csv" for name in ("transaction", "identity")]
    absent = [path.name for path in inputs if not path.is_file()]
    if absent:
        raise DatasetError("Missing dataset files: " + ", ".join(absent)
                           + ". Sign in at https://www.kaggle.com/competitions/ieee-fraud-detection/data, "
                           "accept the competition rules, download and extract both training CSVs "
                           f"into {raw_dir}, or pass --raw-dir. No automatic download is performed.")
    outputs = [processed_dir / f"{name}.jsonl.gz" for name in ("train", "validation", "test")]
    report_path = reports_dir / "profile.json"
    if any(path.exists() for path in [*outputs, report_path]):
        raise DatasetError("Output already exists; choose new --processed-dir and --reports-dir paths")
    processed_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    workspace = processed_dir / f".preparation-{uuid4().hex}.sqlite3"
    try:
        with ExitStack() as stack:
            db = sqlite3.connect(workspace)
            stack.callback(db.close)
            db.execute("CREATE TABLE identities (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE transactions (id INTEGER PRIMARY KEY, seconds INTEGER, payload TEXT)")
            identity_missing, transaction_missing = Counter(), Counter()
            identity_count = count = rounded = 0
            with csv_rows(inputs[1], {"TransactionID"}) as (identity_headers, rows):
                for line, row in enumerate(rows, 2):
                    try:
                        observe(row, identity_headers, identity_missing)
                        key = integer(row["TransactionID"], "TransactionID")
                        # Only fingerprint columns enter the temporary join table.
                        selected = {field: clean(row.get(field)) for field in DEVICE_FIELDS}
                        db.execute("INSERT INTO identities VALUES (?, ?)", (key, json.dumps(selected)))
                        identity_count += 1
                    except (DatasetError, sqlite3.IntegrityError) as exc:
                        message = str(exc) if isinstance(exc, DatasetError) else "duplicate TransactionID"
                        raise DatasetError(f"train_identity.csv row {line}: {message}") from exc
            with csv_rows(inputs[0], REQUIRED) as (transaction_headers, rows):
                for line, row in enumerate(rows, 2):
                    try:
                        observe(row, transaction_headers, transaction_missing)
                        key = integer(row["TransactionID"], "TransactionID")
                        match = db.execute("SELECT payload FROM identities WHERE id=?", (key,)).fetchone()
                        item = map_transaction(row, json.loads(match[0]) if match else {})
                        rounded += Decimal(item["amount"]) != Decimal(item["TransactionAmt"])
                        db.execute("INSERT INTO transactions VALUES (?, ?, ?)",
                                   (key, item["TransactionDT"], json.dumps(item, separators=(",", ":"))))
                        count += 1
                    except (DatasetError, sqlite3.IntegrityError) as exc:
                        message = str(exc) if isinstance(exc, DatasetError) else "duplicate TransactionID"
                        raise DatasetError(f"train_transaction.csv row {line}: {message}") from exc
            if not count:
                raise DatasetError("train_transaction.csv contains no transaction rows")
            db.execute("CREATE INDEX temporal_order ON transactions(seconds, id)")
            train_end, validation_end = split_boundaries(db, count)
            matched = db.execute("SELECT count(*) FROM transactions t JOIN identities i ON t.id=i.id").fetchone()[0]
            report = {
                "pipeline_version": 1, "synthetic_reference_utc": REFERENCE_TIME.isoformat(),
                "currency_assumption": "USD (prototype assumption, not verified dataset metadata)",
                "transaction_rows": count, "identity_rows": identity_count,
                "matched_identity_rows": matched, "missing_identity_rows": count - matched,
                "unmatched_identity_rows": identity_count - matched, "rounded_amount_rows": rounded,
                "source_columns": {
                    "transaction": column_profile(transaction_headers, transaction_missing, count),
                    "identity": column_profile(identity_headers, identity_missing, identity_count),
                },
                "prepared_types": {"transaction_id": "string", "amount": "Decimal string (2 places)",
                                   "TransactionAmt": "original Decimal string", "TransactionDT": "integer seconds",
                                   "event_time": "synthetic UTC ISO-8601", "isFraud": "binary integer",
                                   "card_token": "string", "device_token": "string or null",
                                   "attributes": "allowlisted strings or null", "currency": "string"},
                "split_policy": "nearest timestamp-group ends to cumulative 70%/85%; ties earlier",
                "minimum_class_count_diagnostic": MIN_CLASS_COUNT,
                "partitions": {}, "warnings": [], "class_counts": {"0": 0, "1": 0},
            }
            for name in ("train", "validation", "test"):
                report["partitions"][name] = {"rows": 0, "class_counts": {"0": 0, "1": 0},
                                              "min_TransactionDT": None, "max_TransactionDT": None,
                                              "missing_device_rows": 0}
            # Fixed gzip metadata makes identical inputs produce byte-identical files.
            writers = [stack.enter_context(gzip.GzipFile(filename="", mode="wb", mtime=0,
                        fileobj=stack.enter_context(path.open("xb")))) for path in outputs]
            for index, (payload,) in enumerate(db.execute("SELECT payload FROM transactions ORDER BY seconds, id")):
                partition = 0 if index < train_end else 1 if index < validation_end else 2
                writers[partition].write((payload + "\n").encode())
                item = json.loads(payload)
                stats = report["partitions"][("train", "validation", "test")[partition]]
                stats["rows"] += 1
                stats["class_counts"][str(item["isFraud"])] += 1
                report["class_counts"][str(item["isFraud"])] += 1
                if stats["min_TransactionDT"] is None:
                    stats["min_TransactionDT"] = item["TransactionDT"]
                stats["max_TransactionDT"] = item["TransactionDT"]
                stats["missing_device_rows"] += item["device_token"] is None
            for name, stats in report["partitions"].items():
                stats["fraction"] = stats["rows"] / count
                stats["fraud_rate"] = stats["class_counts"]["1"] / stats["rows"] if stats["rows"] else None
                if min(stats["class_counts"].values()) < MIN_CLASS_COUNT:
                    report["warnings"].append(f"{name}: fewer than {MIN_CLASS_COUNT} examples of at least one class; metrics may be unreliable")
            report["fraud_rate"] = report["class_counts"]["1"] / count
            with report_path.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(report, handle, indent=2, sort_keys=True)
                handle.write("\n")
        return report
    finally:
        # Delete only this run's private workspace; never an existing database.
        workspace.unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, default=PROJECT_ROOT / "data/raw")
    parser.add_argument("--processed-dir", type=Path, default=PROJECT_ROOT / "data/processed")
    parser.add_argument("--reports-dir", type=Path, default=PROJECT_ROOT / "data/reports")
    args = parser.parse_args(argv)
    try:
        report = prepare(args.raw_dir, args.processed_dir, args.reports_dir)
    except (DatasetError, OSError, sqlite3.Error, csv.Error) as exc:
        # No raw CSV values or driver tracebacks are printed.
        message = str(exc) if isinstance(exc, DatasetError) else f"Local I/O failure ({type(exc).__name__}); check paths, permissions and disk space"
        print(f"Preparation failed: {message}", file=sys.stderr)
        return 1
    print(json.dumps({key: report[key] for key in ("transaction_rows", "class_counts", "partitions", "warnings")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
