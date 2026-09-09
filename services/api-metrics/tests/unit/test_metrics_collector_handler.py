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


def test_check_and_emit_stall_swallows_error_when_error_metric_itself_fails():
    """If the query fails AND the fallback BrandedStallCheckError metric emission
    also fails, the double failure must still not raise out of the stall check."""
    import metrics_collector_handler as mch

    now = datetime(2026, 8, 27, 15, 0, tzinfo=timezone.utc)
    mock_table = MagicMock()
    mock_table.query.side_effect = RuntimeError("AccessDeniedException")
    mock_ddb = MagicMock()
    mock_ddb.Table.return_value = mock_table
    mock_cw = MagicMock()
    mock_cw.put_metric_data.side_effect = RuntimeError("CloudWatch unavailable")

    with patch.object(mch, "_ddb", mock_ddb), patch.object(mch, "_cw", mock_cw):
        mch._check_and_emit_stall(
            campaign_id="bc-1", plan_id="p-1", placed=4, agents_available=3, now_utc=now
        )  # must not raise


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


def test_lambda_handler_returns_zero_when_no_active_campaigns():
    import metrics_collector_handler as mch

    mock_cw = MagicMock()
    with (
        patch.object(mch, "_cw", mock_cw),
        patch.object(mch, "_scan_active_campaigns", return_value=[]),
    ):
        result = mch.lambda_handler({}, None)

    assert result == {"collected": 0, "queues": 0}
    # Both unconditional metrics must still be emitted even with zero campaigns.
    mock_cw.put_metric_data.assert_called()


def test_lambda_handler_skips_item_missing_campaign_id_or_queue_id():
    import metrics_collector_handler as mch

    active_items = [
        {"campaignId": "", "queueArn": "arn::queue/q1", "planId": "p-1"},
        {"campaignId": "bc-2", "queueArn": "", "planId": "p-1"},
    ]
    mock_ddb = MagicMock()
    mock_cw = MagicMock()

    with (
        patch.object(mch, "_ddb", mock_ddb),
        patch.object(mch, "_cw", mock_cw),
        patch.object(mch, "_scan_active_campaigns", return_value=active_items),
        patch.object(mch, "_resolve_outcomes") as mock_resolve,
    ):
        result = mch.lambda_handler({}, None)

    mock_resolve.assert_not_called()
    assert result == {"collected": 0, "queues": 0}


def test_outcomes_query_failed_metric_emission_swallows_cw_error():
    """Even the fallback BrandedOutcomesQueryFailed metric emission itself
    failing must not abort the collector loop."""
    import metrics_collector_handler as mch

    active_item = {
        "campaignId": "bc-1",
        "queueArn": "arn::queue/q1",
        "planId": "p-1",
        "runId": "r-1",
        "createdAt": "2026-08-27T14:00:00+00:00",
    }
    mock_ddb = MagicMock()
    mock_cw = MagicMock()
    mock_cw.put_metric_data.side_effect = RuntimeError("CloudWatch unavailable")

    with (
        patch.object(mch, "_ddb", mock_ddb),
        patch.object(mch, "_cw", mock_cw),
        patch.object(mch, "_scan_active_campaigns", return_value=[active_item]),
        patch.object(mch, "_resolve_outcomes"),
        patch.object(mch, "_count_outcomes", return_value=None),
    ):
        result = mch.lambda_handler({}, None)  # must not raise

    assert result == {"collected": 0, "queues": 0}


