"""
Create a NEW Amazon Connect Data Table with 3 primary keys
(campaignType, location, groups) and populate it from a CSV.

Usage
-----
python dt_create_v2.py --profile production --csv filled_groups.csv

The script:
  1. Creates a new Data Table named 'vip-greeting-lookup-v2'
  2. Adds primary attributes: campaignType, location, groups (all TEXT)
  3. Adds value attribute: greeting (TEXT)
  4. Imports all valid rows from the CSV
  5. Prints the new table ARN

Rows where groups == '(empty text) (Default)' or empty string are skipped —
those represented a UI artifact from the original 2-key table. Add a
Default/catch-all row manually if needed.
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

INSTANCE_ID = os.environ.get(
    "CONNECT_INSTANCE_ID", "6b3f17ba-68a4-472a-9b20-db1991507009"
)
TABLE_NAME = "LocationBasedVoicemail-v2"
TABLE_DESCRIPTION = "Greeting prompt lookup by campaign type, location, and contact group"
TABLE_TIMEZONE = "US/Eastern"

PRIMARY_ATTRS = ["campaignType", "location", "groups"]
VALUE_ATTRS = ["greeting"]

BATCH_SIZE = 25
INVALID_GROUPS = {"(empty text) (Default)", ""}


def _connect(profile: str | None) -> object:
    session = boto3.Session(profile_name=profile or os.environ.get("AWS_PROFILE"))
    return session.client("connect", region_name="us-east-1")


def create_table(client, instance_id: str) -> tuple[str, str]:
    log.info("Creating table '%s' …", TABLE_NAME)
    resp = client.create_data_table(
        InstanceId=instance_id,
        Name=TABLE_NAME,
        Description=TABLE_DESCRIPTION,
        TimeZone=TABLE_TIMEZONE,
        ValueLockLevel="PRIMARY_VALUE",
        Status="PUBLISHED",
        Tags={
            "Project": "vip-connect-external-campaigns",
            "Environment": "prod",
            "ManagedBy": "dt_create_v2.py",
        },
    )
    table_id: str = resp["Id"]
    table_arn: str = resp["Arn"]
    log.info("Table created — id=%s", table_id)
    log.info("  ARN: %s", table_arn)
    return table_id, table_arn


def add_attributes(client, instance_id: str, table_id: str) -> None:
    for name in PRIMARY_ATTRS:
        log.info("Adding PRIMARY attribute '%s' …", name)
        client.create_data_table_attribute(
            InstanceId=instance_id,
            DataTableId=table_id,
            Name=name,
            ValueType="TEXT",
            Primary=True,
        )
        time.sleep(0.2)

    for name in VALUE_ATTRS:
        log.info("Adding VALUE attribute '%s' …", name)
        client.create_data_table_attribute(
            InstanceId=instance_id,
            DataTableId=table_id,
            Name=name,
            ValueType="TEXT",
            Primary=False,
        )
        time.sleep(0.2)


def load_csv(csv_path: str) -> list[dict]:
    rows = []
    skipped = []
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            groups_val = row.get("groups", "").strip()
            if groups_val in INVALID_GROUPS:
                skipped.append(row)
                continue
            # Validate all required columns are present and non-empty
            if not all(row.get(col, "").strip() for col in PRIMARY_ATTRS + VALUE_ATTRS):
                log.warning("Row missing required fields — skipped: %s", row)
                skipped.append(row)
                continue
            rows.append(row)

    if skipped:
        log.info(
            "Skipped %d row(s) (empty/invalid groups): %s",
            len(skipped),
            [r.get("campaignType") + "/" + r.get("location", "") for r in skipped],
        )
    log.info("Rows to import: %d", len(rows))
    return rows


def import_rows(
    client, instance_id: str, table_id: str, rows: list[dict]
) -> None:
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

        log.info("Importing attribute '%s' — %d cells …", attr_name, len(values_payload))
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

        log.info("  attribute '%s' done — ok=%d  failed=%d", attr_name, ok, fail)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a new Connect Data Table (3 primary keys) and populate from CSV"
    )
    parser.add_argument("--profile", help="AWS profile (overrides AWS_PROFILE env var)")
    parser.add_argument(
        "--instance-id", default=INSTANCE_ID, help="Connect instance ID"
    )
    parser.add_argument(
        "--csv", required=True, help="CSV with columns: campaignType, location, greeting, groups"
    )
    parser.add_argument(
        "--table-id",
        help="Existing table ID — skips table creation and goes straight to attributes + import",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse CSV and print row counts only — no AWS calls",
    )
    args = parser.parse_args()

    rows = load_csv(args.csv)
    if args.dry_run:
        log.info("Dry run — %d rows would be imported. Exiting.", len(rows))
        return

    client = _connect(args.profile)

    if args.table_id:
        table_id = args.table_id
        table_arn = (
            f"arn:aws:connect:us-east-1:165505826690:instance/"
            f"{args.instance_id}/data-table/{table_id}"
        )
        log.info("Using existing table — id=%s", table_id)
    else:
        table_id, table_arn = create_table(client, args.instance_id)

    add_attributes(client, args.instance_id, table_id)
    import_rows(client, args.instance_id, table_id, rows)

    print("\n" + "=" * 60)
    print("NEW TABLE CREATED SUCCESSFULLY")
    print("=" * 60)
    print(f"Name : {TABLE_NAME}")
    print(f"ID   : {table_id}")
    print(f"ARN  : {table_arn}")
    print("=" * 60)
    print("Next: update your Connect flows to use the new table ARN.")
    print("The lookup key is now: (campaignType, location, groups) → greeting")


if __name__ == "__main__":
    main()
