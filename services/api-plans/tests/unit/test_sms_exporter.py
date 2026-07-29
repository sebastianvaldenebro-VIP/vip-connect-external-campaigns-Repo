"""Tests for sms_exporter.py."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

# awswrangler and pandas are not installed in the unit-test env — stub them so
# the module can be imported and the wr.s3.to_parquet call can be mocked.
sys.modules.setdefault("awswrangler", MagicMock())
sys.modules.setdefault("awswrangler.s3", MagicMock())

_ENV = {
    "SMS_CAMPAIGN_RUNS_TABLE": "VipSmsCampaignRuns",
    "DATA_EXPORT_BUCKET": "test-bucket",
    "GLUE_JOB_NAME": "specialOps-prod-snowflake-loader-glue",
    "SF_SRC_CFG_URI": "s3://config/sf.json",
    "SF_DATABASE": "PRD_RAW_DB",
    "SF_SCHEMA": "AMAZON_CONNECT",
    "SF_KEY_SECRET": "specialops-prod-snowflake-etl-ingestor",
}

_SAMPLE_ITEM = {
    "planId": "plan-1",
    "sk": "run-1#cmp-1",
    "smsCampaignId": "cmp-1",
    "planName": "Test Plan",
    "segmentName": "seg-1",
    "segmentArn": "arn:aws:profile:us-east-1:123:domains/d/segment-definitions/seg-1",
    "messageTemplate": "Your appointment is confirmed. Reply STOP to opt out.",
    "originationNumberArn": "arn:aws:sms-voice:us-east-1:123:phone-number/p-1",
    "originationNumber": "+15125551111",
    "totalEnqueued": 10,
    "totalSent": 9,
    "totalFailed": 1,
    "totalOptedOut": 0,
    "status": "COMPLETED",
    "startedAt": "2026-01-01T10:00:00+00:00",
    "completedAt": "2026-01-01T10:05:00+00:00",
    "exitReason": "queue_drained",
    "pipelineVersion": "v1",
    "createdAt": "2026-01-01T10:00:00+00:00",
    "updatedAt": "2026-01-01T10:05:00+00:00",
}


def _load_exporter():
    with patch.dict(os.environ, _ENV):
        with patch("boto3.resource"), patch("boto3.client"):
            import importlib
            import sms_exporter
            importlib.reload(sms_exporter)
            return sms_exporter


# ── Empty scan returns without calling Glue ───────────────────────────────────


def test_export_empty_scan_returns_zero_without_glue():
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
        result = exporter.export_sms_runs()

    assert result == {"exported": 0, "table": "SMS_CAMPAIGN_RUNS"}
    mock_glue.start_job_run.assert_not_called()


# ── Items found: Parquet written + Glue triggered ─────────────────────────────


def test_export_with_items_triggers_glue():
    exporter = _load_exporter()

    mock_ddb = MagicMock()
    mock_table = MagicMock()
    mock_table.scan.return_value = {"Items": [_SAMPLE_ITEM]}
    mock_ddb.Table.return_value = mock_table
    mock_glue = MagicMock()

    with (
        patch.dict(os.environ, _ENV),
        patch.object(exporter, "_ddb", mock_ddb),
        patch.object(exporter, "_glue", mock_glue),
        patch("sms_exporter.wr") as mock_wr,
    ):
        result = exporter.export_sms_runs()

    assert result["exported"] == 1
    assert result["table"] == "SMS_CAMPAIGN_RUNS"
    mock_glue.start_job_run.assert_called_once()
    mock_wr.s3.to_parquet.assert_called_once()


def test_export_glue_job_has_correct_arguments():
    exporter = _load_exporter()

    mock_ddb = MagicMock()
    mock_table = MagicMock()
    mock_table.scan.return_value = {"Items": [_SAMPLE_ITEM]}
    mock_ddb.Table.return_value = mock_table
    mock_glue = MagicMock()

    with (
        patch.dict(os.environ, _ENV),
        patch.object(exporter, "_ddb", mock_ddb),
        patch.object(exporter, "_glue", mock_glue),
        patch("sms_exporter.wr"),
    ):
        exporter.export_sms_runs()

    call_kwargs = mock_glue.start_job_run.call_args.kwargs
    args = call_kwargs["Arguments"]
    assert args["--sfDatabase"] == "PRD_RAW_DB"
    assert args["--sfSchema"] == "AMAZON_CONNECT"
    assert args["--target_table"] == "SMS_CAMPAIGN_RUNS"
    assert args["--pk_columns"] == "plan_id,sk"
    assert args["--load_type"] == "merge"
    assert args["--sf_key_secret"] == "specialops-prod-snowflake-etl-ingestor"
    assert "sms_campaign_runs" in args["--input_path"]


# ── Column mapping ────────────────────────────────────────────────────────────


def test_export_column_mapping_correct():
    """DDB field names are correctly snake_cased in the Parquet DataFrame."""
    exporter = _load_exporter()

    mock_ddb = MagicMock()
    mock_table = MagicMock()
    mock_table.scan.return_value = {"Items": [_SAMPLE_ITEM]}
    mock_ddb.Table.return_value = mock_table
    mock_glue = MagicMock()
    captured_dfs = []

    def capture_parquet(df, **kwargs):
        captured_dfs.append(df)

    with (
        patch.dict(os.environ, _ENV),
        patch.object(exporter, "_ddb", mock_ddb),
        patch.object(exporter, "_glue", mock_glue),
        patch("sms_exporter.wr") as mock_wr,
    ):
        mock_wr.s3.to_parquet.side_effect = capture_parquet
        exporter.export_sms_runs()

    assert len(captured_dfs) == 1
    df = captured_dfs[0]
    # Verify key column mappings
    assert "plan_id" in df.columns
    assert "sms_campaign_id" in df.columns
    assert "total_enqueued" in df.columns
    assert "total_sent" in df.columns
    assert "total_failed" in df.columns
    assert "total_opted_out" in df.columns
    assert "extracted_at" in df.columns
    # Verify values
    assert df.iloc[0]["plan_id"] == "plan-1"
    assert df.iloc[0]["sms_campaign_id"] == "cmp-1"
    assert df.iloc[0]["total_sent"] == 9


# ── Pagination: LastEvaluatedKey ──────────────────────────────────────────────


def test_scan_follows_pagination():
    """_scan_table continues scanning when LastEvaluatedKey is present."""
    exporter = _load_exporter()

    mock_ddb = MagicMock()
    mock_table = MagicMock()
    page2_item = {**_SAMPLE_ITEM, "sk": "run-2#cmp-2", "smsCampaignId": "cmp-2"}
    mock_table.scan.side_effect = [
        {"Items": [_SAMPLE_ITEM], "LastEvaluatedKey": {"planId": "plan-1", "sk": "run-1#cmp-1"}},
        {"Items": [page2_item]},
    ]
    mock_ddb.Table.return_value = mock_table
    mock_glue = MagicMock()

    with (
        patch.dict(os.environ, _ENV),
        patch.object(exporter, "_ddb", mock_ddb),
        patch.object(exporter, "_glue", mock_glue),
        patch("sms_exporter.wr"),
    ):
        result = exporter.export_sms_runs()

    assert result["exported"] == 2
    assert mock_table.scan.call_count == 2


# ── _scan_table with empty table name ────────────────────────────────────────


def test_scan_table_empty_name_returns_empty():
    exporter = _load_exporter()
    with patch.dict(os.environ, _ENV):
        result = exporter._scan_table("")
    assert result == []
