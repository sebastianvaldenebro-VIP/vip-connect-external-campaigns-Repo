"""Tests for the phone-lookup handler (proxies connectcampaignRedisAuxiliar)."""

from __future__ import annotations

import io
import json
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError


def _event(qs=None):
    return {
        "requestContext": {
            "authorizer": {"jwt": {"claims": {"sub": "u", "email": "u@x"}}}
        },
        "queryStringParameters": qs or {},
    }


def _payload_stream(obj: dict) -> io.BytesIO:
    return io.BytesIO(json.dumps(obj).encode())


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv(
        "REDIS_AUXILIAR_LOOKUP_ARN",
        "arn:aws:lambda:us-east-1:165505826690:function:connectcampaignRedisAuxiliar",
    )


def test_phone_lookup_requires_phone_param():
    from handlers import phone_lookup

    with pytest.raises(ValueError, match="phone"):
        phone_lookup.phone_lookup(_event(qs={}), {})


def test_phone_lookup_returns_upstream_response_verbatim():
    from handlers import phone_lookup

    upstream_result = {
        "phone_searched": "+15623027188",
        "redis": {
            "list_key": "wait_list:BASIC_TEAM:list",
            "total_scanned": 50244,
            "match_count": 0,
            "matches": [],
        },
        "profiles": {
            "domain": "amazon-connect-vipmedicalgroup",
            "key_tried": "_phone",
            "matches": [],
            "errors": {},
        },
        "cross_check": {
            "in_redis": False,
            "in_profiles": False,
            "lead_ids_redis": [],
            "profile_ids": [],
            "orphan_in_profiles": False,
            "pending_ingest_in_redis": False,
        },
    }
    mock_client = MagicMock()
    mock_client.invoke.return_value = {"Payload": _payload_stream(upstream_result)}

    with patch("handlers.phone_lookup._get_lambda_client", return_value=mock_client):
        response = phone_lookup.phone_lookup(_event(qs={"phone": "5623027188"}), {})

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == upstream_result
    mock_client.invoke.assert_called_once_with(
        FunctionName="arn:aws:lambda:us-east-1:165505826690:function:connectcampaignRedisAuxiliar",
        InvocationType="RequestResponse",
        Payload=json.dumps({"phone": "5623027188"}).encode(),
    )


def test_phone_lookup_returns_503_when_env_var_missing(monkeypatch):
    from handlers import phone_lookup

    monkeypatch.delenv("REDIS_AUXILIAR_LOOKUP_ARN", raising=False)

    response = phone_lookup.phone_lookup(_event(qs={"phone": "5623027188"}), {})

    assert response["statusCode"] == 503
    assert json.loads(response["body"])["error"]["code"] == "PHONE_LOOKUP_UNAVAILABLE"


def test_phone_lookup_returns_503_on_client_error():
    from handlers import phone_lookup

    mock_client = MagicMock()
    mock_client.invoke.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "boom"}}, "Invoke"
    )

    with patch("handlers.phone_lookup._get_lambda_client", return_value=mock_client):
        response = phone_lookup.phone_lookup(_event(qs={"phone": "5623027188"}), {})

    assert response["statusCode"] == 503
    assert json.loads(response["body"])["error"]["code"] == "PHONE_LOOKUP_INVOKE_FAILED"


def test_phone_lookup_returns_503_on_function_error():
    from handlers import phone_lookup

    mock_client = MagicMock()
    mock_client.invoke.return_value = {
        "FunctionError": "Unhandled",
        "Payload": _payload_stream({"errorMessage": "boom"}),
    }

    with patch("handlers.phone_lookup._get_lambda_client", return_value=mock_client):
        response = phone_lookup.phone_lookup(_event(qs={"phone": "5623027188"}), {})

    assert response["statusCode"] == 503
    assert json.loads(response["body"])["error"]["code"] == "PHONE_LOOKUP_INVOKE_FAILED"


def test_phone_lookup_returns_503_on_unparseable_payload():
    from handlers import phone_lookup

    mock_client = MagicMock()
    mock_client.invoke.return_value = {"Payload": io.BytesIO(b"not json")}

    with patch("handlers.phone_lookup._get_lambda_client", return_value=mock_client):
        response = phone_lookup.phone_lookup(_event(qs={"phone": "5623027188"}), {})

    assert response["statusCode"] == 503
    assert (
        json.loads(response["body"])["error"]["code"] == "PHONE_LOOKUP_INVALID_RESPONSE"
    )


def test_phone_lookup_returns_503_when_payload_is_not_a_dict():
    from handlers import phone_lookup

    mock_client = MagicMock()
    mock_client.invoke.return_value = {"Payload": _payload_stream([])}

    with patch("handlers.phone_lookup._get_lambda_client", return_value=mock_client):
        response = phone_lookup.phone_lookup(_event(qs={"phone": "5623027188"}), {})

    assert response["statusCode"] == 503
    assert (
        json.loads(response["body"])["error"]["code"] == "PHONE_LOOKUP_INVALID_RESPONSE"
    )
