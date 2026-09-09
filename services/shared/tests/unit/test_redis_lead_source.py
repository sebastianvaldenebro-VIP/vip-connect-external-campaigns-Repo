"""Tests for RedisLeadSource — the iterator that feeds FilterEvaluator during verify."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from vip_shared.infrastructure.persistence.redis_lead_source import (
    RedisLeadSource,
    _parse_record,
    _resolve_redis_password,
    build_from_env,
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
        {
            "id": "cust-abcd1234",
            "phone": "+1111",
            "available": "true",
            "location": "NJ - Newark",
        },
        {
            "id": "cust-deadbeef",
            "phone": "+2222",
            "available": False,
            "location": "FL - Miami",
        },
        {"id": "short", "phone": "+9999"},  # too-short id, should be dropped
        "{{ bad json",  # malformed, should be dropped
        {
            "id": "cust-xyzwuvs1",
            "phone": "+3333",
            "available": "1",
            "location": "TX - Austin",
        },
    ]
    return [p if isinstance(p, str) else json.dumps(p) for p in payloads]


def test_iter_records_drops_malformed_and_too_short_ids(items):
    source = RedisLeadSource(FakeRedis(items), team="BASIC_TEAM", chunk_size=100)
    records = list(source.iter_records())
    assert len(records) == 3
    assert [r["id"] for r in records] == [
        "cust-abcd1234",
        "cust-deadbeef",
        "cust-xyzwuvs1",
    ]


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


def test_is_ready_true_when_list_has_entries():
    source = RedisLeadSource(FakeRedis(["x"]), team="BASIC_TEAM")
    assert source.is_ready() is True


def test_is_ready_false_when_list_empty():
    source = RedisLeadSource(FakeRedis([]), team="BASIC_TEAM")
    assert source.is_ready() is False


def test_parse_record_returns_none_for_none_input():
    assert _parse_record(None) is None


def test_parse_record_returns_none_when_json_is_not_a_dict():
    assert _parse_record(json.dumps(["not", "a", "dict"])) is None


@pytest.mark.parametrize(
    "available_value,expected",
    [(1, "True"), (0, "False"), (1.0, "True"), (0.0, "False")],
)
def test_parse_record_normalizes_numeric_available(available_value, expected):
    payload = json.dumps({"id": "cust-abcd1234", "available": available_value})
    record = _parse_record(payload)
    assert record is not None
    assert record["available"] == expected


class TestResolveRedisPassword:
    def test_returns_env_password_directly(self, monkeypatch):
        monkeypatch.setenv("REDIS_PASS", "s3cr3t")
        monkeypatch.delenv("REDIS_PASSWORD_SECRET_ARN", raising=False)
        assert _resolve_redis_password() == "s3cr3t"

    def test_returns_none_when_neither_password_nor_arn_set(self, monkeypatch):
        from vip_shared.infrastructure.persistence import redis_lead_source as mod

        monkeypatch.delenv("REDIS_PASS", raising=False)
        monkeypatch.delenv("REDIS_PASSWORD_SECRET_ARN", raising=False)
        assert mod._resolve_redis_password() is None

    def test_fetches_password_from_secrets_manager(self, monkeypatch):
        from vip_shared.infrastructure.persistence import redis_lead_source as mod

        monkeypatch.delenv("REDIS_PASS", raising=False)
        monkeypatch.setenv(
            "REDIS_PASSWORD_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:1:secret:x"
        )
        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = {
            "SecretString": json.dumps({"password": "from-secret"})
        }
        with patch("boto3.client", return_value=mock_client):
            assert mod._resolve_redis_password() == "from-secret"

    def test_falls_back_to_raw_secret_string_when_no_password_key(self, monkeypatch):
        from vip_shared.infrastructure.persistence import redis_lead_source as mod

        monkeypatch.delenv("REDIS_PASS", raising=False)
        monkeypatch.setenv(
            "REDIS_PASSWORD_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:1:secret:x"
        )
        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = {
            "SecretString": json.dumps({"unrelated": "value"})
        }
        with patch("boto3.client", return_value=mock_client):
            result = mod._resolve_redis_password()
        assert result == json.dumps({"unrelated": "value"})


class TestBuildFromEnv:
    def test_uses_provided_redis_client_without_constructing_one(self, monkeypatch):
        monkeypatch.setenv("REDIS_HOST", "redis.internal")
        monkeypatch.setenv("TEAM", "BASIC_TEAM")
        fake_client = FakeRedis([])

        source = build_from_env(redis_client=fake_client)

        assert isinstance(source, RedisLeadSource)
        assert source._redis is fake_client

    def test_raises_when_redis_package_unavailable(self, monkeypatch):
        from vip_shared.infrastructure.persistence import redis_lead_source as mod

        monkeypatch.setenv("REDIS_HOST", "redis.internal")
        monkeypatch.setattr(mod, "_redis", None)

        with pytest.raises(RuntimeError, match="redis package not installed"):
            mod.build_from_env()

    def test_constructs_redis_client_from_env_when_none_provided(self, monkeypatch):
        from vip_shared.infrastructure.persistence import redis_lead_source as mod

        monkeypatch.setenv("REDIS_HOST", "redis.internal")
        monkeypatch.setenv("REDIS_PORT", "6380")
        monkeypatch.setenv("TEAM", "ARBITRATION_TEAM")
        monkeypatch.setenv("REDIS_PASS", "s3cr3t")

        fake_redis_instance = MagicMock()
        mock_redis_module = MagicMock()
        mock_redis_module.Redis.return_value = fake_redis_instance
        monkeypatch.setattr(mod, "_redis", mock_redis_module)

        source = mod.build_from_env()

        mock_redis_module.Redis.assert_called_once_with(
            host="redis.internal",
            port=6380,
            password="s3cr3t",
            decode_responses=True,
            socket_timeout=10,
            socket_connect_timeout=10,
            ssl=True,
        )
        assert source._redis is fake_redis_instance
