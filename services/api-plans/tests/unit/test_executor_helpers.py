"""Tests for small executor.py helpers not otherwise exercised: lazy boto3
client singletons, fire-and-forget metric emitters, branded/SMS queue count
helpers, get_branded_queue_counts/items, and _safe_expire_branded_queue.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

sys.modules.setdefault("vip_shared", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure.persistence", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure.persistence.audit", MagicMock())

import executor  # noqa: E402

# conftest.py's autouse `_neutralize_branded_metric` fixture patches
# executor._emit_branded_metric to a MagicMock for EVERY test in the suite
# (it's fire-and-forget telemetry no other test asserts on). Capture the real
# function here, at collection time — before that fixture ever runs — so
# TestEmitBrandedMetric can call the genuine implementation directly instead
# of silently testing the mock the fixture installed.
_real_emit_branded_metric = executor._emit_branded_metric


@pytest.fixture(autouse=True)
def _reset_client_singletons():
    executor._lambda_client = None
    executor._ddb_client_branded = None
    yield
    executor._lambda_client = None
    executor._ddb_client_branded = None


class TestGetLambdaClient:
    def test_constructs_and_caches_client(self):
        fake_client = MagicMock()
        with patch("boto3.client", return_value=fake_client) as mock_boto:
            first = executor._get_lambda_client()
            second = executor._get_lambda_client()
        mock_boto.assert_called_once_with("lambda")
        assert first is fake_client
        assert second is fake_client


class TestGetDdbClient:
    def test_constructs_and_caches_client(self):
        fake_client = MagicMock()
        with patch("boto3.client", return_value=fake_client) as mock_boto:
            first = executor._get_ddb_client()
            second = executor._get_ddb_client()
        mock_boto.assert_called_once_with("dynamodb")
        assert first is fake_client
        assert second is fake_client


class TestEmitBrandedMetric:
    def test_emits_metric_with_branded_dimension(self):
        mock_cw = MagicMock()
        with patch("boto3.client", return_value=mock_cw):
            _real_emit_branded_metric("SomeMetric", value=2.0)
        call_kwargs = mock_cw.put_metric_data.call_args.kwargs
        assert call_kwargs["Namespace"] == "VipConnect/ProgressiveDialer"
        assert call_kwargs["MetricData"][0]["Value"] == 2.0

    def test_swallows_cloudwatch_errors(self):
        with patch("boto3.client", side_effect=RuntimeError("CloudWatch down")):
            _real_emit_branded_metric("SomeMetric")  # must not raise


class TestEmitDispatchStalledMetric:
    def test_emits_metric_with_and_without_dimensions(self):
        mock_cw = MagicMock()
        with patch("executor.boto3.client", return_value=mock_cw):
            executor._emit_dispatch_stalled_metric("camp-1")
        call_kwargs = mock_cw.put_metric_data.call_args.kwargs
        assert call_kwargs["Namespace"] == "VIPPlans"
        dims = [m["Dimensions"] for m in call_kwargs["MetricData"]]
        assert [{"Name": "CampaignId", "Value": "camp-1"}] in dims
        assert [] in dims

    def test_swallows_cloudwatch_errors(self):
        with patch("executor.boto3.client", side_effect=RuntimeError("down")):
            executor._emit_dispatch_stalled_metric("camp-1")  # must not raise


class TestCountBrandedQueue:
    def test_returns_zero_when_queue_empty(self, monkeypatch):
        monkeypatch.setattr(executor, "_CAMPAIGN_QUEUE_TABLE_BRANDED", "VipProgressiveCampaignQueue")
        mock_ddb = MagicMock()
        mock_ddb.query.return_value = {"Count": 0}
        with patch.object(executor, "_get_ddb_client", return_value=mock_ddb):
            result = executor._count_branded_queue("bc-1")
        assert result == 0

    def test_returns_early_on_first_nonzero_page(self, monkeypatch):
        monkeypatch.setattr(executor, "_CAMPAIGN_QUEUE_TABLE_BRANDED", "VipProgressiveCampaignQueue")
        mock_ddb = MagicMock()
        mock_ddb.query.return_value = {"Count": 3, "LastEvaluatedKey": {"x": 1}}
        with patch.object(executor, "_get_ddb_client", return_value=mock_ddb):
            result = executor._count_branded_queue("bc-1")
        assert result == 3
        mock_ddb.query.assert_called_once()  # early exit, no second page fetched

    def test_paginates_when_all_pages_are_zero(self, monkeypatch):
        monkeypatch.setattr(executor, "_CAMPAIGN_QUEUE_TABLE_BRANDED", "VipProgressiveCampaignQueue")
        mock_ddb = MagicMock()
        mock_ddb.query.side_effect = [
            {"Count": 0, "LastEvaluatedKey": {"x": 1}},
            {"Count": 0},
        ]
        with patch.object(executor, "_get_ddb_client", return_value=mock_ddb):
            result = executor._count_branded_queue("bc-1")
        assert result == 0
        assert mock_ddb.query.call_count == 2


class TestCountSmsQueue:
    def test_returns_zero_when_queue_drained(self):
        mock_table = MagicMock()
        mock_table.query.return_value = {"Count": 0}
        mock_resource = MagicMock()
        mock_resource.Table.return_value = mock_table
        with patch("executor.boto3.resource", return_value=mock_resource):
            result = executor._count_sms_queue("sms-camp-1")
        assert result == 0

    def test_returns_nonzero_early(self):
        mock_table = MagicMock()
        mock_table.query.return_value = {"Count": 4}
        mock_resource = MagicMock()
        mock_resource.Table.return_value = mock_table
        with patch("executor.boto3.resource", return_value=mock_resource):
            result = executor._count_sms_queue("sms-camp-1")
        assert result == 4

    def test_paginates_when_all_pages_are_zero(self):
        mock_table = MagicMock()
        mock_table.query.side_effect = [
            {"Count": 0, "LastEvaluatedKey": {"x": 1}},
            {"Count": 0},
        ]
        mock_resource = MagicMock()
        mock_resource.Table.return_value = mock_table
        with patch("executor.boto3.resource", return_value=mock_resource):
            result = executor._count_sms_queue("sms-camp-1")
        assert result == 0
        assert mock_table.query.call_count == 2


class TestInvokeSmsSender:
    def test_invokes_lambda_with_json_payload(self, monkeypatch):
        monkeypatch.setenv("SMS_SENDER_FUNCTION_ARN", "arn:aws:lambda:us-east-1:123:function:sms-sender")
        fake_client = MagicMock()
        fake_client.invoke.return_value = {"FunctionError": None}
        with patch.object(executor, "_get_lambda_client", return_value=fake_client):
            executor._invoke_sms_sender(campaignId="c1")
        call_kwargs = fake_client.invoke.call_args.kwargs
        assert call_kwargs["FunctionName"] == "arn:aws:lambda:us-east-1:123:function:sms-sender"
        assert call_kwargs["InvocationType"] == "RequestResponse"

    def test_raises_on_function_error(self, monkeypatch):
        monkeypatch.setenv("SMS_SENDER_FUNCTION_ARN", "arn:aws:lambda:us-east-1:123:function:sms-sender")
        fake_client = MagicMock()
        fake_payload = MagicMock()
        fake_payload.read.return_value = b'{"errorMessage": "boom"}'
        fake_client.invoke.return_value = {"FunctionError": "Unhandled", "Payload": fake_payload}
        with patch.object(executor, "_get_lambda_client", return_value=fake_client):
            with pytest.raises(RuntimeError, match="SMS Sender Lambda error"):
                executor._invoke_sms_sender(campaignId="c1")


class TestGetBrandedQueueCounts:
    def test_returns_zero_zero_when_table_not_configured(self, monkeypatch):
        monkeypatch.setattr(executor, "_CAMPAIGN_QUEUE_TABLE_BRANDED", "")
        assert executor.get_branded_queue_counts("bc-1") == (0, 0)

    def test_returns_pending_and_dialed_counts(self, monkeypatch):
        monkeypatch.setattr(executor, "_CAMPAIGN_QUEUE_TABLE_BRANDED", "VipProgressiveCampaignQueue")
        mock_ddb = MagicMock()
        mock_ddb.query.side_effect = [
            {"Count": 5},  # pending/dispatching
            {"Count": 12},  # dialed
        ]
        with patch.object(executor, "_get_ddb_client", return_value=mock_ddb):
            pending, dialed = executor.get_branded_queue_counts("bc-1")
        assert (pending, dialed) == (5, 12)

    def test_paginates_each_filter_query(self, monkeypatch):
        monkeypatch.setattr(executor, "_CAMPAIGN_QUEUE_TABLE_BRANDED", "VipProgressiveCampaignQueue")
        mock_ddb = MagicMock()
        mock_ddb.query.side_effect = [
            {"Count": 2, "LastEvaluatedKey": {"x": 1}},
            {"Count": 3},
            {"Count": 1},
        ]
        with patch.object(executor, "_get_ddb_client", return_value=mock_ddb):
            pending, dialed = executor.get_branded_queue_counts("bc-1")
        assert pending == 5
        assert dialed == 1
        assert mock_ddb.query.call_count == 3


class TestGetBrandedQueueItems:
    def test_returns_empty_list_when_table_not_configured(self, monkeypatch):
        monkeypatch.setattr(executor, "_CAMPAIGN_QUEUE_TABLE_BRANDED", "")
        assert executor.get_branded_queue_items("bc-1") == []

    def test_returns_masked_phone_items_newest_first(self, monkeypatch):
        monkeypatch.setattr(executor, "_CAMPAIGN_QUEUE_TABLE_BRANDED", "VipProgressiveCampaignQueue")
        mock_ddb = MagicMock()
        mock_ddb.query.return_value = {
            "Items": [
                {
                    "sk": {"S": "2026-08-27T15:00:00.000Z#uuid-1"},
                    "phone": {"S": "+15551234567"},
                    "status": {"S": "PENDING"},
                }
            ]
        }
        with patch.object(executor, "_get_ddb_client", return_value=mock_ddb):
            items = executor.get_branded_queue_items("bc-1", limit=10)
        assert items == [
            {
                "phone_last4": "4567",
                "status": "PENDING",
                "seededAt": "2026-08-27T15:00:00.000Z",
            }
        ]
        call_kwargs = mock_ddb.query.call_args.kwargs
        assert call_kwargs["Limit"] == 10
        assert call_kwargs["ScanIndexForward"] is False

    def test_handles_short_phone_and_sk_without_hash(self, monkeypatch):
        monkeypatch.setattr(executor, "_CAMPAIGN_QUEUE_TABLE_BRANDED", "VipProgressiveCampaignQueue")
        mock_ddb = MagicMock()
        mock_ddb.query.return_value = {
            "Items": [
                {"sk": {"S": "no-hash-sk"}, "phone": {"S": "12"}, "status": {"S": "DIALED"}}
            ]
        }
        with patch.object(executor, "_get_ddb_client", return_value=mock_ddb):
            items = executor.get_branded_queue_items("bc-1")
        assert items[0]["phone_last4"] == "12"
        assert items[0]["seededAt"] == "no-hash-sk"


class TestExpireBrandedQueueItemsAdditional:
    def test_paginates_across_query_pages(self, monkeypatch):
        monkeypatch.setattr(executor, "_CAMPAIGN_QUEUE_TABLE_BRANDED", "VipProgressiveCampaignQueue")
        mock_ddb = MagicMock()
        mock_ddb.query.side_effect = [
            {
                "Items": [{"campaignId": {"S": "bc-1"}, "sk": {"S": "sk1"}}],
                "LastEvaluatedKey": {"campaignId": {"S": "bc-1"}, "sk": {"S": "sk1"}},
            },
            {"Items": []},
        ]
        mock_ddb.batch_write_item.return_value = {"UnprocessedItems": {}}
        with patch.object(executor, "_get_ddb_client", return_value=mock_ddb), patch("executor.time.sleep"):
            executor._expire_branded_queue_items("bc-1")
        assert mock_ddb.query.call_count == 2
        second_call_kwargs = mock_ddb.query.call_args_list[1].kwargs
        assert second_call_kwargs["ExclusiveStartKey"] == {
            "campaignId": {"S": "bc-1"}, "sk": {"S": "sk1"}
        }

    def test_logs_warning_when_items_still_unprocessed_after_retries(self, monkeypatch):
        monkeypatch.setattr(executor, "_CAMPAIGN_QUEUE_TABLE_BRANDED", "VipProgressiveCampaignQueue")
        mock_ddb = MagicMock()
        mock_ddb.query.return_value = {
            "Items": [{"campaignId": {"S": "bc-1"}, "sk": {"S": "sk1"}}]
        }
        # Every batch_write_item call reports the same item still unprocessed.
        mock_ddb.batch_write_item.return_value = {
            "UnprocessedItems": {
                "VipProgressiveCampaignQueue": [
                    {"PutRequest": {"Item": {"campaignId": {"S": "bc-1"}, "sk": {"S": "sk1"}}}}
                ]
            }
        }
        with patch.object(executor, "_get_ddb_client", return_value=mock_ddb), patch("executor.time.sleep"):
            executor._expire_branded_queue_items("bc-1")  # must not raise despite exhausting retries
        assert mock_ddb.batch_write_item.call_count == 3


class TestStopBrandedCampaignGenericClientError:
    def test_logs_but_does_not_raise_on_non_conditional_client_error(self):
        mock_ddb = MagicMock()
        mock_ddb.delete_item.side_effect = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "x"}},
            "DeleteItem",
        )
        cs = {
            "brandedCampaignId": "bc-1",
            "queueArn": "arn:aws:connect:us-east-1:123:instance/abc/queue/q1",
        }
        with (
            patch.object(executor, "_get_ddb_client", return_value=mock_ddb),
            patch.object(executor, "_expire_branded_queue_items"),
        ):
            executor._stop_branded_campaign(cs)  # must not raise


class TestWriteBrandedRunSummary:
    def _cs(self, **overrides):
        cs = {
            "campaignId": "camp-1",
            "brandedCampaignId": "bc-1",
            "startedAt": "2026-08-27T10:00:00+00:00",
            "completedAt": "2026-08-27T10:30:00+00:00",
            "exitReason": "queue_drained",
        }
        cs.update(overrides)
        return cs

    def test_skips_when_table_not_configured(self, monkeypatch):
        monkeypatch.setattr(executor, "_BRANDED_RUN_SUMMARY_TABLE", "")
        mock_ddb = MagicMock()
        with patch.object(executor, "_get_ddb_client", return_value=mock_ddb):
            executor._write_branded_run_summary("p1", "r1", self._cs())
        mock_ddb.update_item.assert_not_called()

    def test_skips_when_campaign_id_missing(self, monkeypatch):
        monkeypatch.setattr(executor, "_BRANDED_RUN_SUMMARY_TABLE", "VipBrandedRunSummary")
        mock_ddb = MagicMock()
        with patch.object(executor, "_get_ddb_client", return_value=mock_ddb):
            executor._write_branded_run_summary("p1", "r1", self._cs(campaignId=""))
        mock_ddb.update_item.assert_not_called()

    def test_writes_completed_status_on_queue_drained(self, monkeypatch):
        monkeypatch.setattr(executor, "_BRANDED_RUN_SUMMARY_TABLE", "VipBrandedRunSummary")
        mock_ddb = MagicMock()
        with (
            patch.object(executor, "_get_ddb_client", return_value=mock_ddb),
            patch.object(executor, "get_branded_queue_counts", return_value=(3, 7)),
        ):
            executor._write_branded_run_summary("p1", "r1", self._cs())

        call_kwargs = mock_ddb.update_item.call_args.kwargs
        values = call_kwargs["ExpressionAttributeValues"]
        assert values[":s"] == {"S": "COMPLETED"}
        assert values[":ts"] == {"N": "10"}
        assert values[":td"] == {"N": "7"}
        assert call_kwargs["Key"]["sk"] == {"S": "r1#camp-1"}

    @pytest.mark.parametrize(
        "exit_reason", ["aborted", "manually_stopped", "poll_failure", "expired"]
    )
    def test_writes_aborted_status_for_known_abort_reasons(self, monkeypatch, exit_reason):
        monkeypatch.setattr(executor, "_BRANDED_RUN_SUMMARY_TABLE", "VipBrandedRunSummary")
        mock_ddb = MagicMock()
        with (
            patch.object(executor, "_get_ddb_client", return_value=mock_ddb),
            patch.object(executor, "get_branded_queue_counts", return_value=(0, 0)),
        ):
            executor._write_branded_run_summary("p1", "r1", self._cs(exitReason=exit_reason))
        values = mock_ddb.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":s"] == {"S": "ABORTED"}

    def test_writes_error_status_for_unknown_exit_reason(self, monkeypatch):
        monkeypatch.setattr(executor, "_BRANDED_RUN_SUMMARY_TABLE", "VipBrandedRunSummary")
        mock_ddb = MagicMock()
        with (
            patch.object(executor, "_get_ddb_client", return_value=mock_ddb),
            patch.object(executor, "get_branded_queue_counts", return_value=(0, 0)),
        ):
            executor._write_branded_run_summary("p1", "r1", self._cs(exitReason="something_else"))
        values = mock_ddb.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":s"] == {"S": "ERROR"}

    def test_swallows_queue_count_errors_and_defaults_to_zero(self, monkeypatch):
        monkeypatch.setattr(executor, "_BRANDED_RUN_SUMMARY_TABLE", "VipBrandedRunSummary")
        mock_ddb = MagicMock()
        with (
            patch.object(executor, "_get_ddb_client", return_value=mock_ddb),
            patch.object(executor, "get_branded_queue_counts", side_effect=RuntimeError("DDB down")),
        ):
            executor._write_branded_run_summary("p1", "r1", self._cs())
        values = mock_ddb.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":ts"] == {"N": "0"}

    def test_computes_zero_duration_when_started_at_missing(self, monkeypatch):
        monkeypatch.setattr(executor, "_BRANDED_RUN_SUMMARY_TABLE", "VipBrandedRunSummary")
        mock_ddb = MagicMock()
        with (
            patch.object(executor, "_get_ddb_client", return_value=mock_ddb),
            patch.object(executor, "get_branded_queue_counts", return_value=(0, 0)),
        ):
            executor._write_branded_run_summary("p1", "r1", self._cs(startedAt=""))
        values = mock_ddb.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":ds"] == {"N": "0"}

    def test_handles_malformed_timestamps_gracefully(self, monkeypatch):
        monkeypatch.setattr(executor, "_BRANDED_RUN_SUMMARY_TABLE", "VipBrandedRunSummary")
        mock_ddb = MagicMock()
        with (
            patch.object(executor, "_get_ddb_client", return_value=mock_ddb),
            patch.object(executor, "get_branded_queue_counts", return_value=(0, 0)),
        ):
            executor._write_branded_run_summary(
                "p1", "r1", self._cs(startedAt="not-a-date", completedAt="also-not-a-date")
            )
        values = mock_ddb.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":ds"] == {"N": "0"}

    def test_swallows_dynamodb_write_errors(self, monkeypatch):
        monkeypatch.setattr(executor, "_BRANDED_RUN_SUMMARY_TABLE", "VipBrandedRunSummary")
        mock_ddb = MagicMock()
        mock_ddb.update_item.side_effect = RuntimeError("DDB unavailable")
        with (
            patch.object(executor, "_get_ddb_client", return_value=mock_ddb),
            patch.object(executor, "get_branded_queue_counts", return_value=(0, 0)),
        ):
            executor._write_branded_run_summary("p1", "r1", self._cs())  # must not raise


class TestWriteBrandedRunStart:
    def _run(self):
        return {"runId": "r1", "planSnapshot": {"name": "Plan One"}}

    def _cs(self, **overrides):
        cs = {"campaignId": "camp-1", "brandedCampaignId": "bc-1", "bucketIndex": 0, "priority": 0}
        cs.update(overrides)
        return cs

    def _cfg(self, **overrides):
        cfg = {
            "queueId": "q-1",
            "contactFlowId": "cf-1",
            "sourcePhone": "+15125550100",
            "dialerType": "progressive",
        }
        cfg.update(overrides)
        return cfg

    def test_skips_when_table_not_configured(self, monkeypatch):
        monkeypatch.setattr(executor, "_BRANDED_RUN_SUMMARY_TABLE", "")
        mock_ddb = MagicMock()
        with patch.object(executor, "_get_ddb_client", return_value=mock_ddb):
            executor._write_branded_run_start("p1", self._run(), self._cs(), self._cfg(), "seg-1", "arn:seg-1", 50)
        mock_ddb.put_item.assert_not_called()

    def test_skips_when_campaign_id_missing(self, monkeypatch):
        monkeypatch.setattr(executor, "_BRANDED_RUN_SUMMARY_TABLE", "VipBrandedRunSummary")
        mock_ddb = MagicMock()
        with patch.object(executor, "_get_ddb_client", return_value=mock_ddb):
            executor._write_branded_run_start(
                "p1", self._run(), self._cs(campaignId=""), self._cfg(), "seg-1", "arn:seg-1", 50
            )
        mock_ddb.put_item.assert_not_called()

    def test_writes_start_record_masking_phi_phone(self, monkeypatch):
        monkeypatch.setattr(executor, "_BRANDED_RUN_SUMMARY_TABLE", "VipBrandedRunSummary")
        mock_ddb = MagicMock()
        with patch.object(executor, "_get_ddb_client", return_value=mock_ddb):
            executor._write_branded_run_start(
                "p1", self._run(), self._cs(), self._cfg(), "seg-1", "arn:seg-1", 50
            )
        item = mock_ddb.put_item.call_args.kwargs["Item"]
        assert item["sourcePhoneLast4"] == {"S": "0100"}
        assert item["planName"] == {"S": "Plan One"}
        assert item["segmentSize"] == {"N": "50"}
        assert "sourcePhone" not in item["segmentDefinitionJson"]["S"]

    def test_uses_source_phone_number_fallback(self, monkeypatch):
        monkeypatch.setattr(executor, "_BRANDED_RUN_SUMMARY_TABLE", "VipBrandedRunSummary")
        mock_ddb = MagicMock()
        cfg = self._cfg()
        del cfg["sourcePhone"]
        cfg["sourcePhoneNumber"] = "+15125559999"
        with patch.object(executor, "_get_ddb_client", return_value=mock_ddb):
            executor._write_branded_run_start(
                "p1", self._run(), self._cs(), cfg, "seg-1", "arn:seg-1", 50
            )
        item = mock_ddb.put_item.call_args.kwargs["Item"]
        assert item["sourcePhoneLast4"] == {"S": "9999"}

    def test_ignores_conditional_check_failed(self, monkeypatch):
        monkeypatch.setattr(executor, "_BRANDED_RUN_SUMMARY_TABLE", "VipBrandedRunSummary")
        mock_ddb = MagicMock()
        mock_ddb.put_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
            "PutItem",
        )
        with patch.object(executor, "_get_ddb_client", return_value=mock_ddb):
            executor._write_branded_run_start(
                "p1", self._run(), self._cs(), self._cfg(), "seg-1", "arn:seg-1", 50
            )  # must not raise

    def test_logs_but_does_not_raise_on_other_client_error(self, monkeypatch):
        monkeypatch.setattr(executor, "_BRANDED_RUN_SUMMARY_TABLE", "VipBrandedRunSummary")
        mock_ddb = MagicMock()
        mock_ddb.put_item.side_effect = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "x"}},
            "PutItem",
        )
        with patch.object(executor, "_get_ddb_client", return_value=mock_ddb):
            executor._write_branded_run_start(
                "p1", self._run(), self._cs(), self._cfg(), "seg-1", "arn:seg-1", 50
            )  # must not raise

    def test_swallows_generic_exceptions(self, monkeypatch):
        monkeypatch.setattr(executor, "_BRANDED_RUN_SUMMARY_TABLE", "VipBrandedRunSummary")
        mock_ddb = MagicMock()
        mock_ddb.put_item.side_effect = RuntimeError("boom")
        with patch.object(executor, "_get_ddb_client", return_value=mock_ddb):
            executor._write_branded_run_start(
                "p1", self._run(), self._cs(), self._cfg(), "seg-1", "arn:seg-1", 50
            )  # must not raise


class TestSafeExpireBrandedQueue:
    def test_skips_when_table_not_configured(self, monkeypatch):
        monkeypatch.setattr(executor, "_CAMPAIGN_QUEUE_TABLE_BRANDED", "")
        with patch.object(executor, "_expire_branded_queue_items") as mock_expire:
            executor._safe_expire_branded_queue("bc-1", "test-context")
        mock_expire.assert_not_called()

    def test_calls_expire_when_configured(self, monkeypatch):
        monkeypatch.setattr(executor, "_CAMPAIGN_QUEUE_TABLE_BRANDED", "VipProgressiveCampaignQueue")
        with patch.object(executor, "_expire_branded_queue_items") as mock_expire:
            executor._safe_expire_branded_queue("bc-1", "test-context")
        mock_expire.assert_called_once_with("bc-1")

    def test_swallows_expire_errors(self, monkeypatch):
        monkeypatch.setattr(executor, "_CAMPAIGN_QUEUE_TABLE_BRANDED", "VipProgressiveCampaignQueue")
        with patch.object(executor, "_expire_branded_queue_items", side_effect=RuntimeError("boom")):
            executor._safe_expire_branded_queue("bc-1", "test-context")  # must not raise
