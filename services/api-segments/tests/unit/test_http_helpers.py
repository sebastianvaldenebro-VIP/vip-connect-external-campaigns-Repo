"""Tests for shared HTTP helpers."""

from __future__ import annotations

import json

from vip_shared.application.http import (
    error_response,
    extract_caller,
    json_response,
    parse_body,
)


def test_extract_caller_from_api_gateway_v2_event():
    event = {
        "requestContext": {
            "authorizer": {
                "jwt": {
                    "claims": {
                        "sub": "abc-123",
                        "email": "sebastian.valdenebro@medwork.io",
                    }
                }
            },
            "http": {
                "sourceIp": "18.212.243.142",
                "userAgent": "Mozilla/5.0",
            },
        }
    }
    caller = extract_caller(event)
    assert caller.sub == "abc-123"
    assert caller.email == "sebastian.valdenebro@medwork.io"
    assert caller.ip_address == "18.212.243.142"
    assert caller.user_agent == "Mozilla/5.0"


def test_extract_caller_handles_missing_fields():
    caller = extract_caller({"requestContext": {}})
    assert caller.sub == "unknown"
    assert caller.email == "unknown"
    assert caller.ip_address is None


def test_json_response_shape():
    response = json_response(200, {"ok": True})
    assert response["statusCode"] == 200
    assert response["headers"]["Content-Type"] == "application/json"
    assert json.loads(response["body"]) == {"ok": True}


def test_error_response_with_details():
    response = error_response(
        400, "VALIDATION_ERROR", "Missing name", details={"field": "name"}
    )
    body = json.loads(response["body"])
    assert response["statusCode"] == 400
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["details"] == {"field": "name"}


def test_parse_body_returns_dict():
    event = {"body": '{"name": "test"}'}
    assert parse_body(event) == {"name": "test"}


def test_parse_body_handles_empty():
    assert parse_body({}) == {}
    assert parse_body({"body": None}) == {}


def test_parse_body_raises_on_invalid_json():
    import pytest

    with pytest.raises(ValueError, match="Invalid JSON"):
        parse_body({"body": "not json"})
