"""
Amazon Connect Data Table — export / patch / import utility.

Usage
-----
# 1. Export all rows to CSV (you must supply the primary-key values)
python dt_export_import.py export \
    --instance-id  6b3f17ba-68a4-472a-9b20-db1991507009 \
    --table-id     <DataTableId> \
    --pk-csv       primary_keys.csv \
    --out          table_export.csv

# 2. Import / update rows from CSV (fills missing field or updates values)
python dt_export_import.py import \
    --instance-id  6b3f17ba-68a4-472a-9b20-db1991507009 \
    --table-id     <DataTableId> \
    --csv          table_export.csv \
    --mode         update    # update | create

Notes
-----
PRIMARY-KEY CSV FORMAT (--pk-csv):
  A CSV whose column headers match the primary-attribute names of the table.
  Each row is one primary-key combination to read.
  Example (table primary attributes: Language, Department):
    Language,Department
    English,Sales
    Spanish,Sales

EXPORT CSV FORMAT (--out / --csv):
  All columns: primary attributes first (sorted), then value attributes.
  _lock_* columns are internal metadata written during export and required
  during import so AWS optimistic-locking passes.  Do not edit them.

IMPORT MODE:
  update — calls BatchUpdateDataTableValue. Use when the attribute already
           exists in the table but the cell is empty or wrong.
  create — calls BatchCreateDataTableValue. Use when a new attribute was just
           added and no cell values exist yet for that attribute.

BATCH SIZE:
  AWS limits BatchDescribeDataTableValue / BatchUpdateDataTableValue to
  reasonable page sizes; this script uses 25 per request (conservative).
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

BATCH_SIZE = 25
INSTANCE_ID = os.environ.get(
    "CONNECT_INSTANCE_ID", "6b3f17ba-68a4-472a-9b20-db1991507009"
)


# ---------------------------------------------------------------------------
# AWS client
# ---------------------------------------------------------------------------


def _connect(profile: str | None = None) -> Any:
    session = boto3.Session(profile_name=profile or os.environ.get("AWS_PROFILE"))
    return session.client("connect", region_name="us-east-1")


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------


def get_attributes(client, instance_id: str, table_id: str) -> list[dict]:
    """Return all attribute definitions for the table."""
    attrs: list[dict] = []
    next_token = None
    while True:
        kwargs: dict = {
            "InstanceId": instance_id,
            "DataTableId": table_id,
            "MaxResults": 100,
        }
        if next_token:
            kwargs["NextToken"] = next_token
        resp = client.list_data_table_attributes(**kwargs)
        attrs.extend(resp.get("Attributes", []))
        next_token = resp.get("NextToken")
        if not next_token:
            break
    return attrs


def primary_attr_names(attrs: list[dict]) -> list[str]:
    return sorted(a["Name"] for a in attrs if a.get("Primary"))


def value_attr_names(attrs: list[dict]) -> list[str]:
    return sorted(a["Name"] for a in attrs if not a.get("Primary"))


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export(
    client,
    instance_id: str,
    table_id: str,
    pk_csv_path: str,
    out_path: str,
) -> None:
    attrs = get_attributes(client, instance_id, table_id)
    pk_names = primary_attr_names(attrs)
    val_names = value_attr_names(attrs)

    if not pk_names:
        log.warning(
            "Table has no primary attributes — at most one row exists; fetching it."
        )

    # Read primary-key combinations from the user-supplied CSV
    pk_combos: list[list[dict]] = []
    with open(pk_csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            pk_combo = [
                {"AttributeName": k, "Value": str(v)}
                for k, v in row.items()
                if k in pk_names
            ]
            if len(pk_combo) != len(pk_names):
                missing = set(pk_names) - {e["AttributeName"] for e in pk_combo}
                log.warning("Row missing primary keys %s — skipped: %s", missing, row)
                continue
            pk_combos.append(pk_combo)

    log.info("Primary keys to fetch: %d", len(pk_combos))

    # Build BatchDescribeDataTableValue requests per value attribute
    # Each request specifies: which attribute to read + list of PK combos
    all_rows: dict[str, dict] = {}  # key = canonical PK string → row dict

    for attr_name in val_names:
        log.info("Fetching attribute '%s' …", attr_name)
        identifiers = [
            {"AttributeName": attr_name, "PrimaryValues": pk} for pk in pk_combos
        ]

        for batch_start in range(0, len(identifiers), BATCH_SIZE):
            batch = identifiers[batch_start : batch_start + BATCH_SIZE]
            try:
                resp = client.batch_describe_data_table_value(
                    InstanceId=instance_id,
                    DataTableId=table_id,
                    Values=batch,
                )
            except ClientError as exc:
                log.error("BatchDescribe failed for attr '%s': %s", attr_name, exc)
                continue

            for item in resp.get("Successful", []):
                pk_key = _pk_key(item["PrimaryValues"])
                if pk_key not in all_rows:
                    all_rows[pk_key] = {
                        pv["AttributeName"]: pv["Value"] for pv in item["PrimaryValues"]
                    }
                all_rows[pk_key][attr_name] = item.get("Value", "")
                # Store lock version for later update
                lv = item.get("LockVersion", {})
                all_rows[pk_key][f"_lock_{attr_name}"] = json.dumps(lv)

            for failure in resp.get("Failed", []):
                pk_key = _pk_key(failure.get("PrimaryValues", []))
                log.debug(
                    "Not found — attr '%s' pk '%s': %s",
                    attr_name,
                    pk_key,
                    failure.get("Message"),
                )
                if pk_key not in all_rows:
                    all_rows[pk_key] = {
                        pv["AttributeName"]: pv["Value"]
                        for pv in failure.get("PrimaryValues", [])
                    }
                all_rows[pk_key].setdefault(attr_name, "")

            time.sleep(0.05)  # stay under throttle

    # Write CSV
    if not all_rows:
        log.warning(
            "No rows retrieved — check that pk_csv contains the correct primary key column headers."
        )
        return

    fieldnames = pk_names + val_names + [f"_lock_{n}" for n in val_names]
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in all_rows.values():
            writer.writerow(row)

    log.info("Exported %d rows → %s", len(all_rows), out_path)
    log.info("Columns: %s", pk_names + val_names)
    log.info(
        "Next step: open %s, fill in the missing column, then run the import command.",
        out_path,
    )


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def import_csv(
    client,
    instance_id: str,
    table_id: str,
    csv_path: str,
    mode: str,
) -> None:
    attrs = get_attributes(client, instance_id, table_id)
    pk_names = primary_attr_names(attrs)
    val_names = value_attr_names(attrs)

    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    log.info("Rows to process: %d  mode=%s", len(rows), mode)

    # For each VALUE attribute that appears in the CSV, batch-update/create
    for attr_name in val_names:
        if attr_name not in (reader.fieldnames or []):
            log.info("Attribute '%s' not in CSV — skipping.", attr_name)
            continue

        values_payload: list[dict] = []
        for row in rows:
            cell_value = row.get(attr_name, "")
            lock_raw = row.get(f"_lock_{attr_name}", "")
            lock_version = json.loads(lock_raw) if lock_raw else None

            entry: dict = {
                "AttributeName": attr_name,
                "PrimaryValues": [
                    {"AttributeName": pk, "Value": row[pk]} for pk in pk_names
                ],
                "Value": cell_value,
            }
            if lock_version:
                entry["LockVersion"] = lock_version

            values_payload.append(entry)

        log.info(
            "Uploading attribute '%s' — %d cells …", attr_name, len(values_payload)
        )
        ok = 0
        fail = 0
        for batch_start in range(0, len(values_payload), BATCH_SIZE):
            batch = values_payload[batch_start : batch_start + BATCH_SIZE]
            try:
                if mode == "create":
                    resp = client.batch_create_data_table_value(
                        InstanceId=instance_id,
                        DataTableId=table_id,
                        Values=batch,
                    )
                else:
                    resp = client.batch_update_data_table_value(
                        InstanceId=instance_id,
                        DataTableId=table_id,
                        Values=batch,
                    )
            except ClientError as exc:
                log.error("Batch %s failed for attr '%s': %s", mode, attr_name, exc)
                fail += len(batch)
                continue

            ok += len(resp.get("Successful", []))
            fail += len(resp.get("Failed", []))
            for f_item in resp.get("Failed", []):
                log.warning(
                    "Failed cell — pk=%s msg=%s",
                    _pk_key(f_item.get("PrimaryValues", [])),
                    f_item.get("Message"),
                )
            time.sleep(0.05)

        log.info("  attribute '%s' done — ok=%d failed=%d", attr_name, ok, fail)

    log.info("Import complete.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pk_key(primary_values: list[dict]) -> str:
    return "|".join(
        f"{pv['AttributeName']}={pv['Value']}"
        for pv in sorted(primary_values, key=lambda x: x["AttributeName"])
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Amazon Connect Data Table export/import utility"
    )
    parser.add_argument(
        "--profile", help="AWS profile name (overrides AWS_PROFILE env var)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    exp = sub.add_parser("export", help="Export all rows to CSV")
    exp.add_argument("--instance-id", default=INSTANCE_ID, help="Connect instance ID")
    exp.add_argument("--table-id", required=True, help="Data Table ID or ARN")
    exp.add_argument(
        "--pk-csv",
        required=True,
        help="CSV file with primary-key columns (used to locate rows)",
    )
    exp.add_argument("--out", required=True, help="Output CSV path")

    imp = sub.add_parser("import", help="Update/create table values from CSV")
    imp.add_argument("--instance-id", default=INSTANCE_ID, help="Connect instance ID")
    imp.add_argument("--table-id", required=True, help="Data Table ID or ARN")
    imp.add_argument(
        "--csv", required=True, help="CSV file (from export, with missing field filled)"
    )
    imp.add_argument(
        "--mode",
        choices=["update", "create"],
        default="update",
        help="update = BatchUpdateDataTableValue (existing cells), create = BatchCreateDataTableValue (new attribute)",
    )

    info = sub.add_parser(
        "describe", help="Print table attributes (column names and types)"
    )
    info.add_argument("--instance-id", default=INSTANCE_ID, help="Connect instance ID")
    info.add_argument("--table-id", required=True, help="Data Table ID or ARN")

    args = parser.parse_args()
    client = _connect(args.profile)

    if args.command == "export":
        export(client, args.instance_id, args.table_id, args.pk_csv, args.out)
    elif args.command == "import":
        import_csv(client, args.instance_id, args.table_id, args.csv, args.mode)
    elif args.command == "describe":
        attrs = get_attributes(client, args.instance_id, args.table_id)
        pk_names = primary_attr_names(attrs)
        val_names = value_attr_names(attrs)
        print("\nPRIMARY ATTRIBUTES (keys):")
        for a in attrs:
            if a.get("Primary"):
                print(
                    f"  {a['Name']:30s}  type={a.get('ValueType', '?')}  id={a['AttributeId']}"
                )
        print("\nVALUE ATTRIBUTES:")
        for a in attrs:
            if not a.get("Primary"):
                print(
                    f"  {a['Name']:30s}  type={a.get('ValueType', '?')}  id={a['AttributeId']}"
                )
        print(f"\nTotal: {len(pk_names)} primary + {len(val_names)} value attributes")


if __name__ == "__main__":
    main()
