"""Tests for SMS-specific extensions in executor.py."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

# Minimal env before importing executor (which reads env at module level indirectly)
_ENV = {
    "SMS_CAMPAIGN_QUEUE_TABLE": "VipSmsCampaignQueue",
    "SMS_CAMPAIGN_RUNS_TABLE": "VipSmsCampaignRuns",
    "SMS_SENDER_FUNCTION_ARN": "arn:aws:lambda:us-east-1:123:function:vip-admin-sms-sender",
    "CONNECT_INSTANCE_ID": "test-instance",
    "AWS_DEFAULT_REGION": "us-east-1",
    "DAILY_PLAN_TABLE": "VipAdminPlans",
    "PLAN_RUNS_TABLE": "VipAdminPlanRuns",
    "PROGRESSIVE_CAMPAIGN_QUEUE_TABLE": "VipProgressiveCampaignQueue",
}

with patch.dict(os.environ, _ENV):
    with (
        patch("boto3.client"),
        patch("boto3.resource"),
        patch("redis.StrictRedis"),
    ):
        import executor  # noqa: E402


# ── _is_sms ───────────────────────────────────────────────────────────────────


def test_is_sms_true_for_sms_delivery_type():
    assert executor._is_sms({"deliveryType": "sms"}) is True


def test_is_sms_false_for_campaign():
    assert executor._is_sms({"deliveryType": "campaign"}) is False


def test_is_sms_false_for_branded():
    assert executor._is_sms({"deliveryType": "branded"}) is False


def test_is_sms_false_when_no_delivery_type():
    assert executor._is_sms({}) is False


def test_is_sms_false_for_journey():
    assert executor._is_sms({"deliveryType": "journey"}) is False


# ── _count_sms_queue ──────────────────────────────────────────────────────────


def test_count_sms_queue_returns_count():
    mock_table = MagicMock()
    mock_table.query.return_value = {"Count": 7}
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    with patch("executor.boto3") as mock_boto3:
        mock_boto3.resource.return_value = mock_resource
        count = executor._count_sms_queue("cmp-123")

    assert count == 7


def test_count_sms_queue_returns_zero_when_empty():
    mock_table = MagicMock()
    mock_table.query.return_value = {"Count": 0}
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    with patch("executor.boto3") as mock_boto3:
        mock_boto3.resource.return_value = mock_resource
        count = executor._count_sms_queue("cmp-999")

    assert count == 0


def test_count_sms_queue_returns_zero_when_count_missing():
    mock_table = MagicMock()
    mock_table.query.return_value = {}
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    with patch("executor.boto3") as mock_boto3:
        mock_boto3.resource.return_value = mock_resource
        count = executor._count_sms_queue("cmp-x")

    assert count == 0


# ── _invoke_sms_sender ────────────────────────────────────────────────────────


def test_invoke_sms_sender_calls_lambda_with_correct_function():
    mock_lambda = MagicMock()
    mock_lambda.invoke.return_value = {"StatusCode": 200}

    # _get_lambda_client() does a local `import boto3` — patch the cached client directly
    with patch.object(executor, "_lambda_client", mock_lambda):
        with patch.dict(
            os.environ,
            {"SMS_SENDER_FUNCTION_ARN": "arn:aws:lambda:us-east-1:123:function:vip-admin-sms-sender"},
        ):
            executor._invoke_sms_sender(campaignId="cmp-1", planId="plan-1", runId="run-1")

    mock_lambda.invoke.assert_called_once()
    kwargs = mock_lambda.invoke.call_args.kwargs
    assert kwargs["FunctionName"] == "arn:aws:lambda:us-east-1:123:function:vip-admin-sms-sender"
    assert kwargs["InvocationType"] == "RequestResponse"


# ── _stop_sms_campaign ────────────────────────────────────────────────────────


def test_stop_sms_campaign_updates_runs_to_aborted():
    mock_table = MagicMock()
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    cs = {
        "smsCampaignId": "cmp-abc",
        "_smsRunsPlanId": "plan-1",
        "_smsRunsSk": "run-1#cmp-abc",
        "exitReason": "aborted",
    }

    with patch("executor.boto3") as mock_boto3:
        mock_boto3.resource.return_value = mock_resource
        executor._stop_sms_campaign(cs)

    mock_table.update_item.assert_called()
    call_kwargs = mock_table.update_item.call_args.kwargs
    assert "ABORTED" in str(call_kwargs)
    # Direct key lookup — no scan
    assert call_kwargs["Key"]["planId"] == "plan-1"
    assert call_kwargs["Key"]["sk"] == "run-1#cmp-abc"


def test_stop_sms_campaign_no_campaign_id_is_noop():
    mock_resource = MagicMock()

    with patch("executor.boto3") as mock_boto3:
        mock_boto3.resource.return_value = mock_resource
        executor._stop_sms_campaign({})

    mock_resource.Table.assert_not_called()


def test_stop_sms_campaign_missing_run_key_is_noop():
    """If _smsRunsPlanId or _smsRunsSk missing, function is a no-op (safe degradation)."""
    mock_resource = MagicMock()

    cs = {"smsCampaignId": "cmp-1"}  # missing _smsRunsPlanId and _smsRunsSk

    with patch("executor.boto3") as mock_boto3:
        mock_boto3.resource.return_value = mock_resource
        executor._stop_sms_campaign(cs)

    mock_resource.Table.assert_not_called()


def test_stop_sms_campaign_exception_is_non_fatal():
    mock_table = MagicMock()
    mock_table.update_item.side_effect = Exception("DDB error")
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    cs = {
        "smsCampaignId": "cmp-1",
        "_smsRunsPlanId": "plan-1",
        "_smsRunsSk": "run-1#cmp-1",
    }

    # Should not raise
    with patch("executor.boto3") as mock_boto3:
        mock_boto3.resource.return_value = mock_resource
        executor._stop_sms_campaign(cs)


# ── _complete_sms_campaign ────────────────────────────────────────────────────


def test_complete_sms_campaign_updates_runs_to_completed():
    mock_table = MagicMock()
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    cs = {
        "smsCampaignId": "cmp-done",
        "_smsRunsPlanId": "plan-2",
        "_smsRunsSk": "run-2#cmp-done",
        "exitReason": "queue_drained",
    }

    with patch("executor.boto3") as mock_boto3:
        mock_boto3.resource.return_value = mock_resource
        executor._complete_sms_campaign(cs)

    mock_table.update_item.assert_called()
    call_kwargs = mock_table.update_item.call_args.kwargs
    assert "COMPLETED" in str(call_kwargs)
    assert call_kwargs["Key"]["planId"] == "plan-2"
    assert call_kwargs["Key"]["sk"] == "run-2#cmp-done"


def test_complete_sms_campaign_no_campaign_id_is_noop():
    mock_resource = MagicMock()

    with patch("executor.boto3") as mock_boto3:
        mock_boto3.resource.return_value = mock_resource
        executor._complete_sms_campaign({})

    mock_resource.Table.assert_not_called()


def test_complete_sms_campaign_exception_is_non_fatal():
    mock_table = MagicMock()
    mock_table.update_item.side_effect = Exception("DDB error")
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    cs = {
        "smsCampaignId": "cmp-1",
        "_smsRunsPlanId": "plan-1",
        "_smsRunsSk": "run-1#cmp-1",
    }

    # Should not raise
    with patch("executor.boto3") as mock_boto3:
        mock_boto3.resource.return_value = mock_resource
        executor._complete_sms_campaign(cs)


# ── SMS does not warm up (skip warmup for SMS) ────────────────────────────────


def test_is_branded_or_is_sms_both_return_true_for_sms():
    """SMS campaigns should be treated like branded for warmup skipping."""
    campaign_sms = {"deliveryType": "sms"}
    assert executor._is_branded(campaign_sms) or executor._is_sms(campaign_sms)


# ── Regression: existing discriminators unaffected ───────────────────────────


def test_is_branded_false_for_sms():
    assert executor._is_branded({"deliveryType": "sms"}) is False


def test_is_branded_true_for_branded():
    assert executor._is_branded({"deliveryType": "branded"}) is True


def test_is_sms_true_only_for_sms():
    for dt in ("campaign", "journey", "branded", "", None):
        assert executor._is_sms({"deliveryType": dt}) is False
    assert executor._is_sms({"deliveryType": "sms"}) is True
