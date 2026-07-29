"""Tests for branded_exporter.py."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

sys.modules.setdefault("awswrangler", MagicMock())
sys.modules.setdefault("awswrangler.s3", MagicMock())

_ENV = {
    "BRANDED_RUN_SUMMARY_TABLE": "VipBrandedRunSummary",
    "BRANDED_CAMPAIGN_METRICS_TABLE": "VipBrandedCampaignMetrics",
    "DATA_EXPORT_BUCKET": "test-bucket",
    "GLUE_JOB_NAME": "specialOps-prod-snowflake-loader-glue",
    "SF_SRC_CFG_URI": "s3://config/sf.json",
    "SF_DATABASE": "PRD_RAW_DB",
    "SF_SCHEMA": "AMAZON_CONNECT",
}

_SAMPLE_RUN = {
    "planId": "plan-1",
    "sk": "2026-01-01#cmp-1",
    "runId": "run-1",
    "campaignId": "camp-1",
    "brandedCampaignId": "cmp-1",
    "planName": "Test Plan",
    "segmentArn": "arn:aws:profile:us-east-1:123:domains/d/segment-definitions/seg-1",
    "segmentName": "seg-1",
    "segmentDefinitionJson": "{}",
    "segmentSize": 50,
    "contactFlowId": "flow-1",
    "queueArn": "arn:aws:connect:us-east-1:123:instance/i-1/queue/q-1",
    "sourcePhoneLast4": "1234",
    "bucketIndex": 0,
    "priority": 1,
    "status": "COMPLETED",
    "startedAt": "2026-01-01T10:00:00+00:00",
    "completedAt": "2026-01-01T10:30:00+00:00",
    "totalSeeded": 50,
    "totalDialed": 45,
    "exitReason": "queue_drained",
    "durationSeconds": 1800,
}

_SAMPLE_METRIC = {
    "brandedCampaignId": "cmp-1",
    "snapshotAt": "2026-01-01T10:15:00+00:00",
    "planId": "plan-1",
    "runId": "run-1",
    "queueArn": "arn:aws:connect:us-east-1:123:instance/i-1/queue/q-1",
    "windowStart": "2026-01-01T10:00:00+00:00",
    "windowEnd": "2026-01-01T10:15:00+00:00",
    "contactsPlaced": 10,
    "contactsAnswered": 7,
    "contactsVoicemail": 2,
    "contactsBusy": 1,
    "contactsNoAnswer": 0,
    "contactsFailed": 0,
    "answerRate": "0.7",
    "voicemailRate": "0.2",
    "agentsOnCall": 3,
    "agentsAvailable": 2,
    "agentsStaffed": 5,
    "contactsInQueue": 0,
}


def _load_exporter():
    with patch.dict(os.environ, _ENV):
        with patch("boto3.resource"), patch("boto3.client"):
            import importlib
            import branded_exporter
            importlib.reload(branded_exporter)
            return branded_exporter


# ── export_branded_runs ───────────────────────────────────────────────────────

def test_export_branded_runs_empty_scan_returns_zero_without_glue():
    exporter = _load_exporter()

    mock_ddb = MagicMock()
    mock_table = MagicMock()
    mock_table.scan.return_value = {"Items": []}
    mock_ddb.Table.return_value = mock_table
    mock_glue = MagicMock()

    with (
        patch.dict(os.environ, _ENV),
        patch.object(exporter, "_ddb", mock_ddb),
        patch.object(exporter, "_glue", mock_glue),
    ):
        result = exporter.export_branded_runs()

    assert result == {"exported": 0, "table": "BRANDED_CAMPAIGN_RUNS"}
    mock_glue.start_job_run.assert_not_called()


def test_export_branded_runs_with_items_triggers_glue():
    exporter = _load_exporter()

    mock_ddb = MagicMock()
    mock_table = MagicMock()
    mock_table.scan.return_value = {"Items": [_SAMPLE_RUN]}
    mock_ddb.Table.return_value = mock_table
    mock_glue = MagicMock()

    with (
        patch.dict(os.environ, _ENV),
        patch.object(exporter, "_ddb", mock_ddb),
        patch.object(exporter, "_glue", mock_glue),
        patch("branded_exporter.wr"),
    ):
        result = exporter.export_branded_runs()

    assert result["exported"] == 1
    assert result["table"] == "BRANDED_CAMPAIGN_RUNS"
    mock_glue.start_job_run.assert_called_once()


def test_export_branded_runs_glue_args_no_sf_key_secret():
    """Glue must NOT receive --sf_key_secret — lets the job use its default key."""
    exporter = _load_exporter()

    mock_ddb = MagicMock()
    mock_table = MagicMock()
    mock_table.scan.return_value = {"Items": [_SAMPLE_RUN]}
    mock_ddb.Table.return_value = mock_table
    mock_glue = MagicMock()

    with (
        patch.dict(os.environ, _ENV),
        patch.object(exporter, "_ddb", mock_ddb),
        patch.object(exporter, "_glue", mock_glue),
        patch("branded_exporter.wr"),
    ):
        exporter.export_branded_runs()

    call_kwargs = mock_glue.start_job_run.call_args.kwargs
    args = call_kwargs["Arguments"]
    assert "--sf_key_secret" not in args
    assert args["--sfDatabase"] == "PRD_RAW_DB"
    assert args["--sfSchema"] == "AMAZON_CONNECT"
    assert args["--target_table"] == "BRANDED_CAMPAIGN_RUNS"
    assert args["--pk_columns"] == "plan_id,sk"
    assert args["--load_type"] == "merge"
    assert args["--pipeline"] == "vip-connect"
    assert "branded_campaign_runs" in args["--input_path"]


def test_export_branded_runs_column_mapping():
    exporter = _load_exporter()

    mock_ddb = MagicMock()
    mock_table = MagicMock()
    mock_table.scan.return_value = {"Items": [_SAMPLE_RUN]}
    mock_ddb.Table.return_value = mock_table
    mock_glue = MagicMock()
    captured_dfs = []

    def capture_parquet(df, **kwargs):
        captured_dfs.append(df)

    with (
        patch.dict(os.environ, _ENV),
        patch.object(exporter, "_ddb", mock_ddb),
        patch.object(exporter, "_glue", mock_glue),
        patch("branded_exporter.wr") as mock_wr,
    ):
        mock_wr.s3.to_parquet.side_effect = capture_parquet
        exporter.export_branded_runs()

    assert len(captured_dfs) == 1
    df = captured_dfs[0]
    assert "plan_id" in df.columns
    assert "branded_campaign_id" in df.columns
    assert "segment_size" in df.columns
    assert "total_seeded" in df.columns
    assert "source_phone_last4" in df.columns
    assert "extracted_at" in df.columns
    assert df.iloc[0]["plan_id"] == "plan-1"
    assert df.iloc[0]["branded_campaign_id"] == "cmp-1"
    assert df.iloc[0]["segment_size"] == 50


def test_export_branded_runs_enriches_with_last_metric():
    """final_* columns come from the most recent METRICS snapshot."""
    exporter = _load_exporter()

    mock_ddb = MagicMock()
    runs_table = MagicMock()
    metrics_table = MagicMock()
    runs_table.scan.return_value = {"Items": [_SAMPLE_RUN]}
    metrics_table.scan.return_value = {"Items": [_SAMPLE_METRIC]}

    def table_factory(name):
        if "Metrics" in name:
            return metrics_table
        return runs_table

    mock_ddb.Table.side_effect = table_factory
    mock_glue = MagicMock()
    captured_dfs = []

    def capture_parquet(df, **kwargs):
        captured_dfs.append(df)

    with (
        patch.dict(os.environ, _ENV),
        patch.object(exporter, "_ddb", mock_ddb),
        patch.object(exporter, "_glue", mock_glue),
        patch("branded_exporter.wr") as mock_wr,
    ):
        mock_wr.s3.to_parquet.side_effect = capture_parquet
        exporter.export_branded_runs()

    df = captured_dfs[0]
    assert df.iloc[0]["final_contacts_placed"] == 10
    assert df.iloc[0]["final_contacts_answered"] == 7
    assert df.iloc[0]["final_answer_rate"] == pytest.approx(0.7)


# ── export_branded_metrics ────────────────────────────────────────────────────

def test_export_branded_metrics_empty_scan_returns_zero_without_glue():
    exporter = _load_exporter()

    mock_ddb = MagicMock()
    mock_table = MagicMock()
    mock_table.scan.return_value = {"Items": []}
    mock_ddb.Table.return_value = mock_table
    mock_glue = MagicMock()

    with (
        patch.dict(os.environ, _ENV),
        patch.object(exporter, "_ddb", mock_ddb),
        patch.object(exporter, "_glue", mock_glue),
    ):
        result = exporter.export_branded_metrics()

    assert result == {"exported": 0, "table": "BRANDED_CAMPAIGN_METRICS"}
    mock_glue.start_job_run.assert_not_called()


def test_export_branded_metrics_with_items_triggers_glue():
    exporter = _load_exporter()

    mock_ddb = MagicMock()
    mock_table = MagicMock()
    mock_table.scan.return_value = {"Items": [_SAMPLE_METRIC]}
    mock_ddb.Table.return_value = mock_table
    mock_glue = MagicMock()

    with (
        patch.dict(os.environ, _ENV),
        patch.object(exporter, "_ddb", mock_ddb),
        patch.object(exporter, "_glue", mock_glue),
        patch("branded_exporter.wr"),
    ):
        result = exporter.export_branded_metrics()

    assert result["exported"] == 1
    assert result["table"] == "BRANDED_CAMPAIGN_METRICS"
    mock_glue.start_job_run.assert_called_once()


def test_export_branded_metrics_glue_args_no_sf_key_secret():
    """Glue must NOT receive --sf_key_secret for metrics export either."""
    exporter = _load_exporter()

    mock_ddb = MagicMock()
    mock_table = MagicMock()
    mock_table.scan.return_value = {"Items": [_SAMPLE_METRIC]}
    mock_ddb.Table.return_value = mock_table
    mock_glue = MagicMock()

    with (
        patch.dict(os.environ, _ENV),
        patch.object(exporter, "_ddb", mock_ddb),
        patch.object(exporter, "_glue", mock_glue),
        patch("branded_exporter.wr"),
    ):
        exporter.export_branded_metrics()

    call_kwargs = mock_glue.start_job_run.call_args.kwargs
    args = call_kwargs["Arguments"]
    assert "--sf_key_secret" not in args
    assert args["--target_table"] == "BRANDED_CAMPAIGN_METRICS"
    assert args["--pk_columns"] == "branded_campaign_id,snapshot_at"
    assert "branded_campaign_metrics" in args["--input_path"]


def test_export_branded_metrics_column_mapping():
    exporter = _load_exporter()

    mock_ddb = MagicMock()
    mock_table = MagicMock()
    mock_table.scan.return_value = {"Items": [_SAMPLE_METRIC]}
    mock_ddb.Table.return_value = mock_table
    mock_glue = MagicMock()
    captured_dfs = []

    def capture_parquet(df, **kwargs):
        captured_dfs.append(df)

    with (
        patch.dict(os.environ, _ENV),
        patch.object(exporter, "_ddb", mock_ddb),
        patch.object(exporter, "_glue", mock_glue),
        patch("branded_exporter.wr") as mock_wr,
    ):
        mock_wr.s3.to_parquet.side_effect = capture_parquet
        exporter.export_branded_metrics()

    df = captured_dfs[0]
    assert "branded_campaign_id" in df.columns
    assert "snapshot_at" in df.columns
    assert "contacts_placed" in df.columns
    assert "answer_rate" in df.columns
    assert "agents_staffed" in df.columns
    assert "extracted_at" in df.columns
    assert df.iloc[0]["branded_campaign_id"] == "cmp-1"
    assert df.iloc[0]["contacts_answered"] == 7


# ── Pagination ────────────────────────────────────────────────────────────────

def test_export_branded_runs_scan_follows_pagination():
    exporter = _load_exporter()

    mock_ddb = MagicMock()
    runs_table = MagicMock()
    metrics_table = MagicMock()

    run2 = {**_SAMPLE_RUN, "sk": "2026-01-01#cmp-2", "brandedCampaignId": "cmp-2"}
    runs_table.scan.side_effect = [
        {"Items": [_SAMPLE_RUN], "LastEvaluatedKey": {"planId": "plan-1", "sk": "2026-01-01#cmp-1"}},
        {"Items": [run2]},
    ]
    metrics_table.scan.return_value = {"Items": []}

    def table_factory(name):
        if "Metrics" in name:
            return metrics_table
        return runs_table

    mock_ddb.Table.side_effect = table_factory
    mock_glue = MagicMock()

    with (
        patch.dict(os.environ, _ENV),
        patch.object(exporter, "_ddb", mock_ddb),
        patch.object(exporter, "_glue", mock_glue),
        patch("branded_exporter.wr"),
    ):
        result = exporter.export_branded_runs()

    assert result["exported"] == 2
    assert runs_table.scan.call_count == 2