def test_lambda_handler_happy_path_writes_campaign_and_queue_snapshots():
    """Full success path: outcomes resolve, queue metrics are fetched, a
    campaign metrics snapshot is written, and a per-queue agent snapshot is
    written once per distinct queue."""
    import metrics_collector_handler as mch

    active_item = {
        "campaignId": "bc-1",
        "queueArn": "arn:aws:connect:us-east-1:123:instance/abc/queue/q1",
        "planId": "p-1",
        "runId": "r-1",
        "createdAt": "2026-08-27T14:00:00+00:00",
    }
    mock_metrics_table = MagicMock()
    mock_snapshot_table = MagicMock()

    def _table(name):
        if name == mch._METRICS_TABLE:
            return mock_metrics_table
        if name == mch._SNAPSHOT_TABLE:
            return mock_snapshot_table
        return MagicMock()

    mock_ddb = MagicMock()
    mock_ddb.Table.side_effect = _table
    mock_cw = MagicMock()
    mock_connect = MagicMock()
    mock_connect.get_current_metric_data.return_value = {
        "MetricResults": [
            {
                "Collections": [
                    {"Metric": {"Name": "AGENTS_AVAILABLE"}, "Value": 2},
                    {"Metric": {"Name": "AGENTS_STAFFED"}, "Value": 5},
                    {"Metric": {"Name": "AGENTS_ONLINE"}, "Value": 4},
                    {"Metric": {"Name": "AGENTS_ON_CONTACT"}, "Value": 1},
                    {"Metric": {"Name": "CONTACTS_IN_QUEUE"}, "Value": 0},
                ]
            }
        ]
    }

    with (
        patch.object(mch, "_ddb", mock_ddb),
        patch.object(mch, "_cw", mock_cw),
        patch.object(mch, "_connect", mock_connect),
        patch.object(mch, "_scan_active_campaigns", return_value=[active_item]),
        patch.object(mch, "_resolve_outcomes"),
        patch.object(mch, "_count_outcomes", return_value=(10, 6, 2, 1, 1)),
        patch.object(mch, "_check_and_emit_stall") as mock_stall_check,
    ):
        result = mch.lambda_handler({}, None)

    assert result == {"collected": 1, "queues": 1}
    mock_stall_check.assert_called_once()
    mock_metrics_table.put_item.assert_called_once()
    metrics_item = mock_metrics_table.put_item.call_args.kwargs["Item"]
    assert metrics_item["brandedCampaignId"] == "bc-1"
    assert metrics_item["contactsPlaced"] == 10
    assert metrics_item["contactsAnswered"] == 6
    assert metrics_item["answerRate"] == "60.0"
    assert metrics_item["agentsAvailable"] == 2

    mock_snapshot_table.put_item.assert_called_once()
    snapshot_item = mock_snapshot_table.put_item.call_args.kwargs["Item"]
    assert snapshot_item["queueId"] == "q1"
    assert snapshot_item["agentsOnline"] == 4


def test_scan_active_campaigns_returns_empty_when_table_not_configured():
    import metrics_collector_handler as mch

    with patch.object(mch, "_ACTIVE_TABLE", ""):
        assert mch._scan_active_campaigns() == []


def test_scan_active_campaigns_paginates_through_all_pages():
    import metrics_collector_handler as mch

    mock_table = MagicMock()
    mock_table.scan.side_effect = [
        {"Items": [{"campaignId": "bc-1"}], "LastEvaluatedKey": {"campaignId": "bc-1"}},
        {"Items": [{"campaignId": "bc-2"}]},
    ]
    mock_ddb = MagicMock()
    mock_ddb.Table.return_value = mock_table

    with (
        patch.object(mch, "_ddb", mock_ddb),
        patch.object(mch, "_ACTIVE_TABLE", "VipActiveBrandedCampaigns"),
    ):
        items = mch._scan_active_campaigns()

    assert [i["campaignId"] for i in items] == ["bc-1", "bc-2"]
    assert mock_table.scan.call_count == 2
    second_call_kwargs = mock_table.scan.call_args_list[1].kwargs
    assert second_call_kwargs["ExclusiveStartKey"] == {"campaignId": "bc-1"}


class TestDetermineOutcome:
    def test_returns_none_when_still_in_progress(self):
        import metrics_collector_handler as mch

        assert mch._determine_outcome({}) is None

    def test_returns_answered_when_agent_connected(self):
        import metrics_collector_handler as mch

        contact = {
            "DisconnectTimestamp": "2026-08-27T15:00:00Z",
            "AgentInfo": {"ConnectedToAgentTimestamp": "2026-08-27T14:59:00Z"},
        }
        assert mch._determine_outcome(contact) == "answered"

    @pytest.mark.parametrize("reason", ["TELECOM_PROBLEM", "CONTACT_FLOW_ERROR"])
    def test_returns_busy_on_carrier_reject_reasons(self, reason):
        import metrics_collector_handler as mch

        contact = {
            "DisconnectTimestamp": "2026-08-27T15:00:00Z",
            "DisconnectReason": reason,
        }
        assert mch._determine_outcome(contact) == "busy"

    def test_returns_voicemail_when_connected_to_system_but_no_agent(self):
        import metrics_collector_handler as mch

        contact = {
            "DisconnectTimestamp": "2026-08-27T15:00:00Z",
            "ConnectedToSystemTimestamp": "2026-08-27T14:59:30Z",
        }
        assert mch._determine_outcome(contact) == "voicemail"

    def test_returns_no_answer_as_fallback(self):
        import metrics_collector_handler as mch

        contact = {"DisconnectTimestamp": "2026-08-27T15:00:00Z"}
        assert mch._determine_outcome(contact) == "no_answer"


