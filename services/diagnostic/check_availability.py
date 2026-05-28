"""Diagnostic Lambda — check available status for given customer IDs in Redis and CP.

Invoke with:
  {"ids": ["id1", "id2", ...]}

Returns per-ID comparison: redis_available, cp_available, in_sync.
"""

from __future__ import annotations

import json
import os
from typing import Any

import boto3
import redis as _redis


# ── Config from env (same vars as api-plans) ─────────────────────────────────

REDIS_HOST = os.environ["REDIS_HOST"]
REDIS_PORT = int(os.environ.get("REDIS_PORT", 6379))
REDIS_PASS = os.environ.get("REDIS_PASS") or None
TEAM = os.environ.get("TEAM", "BASIC_TEAM")
PROFILES_DOMAIN = os.environ["PROFILES_DOMAIN_NAME"]

LIST_KEY = f"wait_list:{TEAM}:list"
CHUNK = 5_000


# ── Redis lookup ──────────────────────────────────────────────────────────────


def _load_redis_availability(ids: set[str]) -> dict[str, str | None]:
    """Scan Redis list and return {id: "True"/"False"/None} for each requested ID."""
    r = _redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        password=REDIS_PASS,
        decode_responses=True,
        socket_timeout=15,
        socket_connect_timeout=10,
    )
    result: dict[str, str | None] = {i: None for i in ids}
    remaining = set(ids)

    total = int(r.llen(LIST_KEY))
    for start in range(0, total, CHUNK):
        if not remaining:
            break
        items = r.lrange(LIST_KEY, start, start + CHUNK - 1)
        for raw in items:
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue
            lead_id = str(data.get("id", "")).strip()
            if lead_id not in remaining:
                continue
            avail_raw = data.get("available")
            if isinstance(avail_raw, bool):
                avail = "True" if avail_raw else "False"
            elif isinstance(avail_raw, str):
                avail = (
                    "True"
                    if avail_raw.strip().lower() in {"true", "1", "yes"}
                    else "False"
                )
            elif isinstance(avail_raw, (int, float)):
                avail = "True" if avail_raw else "False"
            else:
                avail = None
            result[lead_id] = avail
            remaining.discard(lead_id)

    return result


# ── CP lookup ─────────────────────────────────────────────────────────────────


def _load_cp_availability(ids: list[str]) -> dict[str, str | None]:
    """Fetch CP profiles by ProfileId (batch_get_profile) and return {id: "True"/"False"/None}.

    The Redis `id` field IS the CP ProfileId UUID — same value used in
    GetSegmentMembership and BatchGetProfile throughout the codebase.
    """
    cp = boto3.client("customer-profiles", region_name="us-east-1")
    result: dict[str, str | None] = {i: None for i in ids}

    # batch_get_profile supports up to 100 IDs per call
    BATCH = 100
    for i in range(0, len(ids), BATCH):
        batch = ids[i : i + BATCH]
        try:
            resp = cp.batch_get_profile(
                DomainName=PROFILES_DOMAIN,
                ProfileIds=batch,
            )
            for profile in resp.get("Profiles", []):
                pid = profile.get("ProfileId", "")
                if pid not in result:
                    continue
                attrs = profile.get("Attributes") or {}
                avail = attrs.get("available") or attrs.get("Available")
                result[pid] = str(avail) if avail is not None else None
            # Profiles not returned by CP → remain None (not found)
        except Exception as exc:
            for bid in batch:
                result[bid] = f"ERROR: {exc}"

    return result


# ── Handler ───────────────────────────────────────────────────────────────────


def lambda_handler(event: dict, _context: Any) -> dict:
    ids: list[str] = event.get("ids") or []
    if not ids:
        return {"error": "Provide 'ids' list in event payload"}

    ids = [str(i).strip() for i in ids if str(i).strip()]
    ids_set = set(ids)

    redis_avail = _load_redis_availability(ids_set)
    cp_avail = _load_cp_availability(ids)

    rows = []
    not_in_redis = 0
    not_in_cp = 0
    mismatch = 0
    available_false_redis = 0
    available_false_cp = 0

    for cid in ids:
        r_val = redis_avail.get(cid)
        c_val = cp_avail.get(cid)
        in_sync = r_val == c_val
        if r_val is None:
            not_in_redis += 1
        if c_val is None:
            not_in_cp += 1
        if r_val is not None and c_val is not None and not in_sync:
            mismatch += 1
        if r_val == "False":
            available_false_redis += 1
        if c_val == "False":
            available_false_cp += 1
        rows.append(
            {
                "id": cid,
                "redis_available": r_val,
                "cp_available": c_val,
                "in_sync": in_sync,
            }
        )

    return {
        "total": len(ids),
        "summary": {
            "available_false_in_redis": available_false_redis,
            "available_false_in_cp": available_false_cp,
            "not_found_in_redis": not_in_redis,
            "not_found_in_cp": not_in_cp,
            "mismatches_redis_vs_cp": mismatch,
        },
        "results": rows,
    }
