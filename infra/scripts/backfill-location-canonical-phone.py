#!/usr/bin/env python3
"""One-off backfill: add canonicalPhone/areaCodes to every VipLocationMapping item.

Source of truth for the values: frontend/src/lib/areaCodeMap.ts as of 2026-08-18.
Run once. Safe to re-run (idempotent — always overwrites with the same values).

Usage:
    AWS_PROFILE=production python3 backfill-location-canonical-phone.py --dry-run
    AWS_PROFILE=production python3 backfill-location-canonical-phone.py
"""
import argparse
import sys

import boto3

TABLE_NAME = "VipLocationMapping"

CANONICAL_PHONES = {
    "NY":  "+19174105649",
    "LI":  "+16314497507",
    "NJ":  "+19734949660",
    "MD":  "+13018594566",
    "CT":  "+14753656590",
    "TX":  "+15126508970",
    "SCA": "+18588686651",
    "NCA": "+16694674988",
    "PA":  "+12154009167",
}

AREA_CODES = {
    "NY":  ["212", "646", "332", "718", "347", "929", "516", "631", "914", "845", "585", "716", "315", "680", "607", "518", "838"],
    "NCA": ["415", "628", "510", "341", "925", "669", "408", "650", "209", "559", "916", "279", "530", "707", "369"],
    "SCA": ["213", "323", "747", "818", "310", "424", "562", "626", "661", "714", "657", "949", "805", "619", "858", "760", "442", "951", "909"],
    "NJ":  ["201", "551", "732", "848", "609", "640", "856", "862", "973", "908"],
    "TX":  ["214", "469", "972", "945", "832", "713", "281", "346", "210", "726", "512", "737", "254", "325", "361", "409", "430", "432", "806", "830", "903", "915", "936", "940", "956", "979"],
    "CT":  ["203", "475", "860", "959"],
    "MD":  ["240", "301", "410", "443", "667"],
    "LI":  ["516", "631"],
    "PA":  ["215"],
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    table = boto3.resource("dynamodb").Table(TABLE_NAME)

    scanned = []
    resp = table.scan()
    scanned.extend(resp["Items"])
    while "LastEvaluatedKey" in resp:
        resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        scanned.extend(resp["Items"])

    print(f"Scanned {len(scanned)} items from {TABLE_NAME}")

    missing_codes = {item["stateCode"] for item in scanned} - set(CANONICAL_PHONES)
    if missing_codes:
        print(f"ERROR: no canonical phone defined for state codes: {sorted(missing_codes)}")
        print("Add them to CANONICAL_PHONES/AREA_CODES before running — do not guess.")
        return 1

    updated = 0
    for item in scanned:
        code = item["stateCode"]
        location = item["location"]
        phone = CANONICAL_PHONES[code]
        codes = AREA_CODES[code]
        if args.dry_run:
            print(f"[dry-run] {location} ({code}) -> canonicalPhone={phone}, areaCodes={codes}")
            continue
        table.update_item(
            Key={"location": location},
            UpdateExpression="SET canonicalPhone = :p, areaCodes = :a",
            ExpressionAttributeValues={
                ":p": phone,
                ":a": set(codes),
            },
        )
        updated += 1

    if args.dry_run:
        print(f"[dry-run] would update {len(scanned)} items — no writes performed")
    else:
        print(f"Updated {updated} items")
    return 0


if __name__ == "__main__":
    sys.exit(main())
