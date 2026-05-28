"""Iterate leads out of the Redis wait_list used by the ingest pipeline.

The wait_list key lives at ``wait_list:{team}:list`` as a Redis list of
JSON-encoded lead payloads. We read in chunks via LRANGE so that a large list
(tens of thousands of entries) doesn't blow the Lambda memory budget.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any

try:  # redis is optional at import time so unit tests can patch it.
    import redis as _redis
except ImportError:  # pragma: no cover - only hit when running outside Lambda
    _redis = None  # type: ignore[assignment]

LIST_KEY_TEMPLATE = "wait_list:{team}:list"
DEFAULT_CHUNK = 5_000


class RedisLeadSource:
    """Reads JSON-encoded leads from ``wait_list:{team}:list``.

    Each yielded dict is the raw payload from Redis, augmented with normalised
    ``customerid``/``available`` fields so callers can pass it straight to
    ``FilterEvaluator``.
    """

    def __init__(
        self,
        redis_client: Any,
        team: str,
        chunk_size: int = DEFAULT_CHUNK,
    ) -> None:
        self._redis = redis_client
        self._list_key = LIST_KEY_TEMPLATE.format(team=team)
        self._chunk_size = chunk_size

    def length(self) -> int:
        return int(self._redis.llen(self._list_key))

    def is_ready(self) -> bool:
        """True if the lead list has at least one record (i.e. not mid-rebuild)."""
        return self.length() > 0

    def iter_records(self) -> Iterator[dict[str, Any]]:
        """Yield every lead in the list once, skipping malformed entries."""
        total = self.length()
        for start in range(0, total, self._chunk_size):
            end = start + self._chunk_size - 1
            items = self._redis.lrange(self._list_key, start, end)
            for raw in items:
                record = _parse_record(raw)
                if record is not None:
                    yield record


def _parse_record(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        data = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    lead_id = str(data.get("id", "")).strip()
    if len(lead_id) < 8:
        # Matches MIN_LEAD_ID_LEN in the existing feeder — shorter values are
        # almost certainly corrupted payloads and would create ghost profiles.
        return None

    # Normalise identifiers so downstream filters match what the CP schema
    # uses. The object type maps `_source.id` → `_profile.Attributes.ID`
    # (uppercase), so UI filter `ID = ...` must be able to evaluate against
    # the Redis record locally too. `customerid` stays as an alias for
    # backward compatibility with older feeder code paths.
    record = dict(data)
    record.setdefault("customerid", lead_id)
    record.setdefault("ID", lead_id)
    # Normalise `available` to the same shape Customer Profiles stores it as —
    # the capitalised strings "True"/"False". The UI builds INCLUSIVE filters
    # with values=["True"]/["False"] (CP only accepts strings for
    # AttributeDimension), and FilterEvaluator's `IN` does direct equality —
    # if we left this as a Python bool the local Redis filter would always
    # mismatch and silently include/exclude everything regardless of selection.
    available = data.get("available")
    if isinstance(available, bool):
        record["available"] = "True" if available else "False"
    elif isinstance(available, str):
        record["available"] = (
            "True" if available.strip().lower() in {"true", "1", "yes"} else "False"
        )
    elif isinstance(available, (int, float)):
        record["available"] = "True" if available else "False"
    return record


def build_from_env(redis_client: Any | None = None) -> RedisLeadSource:
    host = os.environ["REDIS_HOST"]
    port = int(os.environ.get("REDIS_PORT", 6379))
    team = os.environ.get("TEAM", "BASIC_TEAM")
    password = os.environ.get("REDIS_PASS") or None

    if redis_client is None:
        if _redis is None:
            raise RuntimeError("redis package not installed")
        redis_client = _redis.Redis(
            host=host,
            port=port,
            password=password,
            decode_responses=True,
            socket_timeout=10,
            socket_connect_timeout=10,
        )
    return RedisLeadSource(redis_client=redis_client, team=team)
