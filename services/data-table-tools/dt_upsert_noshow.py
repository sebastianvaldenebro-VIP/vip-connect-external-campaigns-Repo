"""
Upsert "No Show" rows into the existing LocationBasedVoicemail-v2 Data Table.
Reads the new CSV and imports only rows where groups == "No Show".
Uses batch_create_data_table_value (idempotent — safe to re-run).

Usage:
  AWS_PROFILE=production python dt_upsert_noshow.py --csv groups_to_fill_new.csv
  python dt_upsert_noshow.py --profile production --csv groups_to_fill_new.csv --dry-run
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import time

import boto3
from botocore.exceptions import ClientError

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

INSTANCE_ID = "6b3f17ba-68a4-472a-9b20-db1991507009"
TABLE_ID = "b6b48300-d3ab-4336-994a-a4bde49d0244"

PRIMARY_ATTRS = ["campaignType", "location", "groups"]
VALUE_ATTRS = ["greeting"]
BATCH_SIZE = 25


def _connect(profile: str | None):
    session = boto3.Session(profile_name=profile or os.environ.get("AWS_PROFILE"))
    return session.client("connect", region_name="us-east-1")


def load_noshow_rows(csv_path: str) -> list[dict]:
    rows = []
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("groups", "").strip() != "No Show":
                continue
            if not all(row.get(col, "").strip() for col in PRIMARY_ATTRS + VALUE_ATTRS):
                log.warning("Row missing required fields — skipped: %s", row)
                continue
            rows.append(row)
    log.info("No Show rows to upsert: %d", len(rows))
    return rows


def import_rows(client, instance_id: str, table_id: str, rows: list[dict]) -> None:
    for attr_name in VALUE_ATTRS:
        values_payload = [
            {
                "AttributeName": attr_name,
                "PrimaryValues": [
                    {"AttributeName": pk, "Value": row[pk].strip()}
                    for pk in PRIMARY_ATTRS
                ],
                "Value": row[attr_name].strip(),
            }
            for row in rows
        ]

        log.info("Upserting attribute '%s' — %d cells …", attr_name, len(values_payload))
        ok = 0
        fail = 0

        for start in range(0, len(values_payload), BATCH_SIZE):
            batch = values_payload[start : start + BATCH_SIZE]
            try:
                resp = client.batch_create_data_table_value(
                    InstanceId=instance_id,
                    DataTableId=table_id,
                    Values=batch,
                )
            except ClientError as exc:
                log.error("BatchCreate failed (batch %d): %s", start // BATCH_SIZE, exc)
                fail += len(batch)
                continue

            ok += len(resp.get("Successful", []))
            for f_item in resp.get("Failed", []):
                fail += 1
                pk_str = "|".join(
                    f"{pv['AttributeName']}={pv['Value']}"
                    for pv in f_item.get("PrimaryValues", [])
                )
                log.warning("Failed cell — pk=%s  msg=%s", pk_str, f_item.get("Message"))

            time.sleep(0.05)

        log.info("  done — ok=%d  failed=%d", ok, fail)


def main() -> None:
    parser = argparse.ArgumentParser(description="Upsert No Show rows into LocationBasedVoicemail-v2")
    parser.add_argument("--profile", help="AWS SSO profile (overrides AWS_PROFILE)")
    parser.add_argument("--csv", required=True, help="CSV file containing the new rows")
    parser.add_argument("--dry-run", action="store_true", help="Show row count only — no AWS calls")
    parser.add_argument("--instance-id", default=INSTANCE_ID, help="Connect instance ID")
    parser.add_argument("--table-id", default=TABLE_ID, help="Data Table ID")
    args = parser.parse_args()

    rows = load_noshow_rows(args.csv)

    if args.dry_run:
        log.info("Dry run — %d No Show rows would be upserted. Exiting.", len(rows))
        return

    client = _connect(args.profile)
    import_rows(client, args.instance_id, args.table_id, rows)
    log.info("Upsert complete.")


if __name__ == "__main__":
    main()
