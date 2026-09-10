"""Tests for the run lifecycle handlers in handlers/runs.py not already
covered by test_runs_index_validation.py (force_start_bucket/force_start_campaign
happy paths, index validation) or test_branded_progress_handler.py
(branded_progress's main behavior): trigger_run, list_runs, get_run,
abort_run, force_finish_run, force_stop_bucket, force_stop_campaign,
skip_campaign's 404/success paths, apply_plan_snapshot, the remaining
branded_progress/branded_queue continue-branches, branded_history, and
_get_ddb.

Mirrors test_runs_index_validation.py's _load_runs_module() convention.
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

_stub_modules = {
    "store": MagicMock(),
    "executor": MagicMock(),
    "scheduler_manager": MagicMock(),
    "vip_shared": MagicMock(),
    "vip_shared.application": MagicMock(),
    "vip_shared.application.http": MagicMock(),
    "vip_shared.infrastructure": MagicMock(),
    "vip_shared.infrastructure.persistence": MagicMock(),
    "vip_shared.infrastructure.persistence.audit": MagicMock(),
}


def _make_run(bucket_count: int, campaign_counts: list[int] | None = None) -> dict:
    campaign_counts = campaign_counts or [0] * bucket_count
    return {
        "planId": "plan-1",
        "runId": "run-1",
        "status": "running",
        "bucketStates": [
            {"campaignStates": [{"campaignId": f"c{j}"} for j in range(n)]}
            for n in campaign_counts
        ],
    }


def _parse_body(event: dict) -> dict:
    raw = event.get("body") if event else None
    if raw is None:
        return {}
    return json.loads(raw) if isinstance(raw, str) else raw


def _load_runs_module():
    with patch.dict(sys.modules, _stub_modules):
        import importlib

        import handlers.runs as runs_mod

        importlib.reload(runs_mod)
        runs_mod.executor.reset_mock(return_value=True, side_effect=True)
        runs_mod.store.reset_mock(return_value=True, side_effect=True)
        runs_mod.build_audit.reset_mock(return_value=True, side_effect=True)
        runs_mod.json_response = lambda code, body: {"statusCode": code, "body": body}
        runs_mod.parse_body = _parse_body
        runs_mod.extract_caller = lambda event: MagicMock(
            sub="s", email="e", ip_address="1.2.3.4", user_agent="ua"
        )
        return runs_mod


class TestGetDdb:
    def test_constructs_and_caches_client(self):
        runs_mod = _load_runs_module()
        runs_mod._ddb_client = None
        fake_client = MagicMock()
        with patch("boto3.client", return_value=fake_client) as mock_boto:
            first = runs_mod._get_ddb()
            second = runs_mod._get_ddb()
        mock_boto.assert_called_once_with("dynamodb")
        assert first is fake_client
        assert second is fake_client


class TestTriggerRun:
    def test_starts_run_and_records_audit(self):
        runs_mod = _load_runs_module()
        runs_mod.store.get_plan.return_value = {"planId": "p1", "buckets": [{}, {}]}
        runs_mod.store.get_latest_run.return_value = None
        runs_mod.executor.start_run.return_value = {"runId": "r1", "planId": "p1"}

        response = runs_mod.trigger_run({"body": None}, {"id": "p1"})

        assert response["statusCode"] == 201
        runs_mod.executor.start_run.assert_called_once_with("p1", start_bucket_index=None)
        runs_mod.build_audit.return_value.record.assert_called_once()

    def test_rejects_non_integer_start_bucket_index(self):
        runs_mod = _load_runs_module()
        runs_mod.parse_body = lambda event: {"startBucketIndex": "not-a-number"}

        response = runs_mod.trigger_run({"body": None}, {"id": "p1"})

        body = response["body"]
        assert response["statusCode"] == 400
        assert body["error"]["code"] == "INVALID_INPUT"

    def test_returns_404_when_plan_missing(self):
        runs_mod = _load_runs_module()
        runs_mod.store.get_plan.return_value = None

        response = runs_mod.trigger_run({"body": None}, {"id": "missing"})
        assert response["statusCode"] == 404

    def test_rejects_start_bucket_index_out_of_range(self):
        runs_mod = _load_runs_module()
        runs_mod.parse_body = lambda event: {"startBucketIndex": 5}
        runs_mod.store.get_plan.return_value = {"planId": "p1", "buckets": [{}, {}]}

        response = runs_mod.trigger_run({"body": None}, {"id": "p1"})

        assert response["statusCode"] == 400
        assert response["body"]["error"]["code"] == "INVALID_INPUT"

    def test_rejects_when_run_already_active(self):
        runs_mod = _load_runs_module()
        runs_mod.store.get_plan.return_value = {"planId": "p1", "buckets": [{}]}
        runs_mod.store.get_latest_run.return_value = {"runId": "r0", "status": "running"}

        with pytest.raises(ValueError, match="already has an active run"):
            runs_mod.trigger_run({"body": None}, {"id": "p1"})

    def test_accepts_valid_start_bucket_index(self):
        runs_mod = _load_runs_module()
        runs_mod.parse_body = lambda event: {"startBucketIndex": 1}
        runs_mod.store.get_plan.return_value = {"planId": "p1", "buckets": [{}, {}]}
        runs_mod.store.get_latest_run.return_value = {"runId": "r0", "status": "completed"}
        runs_mod.executor.start_run.return_value = {"runId": "r1", "planId": "p1"}

        response = runs_mod.trigger_run({"body": None}, {"id": "p1"})

        assert response["statusCode"] == 201
        runs_mod.executor.start_run.assert_called_once_with("p1", start_bucket_index=1)


class TestListRuns:
    def test_returns_runs_for_plan(self):
        runs_mod = _load_runs_module()
        runs_mod.store.list_runs.return_value = [{"runId": "r1"}, {"runId": "r2"}]

        response = runs_mod.list_runs({}, {"id": "p1"})

        assert response["statusCode"] == 200
        assert response["body"]["runs"] == [{"runId": "r1"}, {"runId": "r2"}]
        runs_mod.store.list_runs.assert_called_once_with("p1")


class TestGetRun:
    def test_returns_404_when_missing(self):
        runs_mod = _load_runs_module()
        runs_mod.store.get_run.return_value = None
        response = runs_mod.get_run({}, {"id": "p1", "runId": "missing"})
        assert response["statusCode"] == 404

    def test_returns_run_when_found(self):
        runs_mod = _load_runs_module()
        runs_mod.store.get_run.return_value = {"runId": "r1", "status": "running"}
        response = runs_mod.get_run({}, {"id": "p1", "runId": "r1"})
        assert response["statusCode"] == 200
        assert response["body"]["runId"] == "r1"


class TestAbortRun:
    def test_aborts_and_records_audit(self):
        runs_mod = _load_runs_module()
        runs_mod.executor.abort_run.return_value = {"runId": "r1", "status": "aborted"}

        response = runs_mod.abort_run({}, {"id": "p1", "runId": "r1"})

        assert response["statusCode"] == 200
        runs_mod.executor.abort_run.assert_called_once_with("p1", "r1")
        audit_kwargs = runs_mod.build_audit.return_value.record.call_args.kwargs
        assert audit_kwargs["action"] == "abort"


class TestForceFinishRun:
    def test_force_finishes_and_records_audit(self):
        runs_mod = _load_runs_module()
        runs_mod.executor.force_finish_run.return_value = {"runId": "r1", "status": "completed"}

        response = runs_mod.force_finish_run({}, {"id": "p1", "runId": "r1"})

        assert response["statusCode"] == 200
        runs_mod.executor.force_finish_run.assert_called_once_with("p1", "r1")
        audit_kwargs = runs_mod.build_audit.return_value.record.call_args.kwargs
        assert audit_kwargs["action"] == "force_finish"


class TestForceStopBucket:
    def test_returns_404_when_run_missing(self):
        runs_mod = _load_runs_module()
        runs_mod.store.get_run.return_value = None
        response = runs_mod.force_stop_bucket(
            {}, {"id": "p1", "runId": "missing", "bucketIndex": "0"}
        )
        assert response["statusCode"] == 404

    def test_stops_bucket_and_records_audit(self):
        runs_mod = _load_runs_module()
        runs_mod.store.get_run.return_value = _make_run(2)
        runs_mod.executor.force_stop_bucket.return_value = {"runId": "r1"}

        response = runs_mod.force_stop_bucket(
            {}, {"id": "p1", "runId": "r1", "bucketIndex": "1"}
        )

        assert response["statusCode"] == 200
        runs_mod.executor.force_stop_bucket.assert_called_once_with("p1", "r1", 1)


class TestForceStartCampaign:
    def test_returns_404_when_run_missing(self):
        runs_mod = _load_runs_module()
        runs_mod.store.get_run.return_value = None
        response = runs_mod.force_start_campaign(
            {}, {"id": "p1", "runId": "missing", "bucketIndex": "0", "campaignIndex": "0"}
        )
        assert response["statusCode"] == 404


class TestForceStopCampaign:
    def test_returns_404_when_run_missing(self):
        runs_mod = _load_runs_module()
        runs_mod.store.get_run.return_value = None
        response = runs_mod.force_stop_campaign(
            {}, {"id": "p1", "runId": "missing", "bucketIndex": "0", "campaignIndex": "0"}
        )
        assert response["statusCode"] == 404

    def test_stops_campaign_and_records_audit(self):
        runs_mod = _load_runs_module()
        runs_mod.store.get_run.return_value = _make_run(2, campaign_counts=[1, 2])
        runs_mod.executor.force_stop_campaign.return_value = {"runId": "r1"}

        response = runs_mod.force_stop_campaign(
            {}, {"id": "p1", "runId": "r1", "bucketIndex": "1", "campaignIndex": "0"}
        )

        assert response["statusCode"] == 200
        runs_mod.executor.force_stop_campaign.assert_called_once_with("p1", "r1", 1, 0)


class TestSkipCampaign:
    def test_returns_404_when_run_missing(self):
        runs_mod = _load_runs_module()
        runs_mod.store.get_run.return_value = None
        response = runs_mod.skip_campaign(
            {}, {"id": "p1", "runId": "missing", "bucketIndex": "0", "campaignIndex": "0"}
        )
        assert response["statusCode"] == 404

    def test_skips_campaign_and_records_audit(self):
        runs_mod = _load_runs_module()
        runs_mod.store.get_run.return_value = _make_run(1, campaign_counts=[2])
        runs_mod.executor.skip_campaign.return_value = {"runId": "r1"}

        response = runs_mod.skip_campaign(
            {}, {"id": "p1", "runId": "r1", "bucketIndex": "0", "campaignIndex": "1"}
        )

        assert response["statusCode"] == 200
        runs_mod.executor.skip_campaign.assert_called_once_with("p1", "r1", 0, 1)
        audit_kwargs = runs_mod.build_audit.return_value.record.call_args.kwargs
        assert audit_kwargs["action"] == "skip_campaign"


class TestApplyPlanSnapshot:
    def test_returns_404_when_plan_missing(self):
        runs_mod = _load_runs_module()
        runs_mod.store.get_plan.return_value = None
        response = runs_mod.apply_plan_snapshot({}, {"id": "missing", "runId": "r1"})
        assert response["statusCode"] == 404

    def test_returns_409_on_value_error(self):
        runs_mod = _load_runs_module()
        runs_mod.store.get_plan.return_value = {"planId": "p1", "buckets": []}
        runs_mod.store.apply_plan_to_run.side_effect = ValueError("Run r1 is not running")

        response = runs_mod.apply_plan_snapshot({}, {"id": "p1", "runId": "r1"})

        assert response["statusCode"] == 409
        assert response["body"]["error"]["code"] == "CONFLICT"

    def test_returns_409_on_concurrent_write_error(self):
        runs_mod = _load_runs_module()
        runs_mod.store.get_plan.return_value = {"planId": "p1", "buckets": []}
        runs_mod.store.ConcurrentWriteError = type("ConcurrentWriteError", (Exception,), {})
        runs_mod.store.apply_plan_to_run.side_effect = runs_mod.store.ConcurrentWriteError("conflict")

        response = runs_mod.apply_plan_snapshot({}, {"id": "p1", "runId": "r1"})

        assert response["statusCode"] == 409
        assert response["body"]["error"]["code"] == "CONCURRENT_WRITE"

    def test_applies_snapshot_and_records_audit(self):
        runs_mod = _load_runs_module()
        runs_mod.store.get_plan.return_value = {"planId": "p1", "buckets": []}
        runs_mod.store.apply_plan_to_run.return_value = {"runId": "r1", "status": "running"}

        response = runs_mod.apply_plan_snapshot({}, {"id": "p1", "runId": "r1"})

        assert response["statusCode"] == 200
        audit_kwargs = runs_mod.build_audit.return_value.record.call_args.kwargs
        assert audit_kwargs["action"] == "apply_snapshot"


class TestBrandedProgressAdditional:
    def test_skips_campaign_state_missing_campaign_id(self):
        runs_mod = _load_runs_module()
        runs_mod.store.get_run.return_value = {
            "bucketStates": [
                {
                    "campaignStates": [
                        {"brandedCampaignId": "bc-1", "campaignId": ""},
                    ]
                }
            ]
        }
        response = runs_mod.branded_progress({}, {"id": "p1", "runId": "r1"})
        assert response["body"]["progress"] == {}
        runs_mod.executor.get_branded_queue_counts.assert_not_called()


class TestBrandedQueue:
    def test_returns_404_when_run_missing(self):
        runs_mod = _load_runs_module()
        runs_mod.store.get_run.return_value = None
        response = runs_mod.branded_queue({}, {"id": "p1", "runId": "missing"})
        assert response["statusCode"] == 404

    def test_skips_campaign_state_missing_branded_campaign_id(self):
        runs_mod = _load_runs_module()
        runs_mod.store.get_run.return_value = {
            "bucketStates": [
                {"campaignStates": [{"campaignId": "c1"}]},  # no brandedCampaignId
            ]
        }
        response = runs_mod.branded_queue({}, {"id": "p1", "runId": "r1"})
        assert response["body"]["items"] == {}
        runs_mod.executor.get_branded_queue_items.assert_not_called()

    def test_skips_campaign_state_missing_campaign_id(self):
        runs_mod = _load_runs_module()
        runs_mod.store.get_run.return_value = {
            "bucketStates": [
                {"campaignStates": [{"brandedCampaignId": "bc-1", "campaignId": ""}]},
            ]
        }
        response = runs_mod.branded_queue({}, {"id": "p1", "runId": "r1"})
        assert response["body"]["items"] == {}
        runs_mod.executor.get_branded_queue_items.assert_not_called()

    def test_returns_items_for_branded_campaign(self):
        runs_mod = _load_runs_module()
        runs_mod.store.get_run.return_value = {
            "bucketStates": [
                {"campaignStates": [{"brandedCampaignId": "bc-1", "campaignId": "c1"}]},
            ]
        }
        runs_mod.executor.get_branded_queue_items.return_value = [
            {"phone_last4": "1234", "status": "PENDING", "seededAt": "t0"}
        ]

        response = runs_mod.branded_queue({}, {"id": "p1", "runId": "r1"})

        assert response["statusCode"] == 200
        assert response["body"]["items"]["c1"] == [
            {"phone_last4": "1234", "status": "PENDING", "seededAt": "t0"}
        ]


class TestBrandedHistory:
    def test_returns_empty_when_table_not_configured(self):
        runs_mod = _load_runs_module()
        runs_mod._BRANDED_RUN_SUMMARY_TABLE = ""
        response = runs_mod.branded_history({}, {"id": "p1"})
        assert response["statusCode"] == 200
        assert response["body"]["history"] == []

    def test_returns_parsed_history_items(self):
        runs_mod = _load_runs_module()
        runs_mod._BRANDED_RUN_SUMMARY_TABLE = "VipBrandedRunSummary"
        mock_ddb = MagicMock()
        mock_ddb.query.return_value = {
            "Items": [
                {
                    "runId": {"S": "r1"},
                    "campaignId": {"S": "camp-1"},
                    "exitReason": {"S": "queue_drained"},
                    "totalSeeded": {"N": "50"},
                    "totalDialed": {"N": "45"},
                    "startedAt": {"S": "t0"},
                    "completedAt": {"S": "t1"},
                }
            ]
        }
        with patch.object(runs_mod, "_get_ddb", return_value=mock_ddb):
            response = runs_mod.branded_history({}, {"id": "p1"})

        assert response["statusCode"] == 200
        history = response["body"]["history"]
        assert history[0]["runId"] == "r1"
        assert history[0]["totalSeeded"] == 50
        assert history[0]["totalDialed"] == 45
        call_kwargs = mock_ddb.query.call_args.kwargs
        assert call_kwargs["ScanIndexForward"] is False

    def test_paginates_through_multiple_pages(self):
        runs_mod = _load_runs_module()
        runs_mod._BRANDED_RUN_SUMMARY_TABLE = "VipBrandedRunSummary"
        mock_ddb = MagicMock()
        mock_ddb.query.side_effect = [
            {
                "Items": [
                    {
                        "runId": {"S": "r1"},
                        "campaignId": {"S": "c1"},
                        "exitReason": {"S": "x"},
                        "totalSeeded": {"N": "1"},
                        "totalDialed": {"N": "1"},
                        "startedAt": {"S": "t0"},
                        "completedAt": {"S": "t1"},
                    }
                ],
                "LastEvaluatedKey": {"runId": {"S": "r1"}},
            },
            {
                "Items": [
                    {
                        "runId": {"S": "r2"},
                        "campaignId": {"S": "c2"},
                        "exitReason": {"S": "x"},
                        "totalSeeded": {"N": "2"},
                        "totalDialed": {"N": "2"},
                        "startedAt": {"S": "t0"},
                        "completedAt": {"S": "t1"},
                    }
                ]
            },
        ]
        with patch.object(runs_mod, "_get_ddb", return_value=mock_ddb):
            response = runs_mod.branded_history({}, {"id": "p1"})

        assert mock_ddb.query.call_count == 2
        assert [h["runId"] for h in response["body"]["history"]] == ["r1", "r2"]

    def test_returns_500_when_query_fails(self):
        runs_mod = _load_runs_module()
        runs_mod._BRANDED_RUN_SUMMARY_TABLE = "VipBrandedRunSummary"
        mock_ddb = MagicMock()
        mock_ddb.query.side_effect = RuntimeError("DynamoDB unavailable")
        with patch.object(runs_mod, "_get_ddb", return_value=mock_ddb):
            response = runs_mod.branded_history({}, {"id": "p1"})

        assert response["statusCode"] == 500
        assert response["body"]["error"]["code"] == "QUERY_FAILED"