class TestResolveOutcomes:
    def test_returns_early_when_instance_id_not_set(self):
        import metrics_collector_handler as mch

        mock_ddb = MagicMock()
        with (
            patch.object(mch, "_ddb", mock_ddb),
            patch.object(mch, "_CONNECT_INSTANCE_ID", ""),
        ):
            mch._resolve_outcomes("bc-1")

        mock_ddb.Table.assert_not_called()

    def test_logs_and_returns_when_query_fails(self):
        import metrics_collector_handler as mch

        mock_table = MagicMock()
        mock_table.query.side_effect = RuntimeError("AccessDeniedException")
        mock_ddb = MagicMock()
        mock_ddb.Table.return_value = mock_table

        with patch.object(mch, "_ddb", mock_ddb):
            mch._resolve_outcomes("bc-1")  # must not raise

    def test_skips_items_without_contact_id(self):
        import metrics_collector_handler as mch

        mock_table = MagicMock()
        mock_table.query.return_value = {"Items": [{"sk": "ts1#uuid1"}]}
        mock_ddb = MagicMock()
        mock_ddb.Table.return_value = mock_table
        mock_connect = MagicMock()

        with (
            patch.object(mch, "_ddb", mock_ddb),
            patch.object(mch, "_connect", mock_connect),
        ):
            mch._resolve_outcomes("bc-1")

        mock_connect.describe_contact.assert_not_called()

    def test_leaves_in_progress_contact_untouched(self):
        import metrics_collector_handler as mch

        mock_table = MagicMock()
        mock_table.query.return_value = {
            "Items": [{"sk": "ts1#uuid1", "contactId": "contact-1"}]
        }
        mock_ddb = MagicMock()
        mock_ddb.Table.return_value = mock_table
        mock_connect = MagicMock()
        mock_connect.describe_contact.return_value = {"Contact": {}}  # no DisconnectTimestamp

        with (
            patch.object(mch, "_ddb", mock_ddb),
            patch.object(mch, "_connect", mock_connect),
        ):
            mch._resolve_outcomes("bc-1")

        mock_table.update_item.assert_not_called()

    def test_persists_resolved_outcome(self):
        import metrics_collector_handler as mch

        mock_table = MagicMock()
        mock_table.query.return_value = {
            "Items": [{"sk": "ts1#uuid1", "contactId": "contact-1"}]
        }
        mock_ddb = MagicMock()
        mock_ddb.Table.return_value = mock_table
        mock_connect = MagicMock()
        mock_connect.describe_contact.return_value = {
            "Contact": {
                "DisconnectTimestamp": "2026-08-27T15:00:00Z",
                "AgentInfo": {"ConnectedToAgentTimestamp": "2026-08-27T14:59:00Z"},
            }
        }

        with (
            patch.object(mch, "_ddb", mock_ddb),
            patch.object(mch, "_connect", mock_connect),
        ):
            mch._resolve_outcomes("bc-1")

        mock_table.update_item.assert_called_once()
        kwargs = mock_table.update_item.call_args.kwargs
        assert kwargs["ExpressionAttributeValues"] == {":o": "answered"}

    def test_swallows_per_contact_errors_and_continues(self):
        import metrics_collector_handler as mch

        mock_table = MagicMock()
        mock_table.query.return_value = {
            "Items": [
                {"sk": "ts1#uuid1", "contactId": "contact-1"},
                {"sk": "ts2#uuid2", "contactId": "contact-2"},
            ]
        }
        mock_ddb = MagicMock()
        mock_ddb.Table.return_value = mock_table
        mock_connect = MagicMock()
        mock_connect.describe_contact.side_effect = [
            RuntimeError("Connect throttled"),
            {
                "Contact": {
                    "DisconnectTimestamp": "2026-08-27T15:00:00Z",
                    "AgentInfo": {"ConnectedToAgentTimestamp": "2026-08-27T14:59:00Z"},
                }
            },
        ]

        with (
            patch.object(mch, "_ddb", mock_ddb),
            patch.object(mch, "_connect", mock_connect),
        ):
            mch._resolve_outcomes("bc-1")  # must not raise despite first contact failing

        mock_table.update_item.assert_called_once()


