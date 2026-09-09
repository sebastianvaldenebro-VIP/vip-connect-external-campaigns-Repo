"""Tests for the api-plans Lambda entrypoint — dispatches EventBridge tick/
scheduled_run/chain_trigger/prestart_check/janitor actions AND HTTP API routes.

Real vip_shared isn't installed/importable in this test env, and depending on
collection order some other test file (test_contacts_handler.py) may have
already stubbed vip_shared.application.http as its own mock. We install a
*blind* MagicMock only via sys.modules.setdefault (never a bare assignment).

Critically, we also stub out `router` and `executor` (via setdefault) BEFORE
importing `handler` — every test below patches handler.resolve/handler.executor
directly anyway, so we never need the real modules here, and NOT stubbing
them would force a real cascading import of router -> handlers.plans/
handlers.runs/handlers.contacts/handlers.sms -> store/scheduler_manager/
builders the first time this file is collected. That cascade would then
permanently cache real, un-stubbed versions of those modules in sys.modules,
which breaks OTHER test files (e.g. test_handlers_plans.py) that expect to
import handlers.plans fresh with store/scheduler_manager mocked out — Python
does not re-run a module body on a second `import`, so whichever test file
collects first and forces the real import "wins" for the rest of the session.
`store` itself is still imported for real below (needed for
ConcurrentWriteError) — it has no handlers.*/vip_shared dependencies of its
own, so it cannot poison anything.
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

sys.modules.setdefault("vip_shared", MagicMock())
sys.modules.setdefault("vip_shared.application", MagicMock())
sys.modules.setdefault("vip_shared.application.http", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure.telemetry", MagicMock())
sys.modules.setdefault(
    "vip_shared.infrastructure.telemetry.structured_logger", MagicMock()
)
sys.modules.setdefault("router", MagicMock())
sys.modules.setdefault("executor", MagicMock())

import handler  # noqa: E402, F401


def _json_response(status, body, **_kw):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }


def _error_response(status, code, message, details=None, request_id=None):
    payload = {"error": {"code": code, "message": message}}
    if details:
        payload["error"]["details"] = details
    if request_id:
        payload["error"]["requestId"] = request_id
    return _json_response(status, payload)


def _context(request_id: str = "req-1"):
    ctx = MagicMock()
    ctx.aws_request_id = request_id
    return ctx


class TestTickAction:
    def test_tick_success_returns_executor_result(self):

        with patch("handler.executor") as mock_executor:
            mock_executor.tick.return_value = {"ok": True}
            result = handler.lambda_handler(
                {"action": "tick", "planId": "p1", "runId": "r1", "bucketIndex": 2},
                _context(),
            )

        assert result == {"ok": True}
        mock_executor.tick.assert_called_once_with("p1", "r1", 2)

    def test_tick_concurrent_write_is_swallowed(self):
        from store import ConcurrentWriteError

        with patch("handler.executor") as mock_executor:
            mock_executor.tick.side_effect = ConcurrentWriteError("busy")
            result = handler.lambda_handler(
                {"action": "tick", "planId": "p1", "runId": "r1"}, _context()
            )

        assert result == {"ok": True, "reason": "concurrent_write"}

    def test_tick_unhandled_exception_notifies_sns_and_returns_error(self):

        with patch("handler.executor") as mock_executor:
            mock_executor.tick.side_effect = RuntimeError("boom")
            result = handler.lambda_handler(
                {"action": "tick", "planId": "p1", "runId": "r1", "bucketIndex": 3},
                _context(),
            )

        assert result == {"ok": False, "error": "boom"}
        mock_executor._notify_sns.assert_called_once()
        call_kwargs = mock_executor._notify_sns.call_args.kwargs
        assert call_kwargs["attributes"]["alertType"] == "tick_unhandled_error"
        assert call_kwargs["attributes"]["planId"] == "p1"

    def test_tick_defaults_bucket_index_to_zero(self):

        with patch("handler.executor") as mock_executor:
            mock_executor.tick.return_value = {"ok": True}
            handler.lambda_handler({"action": "tick", "planId": "p1", "runId": "r1"}, _context())

        mock_executor.tick.assert_called_once_with("p1", "r1", 0)


class TestScheduledRunAction:
    def test_scheduled_run_success(self):

        with patch("handler.executor") as mock_executor:
            mock_executor.scheduled_run.return_value = {"ok": True, "started": True}
            result = handler.lambda_handler(
                {"action": "scheduled_run", "planId": "p1"}, _context()
            )

        assert result == {"ok": True, "started": True}
        mock_executor.scheduled_run.assert_called_once_with("p1")

    def test_scheduled_run_error_returns_ok_false(self):

        with patch("handler.executor") as mock_executor:
            mock_executor.scheduled_run.side_effect = RuntimeError("db down")
            result = handler.lambda_handler(
                {"action": "scheduled_run", "planId": "p1"}, _context()
            )

        assert result == {"ok": False, "error": "db down"}


class TestChainTriggerAction:
    def test_chain_trigger_success(self):

        with patch("handler.executor") as mock_executor:
            result = handler.lambda_handler(
                {"action": "chain_trigger", "planId": "p1"}, _context()
            )

        assert result == {"ok": True}
        mock_executor.start_run_chained.assert_called_once_with("p1")

    def test_chain_trigger_error_returns_ok_false(self):

        with patch("handler.executor") as mock_executor:
            mock_executor.start_run_chained.side_effect = RuntimeError("chain failed")
            result = handler.lambda_handler(
                {"action": "chain_trigger", "planId": "p1"}, _context()
            )

        assert result == {"ok": False, "error": "chain failed"}


class TestPrestartCheckAction:
    def test_prestart_check_success_merges_result(self):

        with patch("handler.executor") as mock_executor:
            mock_executor.prestart_check.return_value = {"warmed": ["p1", "p2"]}
            result = handler.lambda_handler({"action": "prestart_check"}, _context())

        assert result == {"ok": True, "warmed": ["p1", "p2"]}

    def test_prestart_check_error_returns_ok_false(self):

        with patch("handler.executor") as mock_executor:
            mock_executor.prestart_check.side_effect = RuntimeError("boom")
            result = handler.lambda_handler({"action": "prestart_check"}, _context())

        assert result == {"ok": False, "error": "boom"}


class TestJanitorAction:
    def test_janitor_success_merges_result(self):

        with patch("handler.executor") as mock_executor:
            mock_executor.janitor_cleanup_orphan_schedules.return_value = {
                "deleted": ["sched-1"]
            }
            result = handler.lambda_handler({"action": "janitor"}, _context())

        assert result == {"ok": True, "deleted": ["sched-1"]}

    def test_janitor_error_returns_ok_false(self):

        with patch("handler.executor") as mock_executor:
            mock_executor.janitor_cleanup_orphan_schedules.side_effect = RuntimeError("boom")
            result = handler.lambda_handler({"action": "janitor"}, _context())

        assert result == {"ok": False, "error": "boom"}


class TestHttpRouting:
    def _event(self, route_key: str, path_params: dict | None = None):
        return {
            "routeKey": route_key,
            "pathParameters": path_params or {},
            "requestContext": {
                "authorizer": {"jwt": {"claims": {"sub": "u", "email": "u@example.com"}}}
            },
        }

    def test_dispatches_to_resolved_route(self):

        fake_handler = MagicMock(return_value={"statusCode": 200, "body": "{}"})
        with patch("handler.resolve", return_value=fake_handler):
            response = handler.lambda_handler(self._event("GET /plans"), _context())

        assert response["statusCode"] == 200

    def test_reads_route_key_from_request_context_when_missing_top_level(self):

        fake_handler = MagicMock(return_value={"statusCode": 200, "body": "{}"})
        event = {"requestContext": {"routeKey": "GET /plans"}, "pathParameters": {}}
        with patch("handler.resolve", return_value=fake_handler) as mock_resolve:
            handler.lambda_handler(event, _context())

        mock_resolve.assert_called_once_with("GET /plans")

    def test_returns_404_when_no_route_matches(self):

        with (
            patch("handler.resolve", return_value=None),
            patch("handler.error_response", side_effect=_error_response),
        ):
            response = handler.lambda_handler(self._event("GET /nope"), _context())

        body = json.loads(response["body"])
        assert response["statusCode"] == 404
        assert body["error"]["code"] == "ROUTE_NOT_FOUND"

    def test_maps_value_error_to_400(self):

        fake_handler = MagicMock(side_effect=ValueError("bad input"))
        with (
            patch("handler.resolve", return_value=fake_handler),
            patch("handler.error_response", side_effect=_error_response),
        ):
            response = handler.lambda_handler(self._event("GET /plans"), _context())

        body = json.loads(response["body"])
        assert response["statusCode"] == 400
        assert body["error"]["code"] == "VALIDATION_ERROR"

    def test_maps_concurrent_write_error_to_409(self):
        from store import ConcurrentWriteError

        fake_handler = MagicMock(side_effect=ConcurrentWriteError("busy"))
        with (
            patch("handler.resolve", return_value=fake_handler),
            patch("handler.error_response", side_effect=_error_response),
        ):
            response = handler.lambda_handler(self._event("GET /plans"), _context())

        body = json.loads(response["body"])
        assert response["statusCode"] == 409
        assert body["error"]["code"] == "CONCURRENT_WRITE"

    @pytest.mark.parametrize(
        "aws_code,expected_status",
        [
            ("ResourceNotFoundException", 404),
            ("ValidationException", 400),
            ("AccessDeniedException", 403),
            ("ConflictException", 409),
            ("ThrottlingException", 429),
            ("BadRequestException", 400),
            ("SomeUnmappedException", 500),
        ],
    )
    def test_maps_client_error_codes_to_status(self, aws_code, expected_status):

        error = ClientError(
            error_response={"Error": {"Code": aws_code, "Message": "internal detail"}},
            operation_name="GetItem",
        )
        fake_handler = MagicMock(side_effect=error)
        with (
            patch("handler.resolve", return_value=fake_handler),
            patch("handler.error_response", side_effect=_error_response),
        ):
            response = handler.lambda_handler(self._event("GET /plans"), _context())

        body = json.loads(response["body"])
        assert response["statusCode"] == expected_status
        assert body["error"]["code"] == aws_code
        assert "internal detail" not in body["error"]["message"]

    def test_maps_unhandled_exception_to_500_with_request_id(self):

        fake_handler = MagicMock(side_effect=RuntimeError("boom"))
        with (
            patch("handler.resolve", return_value=fake_handler),
            patch("handler.error_response", side_effect=_error_response),
        ):
            response = handler.lambda_handler(self._event("GET /plans"), _context("req-9"))

        body = json.loads(response["body"])
        assert response["statusCode"] == 500
        assert body["error"]["code"] == "INTERNAL_ERROR"
        assert body["error"]["requestId"] == "req-9"

    def test_unhandled_exception_without_context_omits_request_id(self):

        fake_handler = MagicMock(side_effect=RuntimeError("boom"))
        with (
            patch("handler.resolve", return_value=fake_handler),
            patch("handler.error_response", side_effect=_error_response),
        ):
            response = handler.lambda_handler(self._event("GET /plans"), None)

        body = json.loads(response["body"])
        assert response["statusCode"] == 500
        assert "requestId" not in body["error"]
