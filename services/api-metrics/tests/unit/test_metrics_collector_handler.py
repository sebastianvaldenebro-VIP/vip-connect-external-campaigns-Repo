"""Tests for metrics_collector_handler.py's stall-detection metric.

Bug this covers: neither StuckRun (4h threshold) nor NoActiveCampaign (checks
campaign *status*, not throughput) catches a branded campaign that stays
"running" indefinitely with near-zero dispatch despite free agent capacity —
exactly what happened 2026-08-27 with Plan 1.2/2.2 before the sweep-timer fix.
This adds a dedicated BrandedCampaignStalled metric for that failure mode.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ACTIVE_BRANDED_CAMPAIGNS_TABLE", "VipActiveBrandedCampaigns")
    monkeypatch.setenv("BRANDED_CAMPAIGN_METRICS_TABLE", "VipBrandedCampaignMetrics")
    monkeypatch.setenv("AGENT_SNAPSHOT_TABLE", "VipAgentSnapshot")
    monkeypatch.setenv("CONNECT_INSTANCE_ID", "instance-1")


def test_emits_stalled_metric_when_no_progress_and_agents_available():
    import metrics_collector_handler as mch

    now = datetime(2026, 8, 27, 15, 0, tzinfo=timezone.utc)
    mock_table = MagicMock()
    mock_table.query.return_value = {
        "Items": [{"contactsPlaced": 4, "snapshotAt": "2026-08-27T14:48:00+00:00"}]
    }
    mock_ddb = MagicMock()
    mock_ddb.Table.return_value = mock_table
    mock_cw = MagicMock()

    with patch.object(mch, "_ddb", mock_ddb), patch.object(mch, "_cw", mock_cw):
        mch._check_and_emit_stall(
            campaign_id="bc-1", plan_id="p-1", placed=4, agents_available=3, now_utc=now
        )

    mock_cw.put_metric_data.assert_called_once()
    call = mock_cw.put_metric_data.call_args.kwargs
    assert call["Namespace"] == "VipBrandedMonitor"
    names = {m["MetricName"] for m in call["MetricData"]}
    assert names == {"BrandedCampaignStalled"}


def test_does_not_emit_when_progress_made():
    import metrics_collector_handler as mch

    now = datetime(2026, 8, 27, 15, 0, tzinfo=timezone.utc)
    mock_table = MagicMock()
    mock_table.query.return_value = {
        "Items": [{"contactsPlaced": 4, "snapshotAt": "2026-08-27T14:48:00+00:00"}]
    }
    mock_ddb = MagicMock()
    mock_ddb.Table.return_value = mock_table
    mock_cw = MagicMock()

    with patch.object(mch, "_ddb", mock_ddb), patch.object(mch, "_cw", mock_cw):
        mch._check_and_emit_stall(
            campaign_id="bc-1", plan_id="p-1", placed=9, agents_available=3, now_utc=now
        )

    mock_cw.put_metric_data.assert_not_called()


def test_does_not_emit_when_no_agents_available():
    import metrics_collector_handler as mch

    now = datetime(2026, 8, 27, 15, 0, tzinfo=timezone.utc)
    mock_ddb = MagicMock()
    mock_cw = MagicMock()

    with patch.object(mch, "_ddb", mock_ddb), patch.object(mch, "_cw", mock_cw):
        mch._check_and_emit_stall(
            campaign_id="bc-1", plan_id="p-1", placed=4, agents_available=0, now_utc=now
        )

    mock_ddb.Table.assert_not_called()  # short-circuits before querying history
    mock_cw.put_metric_data.assert_not_called()


def test_does_not_emit_when_no_prior_snapshot_yet():
    import metrics_collector_handler as mch

    now = datetime(2026, 8, 27, 15, 0, tzinfo=timezone.utc)
    mock_table = MagicMock()
    mock_table.query.return_value = {
        "Items": []
    }  # campaign younger than lookback window
    mock_ddb = MagicMock()
    mock_ddb.Table.return_value = mock_table
    mock_cw = MagicMock()

    with patch.object(mch, "_ddb", mock_ddb), patch.object(mch, "_cw", mock_cw):
        mch._check_and_emit_stall(
            campaign_id="bc-1", plan_id="p-1", placed=4, agents_available=3, now_utc=now
        )

    mock_cw.put_metric_data.assert_not_called()


def test_query_uses_lookback_cutoff_and_most_recent_before_it():
    import metrics_collector_handler as mch

    now = datetime(2026, 8, 27, 15, 0, tzinfo=timezone.utc)
    mock_table = MagicMock()
    mock_table.query.return_value = {"Items": []}
    mock_ddb = MagicMock()
    mock_ddb.Table.return_value = mock_table
    mock_cw = MagicMock()

    with patch.object(mch, "_ddb", mock_ddb), patch.object(mch, "_cw", mock_cw):
        mch._check_and_emit_stall(
            campaign_id="bc-1", plan_id="p-1", placed=4, agents_available=3, now_utc=now
        )

    mock_table.query.assert_called_once()
    kwargs = mock_table.query.call_args.kwargs
    assert kwargs["ScanIndexForward"] is False
    assert kwargs["Limit"] == 1


# Bug: no upper bound on how old a "prior" snapshot could be. brandedCampaignId
# is deterministic per (planId, runId, bucket_index, campaign_index) and
# survives a stop/force-restart within the same run — the first post-restart
# cycle could compare against an hours-old pre-restart snapshot and emit a
# false BrandedCampaignStalled right after a legitimate restart (root-caused
# 2026-08-27, adversarial code review).


def test_does_not_emit_when_prior_snapshot_is_too_stale_after_restart():
    import metrics_collector_handler as mch

    now = datetime(2026, 8, 27, 15, 0, tzinfo=timezone.utc)
    stale_snapshot_time = (now - timedelta(hours=3)).isoformat()
    mock_table = MagicMock()
    mock_table.query.return_value = {
        "Items": [{"contactsPlaced": 50, "snapshotAt": stale_snapshot_time}]
    }
    mock_ddb = MagicMock()
    mock_ddb.Table.return_value = mock_table
    mock_cw = MagicMock()

    with patch.object(mch, "_ddb", mock_ddb), patch.object(mch, "_cw", mock_cw):
        mch._check_and_emit_stall(
            campaign_id="bc-1", plan_id="p-1", placed=2, agents_available=3, now_utc=now
        )

    mock_cw.put_metric_data.assert_not_called()


# Bug: the 2x-lookback staleness bound (20 min) left a false-positive window
# for restart gaps between 10 and 20 minutes — the query itself already
# requires the found snapshot to be >=10 min old (Key("snapshotAt").lte(now
# -10min)), so a restart gap of e.g. 14 minutes was NOT caught by the 20-min
# bound and would still false-positive right after a legitimate restart
# (root-caused 2026-08-27, second adversarial review round).


def test_does_not_emit_for_a_restart_gap_inside_the_old_20min_window():
    import metrics_collector_handler as mch

    now = datetime(2026, 8, 27, 15, 0, tzinfo=timezone.utc)
    # 14 minutes old — inside the OLD (10,20] false-positive band, must now
    # be rejected by the tighter bound.
    borderline_snapshot_time = (now - timedelta(minutes=14)).isoformat()
    mock_table = MagicMock()
    mock_table.query.return_value = {
        "Items": [{"contactsPlaced": 50, "snapshotAt": borderline_snapshot_time}]
    }
    mock_ddb = MagicMock()
    mock_ddb.Table.return_value = mock_table
    mock_cw = MagicMock()

    with patch.object(mch, "_ddb", mock_ddb), patch.object(mch, "_cw", mock_cw):
        mch._check_and_emit_stall(
            campaign_id="bc-1", plan_id="p-1", placed=2, agents_available=3, now_utc=now
        )

    mock_cw.put_metric_data.assert_not_called()


# Bug: the stall-detection mechanism's own failures (e.g. a lost IAM
# permission) were only logged, never surfaced as a metric — the check meant
# to catch silent failures elsewhere failed silently itself (root-caused
# 2026-08-27, adversarial code review).


def test_check_and_emit_stall_emits_metric_when_query_fails():
    import metrics_collector_handler as mch

    now = datetime(2026, 8, 27, 15, 0, tzinfo=timezone.utc)
    mock_table = MagicMock()
    mock_table.query.side_effect = RuntimeError("AccessDeniedException")
    mock_ddb = MagicMock()
    mock_ddb.Table.return_value = mock_table
    mock_cw = MagicMock()

    with patch.object(mch, "_ddb", mock_ddb), patch.object(mch, "_cw", mock_cw):
        mch._check_and_emit_stall(
            campaign_id="bc-1", plan_id="p-1", placed=4, agents_available=3, now_utc=now
        )

    mock_cw.put_metric_data.assert_called_once()
    metric_data = mock_cw.put_metric_data.call_args.kwargs["MetricData"]
    metric_names = {m["MetricName"] for m in metric_data}
    assert "BrandedStallCheckError" in metric_names
    # Bug: emitted with no Dimensions, unlike BrandedCampaignStalled — on-call
    # couldn't identify the affected campaign from CloudWatch alone (root-caused
    # 2026-08-27, second adversarial review round).
    stall_check_error = next(
        m for m in metric_data if m["MetricName"] == "BrandedStallCheckError"
    )
    assert {"Name": "CampaignId", "Value": "bc-1"} in stall_check_error["Dimensions"]
    assert {"Name": "PlanId", "Value": "p-1"} in stall_check_error["Dimensions"]


# Bug: _count_outcomes returned a fabricated (0,0,0,0,0) on any query error,
# indistinguishable from genuine zero progress — lambda_handler passed that
# straight into _check_and_emit_stall (false stall alarm) and persisted it as
# a real VipBrandedCampaignMetrics snapshot (contaminating the history used by
# the NEXT cycle's stall check too) (root-caused 2026-08-27, adversarial code
# review).


def test_count_outcomes_returns_none_on_query_error():
    import metrics_collector_handler as mch

    mock_table = MagicMock()
    mock_table.query.side_effect = RuntimeError("ProvisionedThroughputExceeded")
    mock_ddb = MagicMock()
    mock_ddb.Table.return_value = mock_table

    with patch.object(mch, "_ddb", mock_ddb):
        result = mch._count_outcomes("bc-1")

    assert result is None


def test_lambda_handler_skips_campaign_when_outcomes_query_fails():
    import metrics_collector_handler as mch

    active_item = {
        "campaignId": "bc-1",
        "queueArn": "arn::queue/q1",
        "planId": "p-1",
        "runId": "r-1",
        "createdAt": "2026-08-27T14:00:00+00:00",
    }
    mock_metrics_table = MagicMock()
    mock_ddb = MagicMock()
    mock_ddb.Table.return_value = mock_metrics_table
    mock_cw = MagicMock()
    mock_connect = MagicMock()

    with (
        patch.object(mch, "_ddb", mock_ddb),
        patch.object(mch, "_cw", mock_cw),
        patch.object(mch, "_connect", mock_connect),
        patch.object(mch, "_scan_active_campaigns", return_value=[active_item]),
        patch.object(mch, "_resolve_outcomes"),
        patch.object(mch, "_count_outcomes", return_value=None),
        patch.object(mch, "_check_and_emit_stall") as mock_stall_check,
    ):
        result = mch.lambda_handler({}, None)

    mock_stall_check.assert_not_called()
    mock_metrics_table.put_item.assert_not_called()
    assert result["collected"] == 0
    assert result["queues"] == 0
    # Bug: a sustained outcomes-query failure (lost IAM permission, sustained
    # throttling) produced zero CloudWatch signal, only log lines — the same
    # blind spot BD-021 item 7 closed for _check_and_emit_stall's own query,
    # one level up (root-caused 2026-08-27, second adversarial review round).
    # NOTE: _emit_business_hours_metric/_emit_stuck_campaigns_metric also call
    # put_metric_data unconditionally, so check across ALL calls, not just one.
    all_metric_names = {
        m["MetricName"]
        for call in mock_cw.put_metric_data.call_args_list
        for m in call.kwargs["MetricData"]
    }
    assert "BrandedOutcomesQueryFailed" in all_metric_names
