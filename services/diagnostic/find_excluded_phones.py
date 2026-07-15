"""
Diagnostic: find leads in Redis whose phone can't be normalized to E.164.

These leads are silently excluded from campaign segments. Use this script
to identify them before re-running a campaign or cleaning up CRM data.

Usage (from Lambda env or local with VPN + env vars):

    # All leads with bad phones (no filter):
    python find_excluded_phones.py

    # Filter by state (same filter the executor uses):
    python find_excluded_phones.py --state NJ NY

    # Filter by available flag:
    python find_excluded_phones.py --available True

    # Filter by group:
    python find_excluded_phones.py --group 1 2 3

    # Find the bucket that ran campaign a3efed61 (DynamoDB lookup):
    python find_excluded_phones.py --find-campaign a3efed61

Required env vars (same as executor Lambda):
    REDIS_HOST, REDIS_PORT (default 6379), REDIS_PASS or REDIS_PASSWORD_SECRET_ARN
    TEAM  (e.g. BASIC_TEAM)
    AWS_REGION, AWS_PROFILE (for DynamoDB lookups)
    DYNAMODB_TABLE (default VipAdminPlans)
"""

from __future__ import annotations

import argparse
import json
import os
import sys


# ── Phone normalization (mirrors executor._normalize_phone_e164) ──────────────

def _normalize_phone_e164(raw: str) -> str | None:
    if not raw:
        return None
    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if raw.strip().startswith("+") and len(digits) >= 10:
        return raw.strip()
    return None


# ── Redis ─────────────────────────────────────────────────────────────────────

def _build_redis_client():
    import redis

    host = os.environ["REDIS_HOST"]
    port = int(os.environ.get("REDIS_PORT", 6379))
    password = os.environ.get("REDIS_PASS")
    if not password:
        arn = os.environ.get("REDIS_PASSWORD_SECRET_ARN")
        if arn:
            import boto3
            secret = boto3.client("secretsmanager").get_secret_value(SecretId=arn)
            payload = json.loads(secret["SecretString"])
            password = payload.get("password") or payload.get("REDIS_PASS") or secret["SecretString"]

    return redis.Redis(
        host=host,
        port=port,
        password=password,
        decode_responses=True,
        socket_timeout=15,
        ssl=True,
    )


def _iter_redis_leads(redis_client, team: str, chunk: int = 5_000):
    key = f"wait_list:{team}:list"
    total = int(redis_client.llen(key))
    print(f"[redis] key={key}  total_records={total}", file=sys.stderr)
    for start in range(0, total, chunk):
        items = redis_client.lrange(key, start, start + chunk - 1)
        for raw in items:
            try:
                record = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(record, dict):
                yield record


# ── DynamoDB: find which plan/bucket owned a Connect Campaign ID ──────────────

def find_campaign_in_dynamo(connect_campaign_id: str) -> None:
    import boto3
    table_name = os.environ.get("DYNAMODB_TABLE", "VipAdminPlans")
    ddb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    table = ddb.Table(table_name)

    print(f"\n[dynamo] Scanning {table_name} for connectCampaignId={connect_campaign_id!r}...")
    response = table.scan(
        FilterExpression="contains(#d, :cid)",
        ExpressionAttributeNames={"#d": "data"},
        ExpressionAttributeValues={":cid": connect_campaign_id},
    )
    items = response.get("Items", [])
    while "LastEvaluatedKey" in response:
        response = table.scan(
            FilterExpression="contains(#d, :cid)",
            ExpressionAttributeNames={"#d": "data"},
            ExpressionAttributeValues={":cid": connect_campaign_id},
            ExclusiveStartKey=response["LastEvaluatedKey"],
        )
        items.extend(response.get("Items", []))

    if not items:
        print(f"  No plan found with connectCampaignId={connect_campaign_id!r}")
        return

    for item in items:
        plan_id = item.get("planId") or item.get("PK") or item.get("id")
        data = item.get("data", {})
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                pass
        print(f"\n  plan_id : {plan_id}")
        print(f"  name    : {data.get('name') if isinstance(data, dict) else '?'}")
        # Find the specific bucket
        buckets = data.get("buckets", []) if isinstance(data, dict) else []
        for b in buckets:
            warmup = b.get("warmupData") or {}
            campaigns = warmup.get("campaigns", [])
            for c in campaigns:
                if c.get("connectCampaignId") == connect_campaign_id:
                    print(f"  bucket  : {b.get('name') or b.get('id')}")
                    print(f"  filters : {json.dumps(b.get('segmentFilters', {}), indent=4)}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Find Redis leads with non-normalizable phones.")
    parser.add_argument("--state", nargs="*", default=[], help="Filter by state codes (e.g. NJ NY)")
    parser.add_argument("--available", choices=["True", "False"], default=None)
    parser.add_argument("--group", nargs="*", default=[], type=int, dest="groups")
    parser.add_argument("--find-campaign", metavar="CAMPAIGN_ID",
                        help="Scan DynamoDB to find which plan/bucket owns this Connect Campaign ID")
    parser.add_argument("--out", default="-", help="Output file (default: stdout)")
    args = parser.parse_args()

    if args.find_campaign:
        find_campaign_in_dynamo(args.find_campaign)

    team = os.environ.get("TEAM", "BASIC_TEAM")
    rc = _build_redis_client()

    out = open(args.out, "w") if args.out != "-" else sys.stdout  # noqa: WPS515
    print("cid,raw_phone,reason", file=out)

    excluded_count = 0
    total_matched = 0

    for record in _iter_redis_leads(rc, team):
        cid = str(record.get("customerid") or record.get("ID") or "").strip()
        if not cid:
            continue

        # Apply same filters as executor
        if args.state:
            loc = str(record.get("location", ""))
            if not any(s in loc for s in args.state):
                continue
        if args.available is not None:
            if str(record.get("available", "")) != args.available:
                continue
        if args.groups:
            rec_groups = record.get("groups", [])
            if not any(g in rec_groups for g in args.groups):
                continue

        total_matched += 1
        raw_phone = str(record.get("phone", "")).strip()
        normalized = _normalize_phone_e164(raw_phone)
        if normalized is None:
            reason = "empty" if not raw_phone else f"bad_format({len(''.join(c for c in raw_phone if c.isdigit()))} digits)"
            print(f"{cid},{raw_phone},{reason}", file=out)
            excluded_count += 1

    if out is not sys.stdout:
        out.close()

    print(
        f"\n[summary] matched={total_matched}  excluded={excluded_count}  "
        f"included={total_matched - excluded_count}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
