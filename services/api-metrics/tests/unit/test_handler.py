"""Tests for the api-metrics Lambda entrypoint (routing + error mapping)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError


def _event(route_key: str, path_params: dict | None = None):
    return {
        "routeKey": route_key,
        "pathParameters": path_params or {},
        "requestContext": {
            "authorizer": {"jwt": {"claims": {"sub": "u", "email": "u@example.com"}}}
        },
    }


def _context(request_id: str = "req-1"):
    ctx = MagicMock()
    ctx.aws_request_id = request_id
    return ctx


def test_lambda_handler_dispatches_to_resolved_route():
    import handler

    fake_handler = MagicMock(return_value={"statusCode": 200, "body": "{}"})

    with patch("handler.resolve", return_value=fake_handler):
        response = handler.lambda_handler(_event("GET /metrics/current"), _context())

    assert response["statusCode"] == 200
    fake_handler.assert_called_once()


def test_lambda_handler_reads_route_key_from_request_context_when_missing_top_level():
    import handler

    fake_handler = MagicMock(return_value={"statusCode": 200, "body": "{}"})
    event = {
        "requestContext": {"routeKey": "GET /metrics/current"},
        "pathParameters": {},
    }

    with patch("handler.resolve", return_value=fake_handler) as mock_resolve:
        handler.lambda_handler(event, _context())

    mock_resolve.assert_called_once_with("GET /metrics/current")


def test_lambda_handler_returns_404_when_no_route_matches():
    import handler

    with patch("handler.resolve", return_value=None):
        response = handler.lambda_handler(_event("GET /nope"), _context())

    body = json.loads(response["body"])
    assert response["statusCode"] == 404
    assert body["error"]["code"] == "ROUTE_NOT_FOUND"


def test_lambda_handler_maps_value_error_to_400():
    import handler

    fake_handler = MagicMock(side_effect=ValueError("bad input"))

    with patch("handler.resolve", return_value=fake_handler):
        response = handler.lambda_handler(_event("GET /metrics/current"), _context())

    body = json.loads(response["body"])
    assert response["statusCode"] == 400
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["message"] == "bad input"


@pytest.mark.parametrize(
    "aws_code,expected_status",
    [
        ("ResourceNotFoundException", 404),
        ("ValidationException", 400),
        ("AccessDeniedException", 403),
        ("ThrottlingException", 429),
        ("SomeUnmappedException", 500),
    ],
)
def test_lambda_handler_maps_client_error_codes_to_status(aws_code, expected_status):
    import handler

    error = ClientError(
        error_response={
            "Error": {"Code": aws_code, "Message": "internal detail, do not leak"}
        },
        operation_name="GetMetricData",
    )
    fake_handler = MagicMock(side_effect=error)

    with patch("handler.resolve", return_value=fake_handler):
        response = handler.lambda_handler(
            _event("GET /metrics/current"), _context("req-123")
        )

    body = json.loads(response["body"])
    assert response["statusCode"] == expected_status
    assert body["error"]["code"] == aws_code
    assert body["error"]["requestId"] == "req-123"
    assert "internal detail" not in body["error"]["message"]


def test_lambda_handler_client_error_without_context_omits_request_id():
    import handler

    error = ClientError(
        error_response={"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
        operation_name="GetMetricData",
    )
    fake_handler = MagicMock(side_effect=error)

    with patch("handler.resolve", return_value=fake_handler):
        response = handler.lambda_handler(_event("GET /metrics/current"), None)

    body = json.loads(response["body"])
    assert response["statusCode"] == 429
    assert "requestId" not in body["error"]


def test_lambda_handler_maps_unhandled_exception_to_500():
    import handler

    fake_handler = MagicMock(side_effect=RuntimeError("boom"))

    with patch("handler.resolve", return_value=fake_handler):
        response = handler.lambda_handler(
            _event("GET /metrics/current"), _context("req-9")
        )

    body = json.loads(response["body"])
    assert response["statusCode"] == 500
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert body["error"]["requestId"] == "req-9"


def test_lambda_handler_unhandled_exception_without_context_omits_request_id():
    import handler

    fake_handler = MagicMock(side_effect=RuntimeError("boom"))

    with patch("handler.resolve", return_value=fake_handler):
        response = handler.lambda_handler(_event("GET /metrics/current"), None)

    body = json.loads(response["body"])
    assert response["statusCode"] == 500
    assert "requestId" not in body["error"]