def test_count_outcomes_success_path_tallies_by_outcome():
    import metrics_collector_handler as mch

    mock_table = MagicMock()
    mock_table.query.return_value = {
        "Items": [
            {"outcome": "answered"},
            {"outcome": "answered"},
            {"outcome": "voicemail"},
            {"outcome": "busy"},
            {"outcome": "no_answer"},
            {},  # in-progress, no outcome yet — counted only in `placed`
        ]
    }
    mock_ddb = MagicMock()
    mock_ddb.Table.return_value = mock_table

    with patch.object(mch, "_ddb", mock_ddb):
        result = mch._count_outcomes("bc-1")

    assert result == (6, 2, 1, 1, 1)


class TestQueueMetrics:
    def test_returns_empty_result_when_queue_id_missing(self):
        import metrics_collector_handler as mch

        mock_connect = MagicMock()
        with (
            patch.object(mch, "_connect", mock_connect),
            patch.object(mch, "_CONNECT_INSTANCE_ID", "instance-1"),
        ):
            result = mch._queue_metrics("", "arn::queue/q1")

        assert result == {"_queueArn": "arn::queue/q1"}
        mock_connect.get_current_metric_data.assert_not_called()

    def test_returns_empty_result_when_instance_id_missing(self):
        import metrics_collector_handler as mch

        mock_connect = MagicMock()
        with (
            patch.object(mch, "_connect", mock_connect),
            patch.object(mch, "_CONNECT_INSTANCE_ID", ""),
        ):
            result = mch._queue_metrics("q1", "arn::queue/q1")

        assert result == {"_queueArn": "arn::queue/q1"}
        mock_connect.get_current_metric_data.assert_not_called()

    def test_returns_parsed_metrics_on_success(self):
        import metrics_collector_handler as mch

        mock_connect = MagicMock()
        mock_connect.get_current_metric_data.return_value = {
            "MetricResults": [
                {"Collections": [{"Metric": {"Name": "AGENTS_AVAILABLE"}, "Value": 3}]}
            ]
        }
        with (
            patch.object(mch, "_connect", mock_connect),
            patch.object(mch, "_CONNECT_INSTANCE_ID", "instance-1"),
        ):
            result = mch._queue_metrics("q1", "arn::queue/q1")

        assert result["AGENTS_AVAILABLE"] == 3
        assert result["_queueArn"] == "arn::queue/q1"

    def test_swallows_connect_errors_and_returns_partial_result(self):
        import metrics_collector_handler as mch

        mock_connect = MagicMock()
        mock_connect.get_current_metric_data.side_effect = RuntimeError("Throttled")
        with (
            patch.object(mch, "_connect", mock_connect),
            patch.object(mch, "_CONNECT_INSTANCE_ID", "instance-1"),
        ):
            result = mch._queue_metrics("q1", "arn::queue/q1")

        assert result == {"_queueArn": "arn::queue/q1"}


