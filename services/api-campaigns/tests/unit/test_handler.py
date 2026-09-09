"""Tests for the Lambda entrypoint's error mapping and request logging.

Audit finding #014: raw AWS ClientError messages must never reach the HTTP
client, and path_params must be logged by key only (values may be PHI-adjacent
identifiers such as runId/contactId).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

import handler


def _event(
    route_key: str = "GET /campaigns/{id}", path_params: dict | None = None
) -> dict:
    return {
        "routeKey": route_key,
        "pathParameters": path_params or {"id": "campaign-123"},
    }


def test_client_error_message_not_leaked_to_caller():
    exc = ClientError(
        {
            "Error": {
                "Code": "AccessDeniedException",
                "Message": "arn:aws:iam::123456789012:role/secret-role denied",
            }
        },
        "DescribeCampaign",
    )
    with patch("handler.resolve", return_value=MagicMock(side_effect=exc)):
        result = handler.lambda_handler(_event(), context=None)

    body = json.loads(result["body"])
    assert result["statusCode"] == 403
    assert "arn:aws:iam" not in result["body"]
    assert "secret-role" not in result["body"]
    assert body["error"]["code"] == "AccessDeniedException"


def test_client_error_still_logs_full_detail_server_side():
    exc = ClientError(
        {
            "Error": {
                "Code": "ValidationException",
                "Message": "detail with arn:aws:sqs:x",
            }
        },
        "Op",
    )
    with (
        patch("handler.resolve", return_value=MagicMock(side_effect=exc)),
        patch.object(handler, "_logger") as mock_logger,
    ):
        handler.lambda_handler(_event(), context=None)

    # Server-side log still gets the real AWS detail, correlated for support.
    _, kwargs = mock_logger.error.call_args
    assert kwargs["message"] == "detail with arn:aws:sqs:x"


def test_request_received_logs_path_param_keys_not_values():
    with (
        patch(
            "handler.resolve",
            return_value=lambda event, params: {"statusCode": 200, "body": "{}"},
        ),
        patch.object(handler, "_logger") as mock_logger,
    ):
        handler.lambda_handler(
            _event(path_params={"id": "sensitive-run-id-value"}), context=None
        )

    _, kwargs = mock_logger.info.call_args
    assert kwargs["path_param_keys"] == ["id"]
    assert "sensitive-run-id-value" not in str(kwargs)
