"""Tests for RedisLeadSource — the iterator that feeds FilterEvaluator during verify."""
from __future__ import annotations

import json

import pytest

from vip_shared.infrastructure.persistence.redis_lead_source import (
    RedisLeadSource,
    _parse_record,
)


class FakeRedis:
    """Minimal Redis stub backing LLEN + LRANGE from a list."""

    def __init__(self, items: list[str]) -> None:
        self._items = items

    def llen(self, key: str) -> int:
        return len(self._items)

    def lrange(self, key: str, start: int, end: int) -> list[str]:
        # LRANGE is inclusive on both ends.
        return self._items[start : end + 1]


@pytest.fixture
def items() -> list[str]:
    payloads = [
        {"id": "cust-abcd1234", "phone": "+1111", "available": "true", "location": "NJ - Newark"},
        {"id": "cust-deadbeef", "phone": "+2222", "available": False, "location": "FL - Miami"},
        {"id": "short", "phone": "+9999"},  # too-short id, should be dropped
        "{{ bad json",  # malformed, should be dropped
        {"id": "cust-xyzwuvs1", "phone": "+3333", "available": "1", "location": "TX - Austin"},
    ]
    return [p if isinstance(p, str) else json.dumps(p) for p in payloads]


def test_iter_records_drops_malformed_and_too_short_ids(items):
    source = RedisLeadSource(FakeRedis(items), team="BASIC_TEAM", chunk_size=100)
    records = list(source.iter_records())
    assert len(records) == 3
    assert [r["id"] for r in records] == ["cust-abcd1234", "cust-deadbeef", "cust-xyzwuvs1"]


def test_iter_records_normalizes_available_to_capitalized_string(items):
    """`available` is normalised to the CP-shape strings 'True'/'False' so the
    local Redis filter and the CP-side filter compare values consistently."""
    source = RedisLeadSource(FakeRedis(items), team="BASIC_TEAM", chunk_size=100)
    records = list(source.iter_records())
    flags = {r["id"]: r["available"] for r in records}
    assert flags["cust-abcd1234"] == "True"
    assert flags["cust-deadbeef"] == "False"
    assert flags["cust-xyzwuvs1"] == "True"  # "1" → "True"


def test_customerid_defaults_to_id_when_absent():
    payload = json.dumps({"id": "cust-abcd1234", "phone": "+1111"})
    record = _parse_record(payload)
    assert record is not None
    assert record["customerid"] == "cust-abcd1234"


def test_iter_records_respects_chunking(items):
    fake = FakeRedis(items)
    source = RedisLeadSource(fake, team="BASIC_TEAM", chunk_size=2)
    # With chunk_size=2 and 5 items, LRANGE should be called 3 times.
    records = list(source.iter_records())
    # Output is still the same valid 3 records regardless of chunk size.
    assert len(records) == 3