class TestEmitBusinessHoursMetric:
    def test_skips_emit_outside_business_hours(self):
        import metrics_collector_handler as mch

        mock_cw = MagicMock()
        now = datetime(2026, 8, 27, 3, 0, tzinfo=timezone.utc)  # 3am UTC — outside 12-23
        with patch.object(mch, "_cw", mock_cw):
            mch._emit_business_hours_metric(2, now)

        mock_cw.put_metric_data.assert_not_called()

    def test_emits_during_business_hours(self):
        import metrics_collector_handler as mch

        mock_cw = MagicMock()
        now = datetime(2026, 8, 27, 15, 0, tzinfo=timezone.utc)
        with patch.object(mch, "_cw", mock_cw):
            mch._emit_business_hours_metric(2, now)

        mock_cw.put_metric_data.assert_called_once()

    def test_swallows_cloudwatch_errors(self):
        import metrics_collector_handler as mch

        mock_cw = MagicMock()
        mock_cw.put_metric_data.side_effect = RuntimeError("CloudWatch unavailable")
        now = datetime(2026, 8, 27, 15, 0, tzinfo=timezone.utc)
        with patch.object(mch, "_cw", mock_cw):
            mch._emit_business_hours_metric(2, now)  # must not raise


def test_check_and_emit_stall_treats_malformed_snapshot_timestamp_as_infinitely_old():
    """A malformed snapshotAt must not crash datetime.fromisoformat — treated as
    infinitely stale so the gap-too-large guard rejects it."""
    import metrics_collector_handler as mch

    now = datetime(2026, 8, 27, 15, 0, tzinfo=timezone.utc)
    mock_table = MagicMock()
    mock_table.query.return_value = {
        "Items": [{"contactsPlaced": 4, "snapshotAt": "not-a-timestamp"}]
    }
    mock_ddb = MagicMock()
    mock_ddb.Table.return_value = mock_table
    mock_cw = MagicMock()

    with patch.object(mch, "_ddb", mock_ddb), patch.object(mch, "_cw", mock_cw):
        mch._check_and_emit_stall(
            campaign_id="bc-1", plan_id="p-1", placed=4, agents_available=3, now_utc=now
        )

    mock_cw.put_metric_data.assert_not_called()


def test_check_and_emit_stall_swallows_error_when_stalled_metric_emit_fails():
    import metrics_collector_handler as mch

    now = datetime(2026, 8, 27, 15, 0, tzinfo=timezone.utc)
    mock_table = MagicMock()
    mock_table.query.return_value = {
        "Items": [{"contactsPlaced": 4, "snapshotAt": "2026-08-27T14:48:00+00:00"}]
    }
    mock_ddb = MagicMock()
    mock_ddb.Table.return_value = mock_table
    mock_cw = MagicMock()
    mock_cw.put_metric_data.side_effect = RuntimeError("CloudWatch unavailable")

    with patch.object(mch, "_ddb", mock_ddb), patch.object(mch, "_cw", mock_cw):
        mch._check_and_emit_stall(
            campaign_id="bc-1", plan_id="p-1", placed=4, agents_available=3, now_utc=now
        )  # must not raise


class TestEmitStuckCampaignsMetric:
    def test_emits_zero_when_no_stuck_campaigns(self):
        import metrics_collector_handler as mch

        now = datetime(2026, 8, 27, 15, 0, tzinfo=timezone.utc)
        mock_cw = MagicMock()
        with patch.object(mch, "_cw", mock_cw):
            mch._emit_stuck_campaigns_metric(
                [{"createdAt": now.isoformat()}], now
            )

        mock_cw.put_metric_data.assert_called_once()
        value = mock_cw.put_metric_data.call_args.kwargs["MetricData"][0]["Value"]
        assert value == 0.0

    def test_counts_campaigns_older_than_26_hours_as_stuck(self):
        import metrics_collector_handler as mch

        now = datetime(2026, 8, 27, 15, 0, tzinfo=timezone.utc)
        stuck_created_at = (now - timedelta(hours=30)).isoformat()
        mock_cw = MagicMock()
        with patch.object(mch, "_cw", mock_cw):
            mch._emit_stuck_campaigns_metric(
                [{"createdAt": stuck_created_at}], now
            )

        value = mock_cw.put_metric_data.call_args.kwargs["MetricData"][0]["Value"]
        assert value == 1.0

    def test_swallows_cloudwatch_errors(self):
        import metrics_collector_handler as mch

        now = datetime(2026, 8, 27, 15, 0, tzinfo=timezone.utc)
        mock_cw = MagicMock()
        mock_cw.put_metric_data.side_effect = RuntimeError("CloudWatch unavailable")
        with patch.object(mch, "_cw", mock_cw):
            mch._emit_stuck_campaigns_metric([], now)  # must not raise
