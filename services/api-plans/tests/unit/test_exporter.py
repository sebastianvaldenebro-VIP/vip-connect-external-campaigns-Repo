"""Tests for exporter.py — Connect V2 campaign mapping export to Snowflake."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

# awswrangler is not installed in the unit-test env — stub it so the module
# can be imported and the wr.s3.to_parquet call can be mocked. pandas IS
# installed and used for real (df construction/dtype coercion is genuine
# behavior worth exercising, not something to fake away).
sys.modules.setdefault("awswrangler", MagicMock())
sys.modules.setdefault("awswrangler.s3", MagicMock())

_ENV = {
    "DATA_EXPORT_BUCKET": "test-bucket",
    "PLANS_TABLE_NAME": "VipAdminPlans",
    "GLUE_JOB_NAME": "specialOps-prod-snowflake-loader-glue",
    "SF_SRC_CFG_URI": "s3://config/sf.json",
    "SF_DATABASE": "PRD_RAW_DB",
    "SF_SCHEMA": "AMAZON_CONNECT",
}


def _load_exporter():
    with patch.dict(os.environ, _ENV):
        with patch("boto3.resource"), patch("boto3.client"):
            import importlib
            import exporter

            importlib.reload(exporter)
            return exporter


class TestExportCampaignMappings:
    def test_raises_when_bucket_not_configured(self, monkeypatch):
        exporter = _load_exporter()
        monkeypatch.setattr(exporter, "DATA_EXPORT_BUCKET", "")
        with pytest.raises(RuntimeError, match="DATA_EXPORT_BUCKET"):
            exporter.export_campaign_mappings()

    def test_returns_zero_when_no_campaigns_found(self):
        exporter = _load_exporter()
        with (
            patch.object(exporter, "_build_db_index", return_value={}),
            patch.object(exporter, "_list_connect_campaigns", return_value={}),
        ):
            result = exporter.export_campaign_mappings()
        assert result == {"exported": 0}

    def test_exports_merged_rows_and_triggers_glue(self):
        exporter = _load_exporter()
        db_index = {
            "cid-1": {
                "campaign_name": "Campaign One",
                "segment_name": "seg-1",
                "segment_arn": "arn:seg-1",
                "campaign_status": "completed",
                "exit_reason": "queue_drained",
                "started_at": "2026-01-01T10:00:00+00:00",
                "completed_at": "2026-01-01T11:00:00+00:00",
                "bucket_index": 0,
                "bucket_id": "b0",
                "bucket_name": "Bucket 0",
                "plan_id": "p1",
                "plan_name": "Plan One",
                "run_id": "r1",
                "run_date": "2026-01-01",
                "triggered_by": "manual",
            }
        }
        connect_campaigns = {
            "cid-1": {"name": "Connect Name", "segment_arn": "", "state": "Complete"},
            "cid-2": {"name": "External Campaign", "segment_arn": "arn:external", "state": "Running"},
        }
        # exporter.py does `import awswrangler as wr` *inside* the function
        # body (not at module scope), so it always resolves the single
        # sys.modules["awswrangler"] stub installed above — patch that
        # directly rather than a nonexistent "exporter.wr" module attribute.
        import awswrangler as wr

        wr.s3.to_parquet.reset_mock(return_value=True, side_effect=True)
        with (
            patch.object(exporter, "_build_db_index", return_value=db_index),
            patch.object(exporter, "_list_connect_campaigns", return_value=connect_campaigns),
            patch.object(exporter, "_enrich_external_campaigns"),
            patch.object(exporter, "_trigger_snowflake_load", return_value="glue-run-1"),
        ):
            result = exporter.export_campaign_mappings()

        assert result["exported"] == 2
        assert result["glue_run_id"] == "glue-run-1"
        wr.s3.to_parquet.assert_called_once()
        df = wr.s3.to_parquet.call_args.kwargs["df"]
        assert set(df["connect_campaign_id"]) == {"cid-1", "cid-2"}
        # External campaign (no db_index entry) gets triggered_by="external".
        ext_row = df[df["connect_campaign_id"] == "cid-2"].iloc[0]
        assert ext_row["triggered_by"] == "external"


class TestTriggerSnowflakeLoad:
    def test_returns_none_when_glue_job_name_not_set(self, monkeypatch):
        exporter = _load_exporter()
        monkeypatch.setattr(exporter, "GLUE_JOB_NAME", "")
        result = exporter._trigger_snowflake_load("2026-01-01")
        assert result is None

    def test_returns_none_when_src_cfg_uri_not_set(self, monkeypatch):
        exporter = _load_exporter()
        monkeypatch.setattr(exporter, "SF_SRC_CFG_URI", "")
        result = exporter._trigger_snowflake_load("2026-01-01")
        assert result is None

    def test_starts_glue_job_and_returns_run_id(self):
        exporter = _load_exporter()
        mock_glue = MagicMock()
        mock_glue.start_job_run.return_value = {"JobRunId": "run-123"}
        with patch("exporter.boto3.client", return_value=mock_glue):
            result = exporter._trigger_snowflake_load("2026-01-01")
        assert result == "run-123"
        call_kwargs = mock_glue.start_job_run.call_args.kwargs
        assert call_kwargs["JobName"] == "specialOps-prod-snowflake-loader-glue"
        assert call_kwargs["Arguments"]["--run_id"] == "2026-01-01"

    def test_returns_none_when_glue_start_fails(self):
        exporter = _load_exporter()
        mock_glue = MagicMock()
        mock_glue.start_job_run.side_effect = RuntimeError("Glue unavailable")
        with patch("exporter.boto3.client", return_value=mock_glue):
            result = exporter._trigger_snowflake_load("2026-01-01")
        assert result is None


class TestListConnectCampaigns:
    def test_returns_empty_dict_when_no_campaigns(self):
        exporter = _load_exporter()
        mock_client = MagicMock()
        mock_client.list_campaigns.return_value = {"campaignSummaryList": []}
        with patch("exporter.boto3.client", return_value=mock_client):
            result = exporter._list_connect_campaigns()
        assert result == {}
        mock_client.get_campaign_state.assert_not_called()

    def test_paginates_and_fetches_state_per_campaign(self):
        exporter = _load_exporter()
        mock_client = MagicMock()
        mock_client.list_campaigns.side_effect = [
            {
                "campaignSummaryList": [{"id": "c1", "name": "Campaign 1"}],
                "nextToken": "page2",
            },
            {"campaignSummaryList": [{"id": "c2", "name": "Campaign 2"}]},
        ]
        mock_client.get_campaign_state.side_effect = [
            {"state": "Running"},
            {"state": "Complete"},
        ]
        with patch("exporter.boto3.client", return_value=mock_client), patch("exporter.time.sleep"):
            result = exporter._list_connect_campaigns()

        assert result["c1"]["state"] == "Running"
        assert result["c2"]["state"] == "Complete"
        assert mock_client.list_campaigns.call_count == 2
        second_call_kwargs = mock_client.list_campaigns.call_args_list[1].kwargs
        assert second_call_kwargs["nextToken"] == "page2"

    def test_swallows_get_campaign_state_errors(self):
        exporter = _load_exporter()
        mock_client = MagicMock()
        mock_client.list_campaigns.return_value = {
            "campaignSummaryList": [{"id": "c1", "name": "Campaign 1"}]
        }
        mock_client.get_campaign_state.side_effect = RuntimeError("throttled")
        with patch("exporter.boto3.client", return_value=mock_client), patch("exporter.time.sleep"):
            result = exporter._list_connect_campaigns()
        assert result["c1"]["state"] == ""


class TestEnrichExternalCampaigns:
    def test_enriches_only_campaigns_missing_from_db_index(self):
        exporter = _load_exporter()
        connect_campaigns = {
            "c1": {"name": "In DB", "segment_arn": "", "state": ""},
            "c2": {"name": "External", "segment_arn": "", "state": ""},
        }
        db_index = {"c1": {}}
        mock_client = MagicMock()
        mock_client.describe_campaign.return_value = {
            "campaign": {"source": {"customerProfilesSegmentArn": "arn:c2-seg"}}
        }
        with patch("exporter.boto3.client", return_value=mock_client), patch("exporter.time.sleep"):
            exporter._enrich_external_campaigns(connect_campaigns, db_index)

        assert connect_campaigns["c2"]["segment_arn"] == "arn:c2-seg"
        mock_client.describe_campaign.assert_called_once_with(id="c2")

    def test_swallows_describe_campaign_errors(self):
        exporter = _load_exporter()
        connect_campaigns = {"c1": {"name": "External", "segment_arn": "", "state": ""}}
        mock_client = MagicMock()
        mock_client.describe_campaign.side_effect = RuntimeError("throttled")
        with patch("exporter.boto3.client", return_value=mock_client), patch("exporter.time.sleep"):
            exporter._enrich_external_campaigns(connect_campaigns, {})  # must not raise
        assert connect_campaigns["c1"]["segment_arn"] == ""


class TestGetLeadCountsCw:
    def test_returns_empty_dict_for_no_campaign_ids(self):
        exporter = _load_exporter()
        assert exporter._get_lead_counts_cw([]) == {}

    def test_returns_summed_delivery_values_per_campaign(self):
        exporter = _load_exporter()
        mock_cw = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {
                "MetricDataResults": [
                    {"Id": "m0", "Values": [3.0, 2.0]},
                    {"Id": "m1", "Values": []},
                ]
            }
        ]
        mock_cw.get_paginator.return_value = paginator
        with patch("exporter.boto3.client", return_value=mock_cw):
            result = exporter._get_lead_counts_cw(["c1", "c2"])
        assert result["c1"] == 5
        assert result["c2"] is None

    def test_batches_queries_in_groups_of_500(self):
        exporter = _load_exporter()
        mock_cw = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = []
        mock_cw.get_paginator.return_value = paginator
        campaign_ids = [f"c{i}" for i in range(600)]
        with patch("exporter.boto3.client", return_value=mock_cw):
            exporter._get_lead_counts_cw(campaign_ids)
        assert paginator.paginate.call_count == 2

    def test_swallows_cloudwatch_errors(self):
        exporter = _load_exporter()
        mock_cw = MagicMock()
        paginator = MagicMock()
        paginator.paginate.side_effect = RuntimeError("CloudWatch unavailable")
        mock_cw.get_paginator.return_value = paginator
        with patch("exporter.boto3.client", return_value=mock_cw):
            result = exporter._get_lead_counts_cw(["c1"])
        assert result == {"c1": None}


class TestBuildDbIndex:
    def test_scans_and_paginates(self):
        exporter = _load_exporter()
        mock_table = MagicMock()
        mock_table.scan.side_effect = [
            {
                "Items": [
                    {
                        "planId": "p1",
                        "runId": "r1",
                        "bucketStates": [
                            {
                                "bucketId": "b0",
                                "name": "Bucket 0",
                                "campaignStates": [
                                    {"connectCampaignId": "cid-1", "name": "C1", "status": "completed"}
                                ],
                            }
                        ],
                    }
                ],
                "LastEvaluatedKey": {"pk": "x"},
            },
            {"Items": []},
        ]
        mock_resource = MagicMock()
        mock_resource.Table.return_value = mock_table
        with patch("exporter.boto3.resource", return_value=mock_resource):
            index = exporter._build_db_index()

        assert "cid-1" in index
        assert mock_table.scan.call_count == 2


class TestIndexRun:
    def test_skips_campaign_states_without_connect_campaign_id(self):
        exporter = _load_exporter()
        item = {
            "planId": "p1",
            "runId": "r1",
            "bucketStates": [{"campaignStates": [{"name": "no id here"}]}],
        }
        index: dict = {}
        exporter._index_run(item, index)
        assert index == {}

    def test_indexes_campaign_with_started_at_and_metadata(self):
        exporter = _load_exporter()
        item = {
            "planId": "p1",
            "runId": "r1",
            "triggeredBy": "manual",
            "planSnapshot": {"name": "Plan One"},
            "startedAt": "2026-01-01T10:00:00+00:00",
            "bucketStates": [
                {
                    "bucketId": "b0",
                    "name": "Bucket 0",
                    "campaignStates": [
                        {
                            "connectCampaignId": "cid-1",
                            "name": "Campaign 1",
                            "segmentName": "seg-1",
                            "segmentArn": "arn:seg-1",
                            "status": "completed",
                            "exitReason": "queue_drained",
                            "startedAt": "2026-01-01T10:00:00+00:00",
                            "completedAt": "2026-01-01T11:00:00+00:00",
                        }
                    ],
                }
            ],
        }
        index: dict = {}
        exporter._index_run(item, index)
        assert index["cid-1"]["plan_name"] == "Plan One"
        assert index["cid-1"]["bucket_id"] == "b0"
        assert index["cid-1"]["run_date"] == "2026-01-01"

    def test_keeps_more_recent_run_when_campaign_id_reused(self):
        exporter = _load_exporter()
        index = {
            "cid-1": {
                "started_at": "2026-02-01T00:00:00+00:00",
                "campaign_name": "Newer run",
            }
        }
        older_item = {
            "planId": "p1",
            "runId": "r-old",
            "bucketStates": [
                {
                    "campaignStates": [
                        {
                            "connectCampaignId": "cid-1",
                            "name": "Older run",
                            "startedAt": "2026-01-01T00:00:00+00:00",
                        }
                    ]
                }
            ],
        }
        exporter._index_run(older_item, index)
        # The newer run's data must NOT be overwritten by the older one.
        assert index["cid-1"]["campaign_name"] == "Newer run"

    def test_overwrites_when_new_run_is_more_recent(self):
        exporter = _load_exporter()
        index = {
            "cid-1": {
                "started_at": "2026-01-01T00:00:00+00:00",
                "campaign_name": "Older run",
            }
        }
        newer_item = {
            "planId": "p1",
            "runId": "r-new",
            "bucketStates": [
                {
                    "campaignStates": [
                        {
                            "connectCampaignId": "cid-1",
                            "name": "Newer run",
                            "startedAt": "2026-02-01T00:00:00+00:00",
                        }
                    ]
                }
            ],
        }
        exporter._index_run(newer_item, index)
        assert index["cid-1"]["campaign_name"] == "Newer run"


class TestNormTs:
    def test_returns_none_for_none_input(self):
        exporter = _load_exporter()
        assert exporter._norm_ts(None) is None

    def test_converts_epoch_millis_decimal(self):
        exporter = _load_exporter()
        from decimal import Decimal

        result = exporter._norm_ts(Decimal("1735689600000"))
        assert result is not None
        assert result.startswith("2025-")

    def test_parses_iso_string_and_defaults_to_utc(self):
        exporter = _load_exporter()
        result = exporter._norm_ts("2026-01-01T10:00:00")
        assert "+00:00" in result

    def test_returns_none_for_empty_string(self):
        exporter = _load_exporter()
        assert exporter._norm_ts("   ") is None

    def test_returns_raw_string_when_unparseable(self):
        exporter = _load_exporter()
        assert exporter._norm_ts("not-a-timestamp") == "not-a-timestamp"

    def test_converts_epoch_millis_int(self):
        """int (not just Decimal) must also hit the numeric branch."""
        exporter = _load_exporter()
        result = exporter._norm_ts(0)
        assert result == "1970-01-01T00:00:00+00:00"


class TestCotDate:
    def test_converts_iso_string_to_cot_date(self):
        exporter = _load_exporter()
        # 10:00 UTC - 5h (COT, UTC-5, no DST) = 05:00 same calendar day.
        result = exporter._cot_date("2026-01-01T10:00:00+00:00")
        assert result == "2026-01-01"

    def test_converts_iso_string_crossing_to_prior_cot_day(self):
        exporter = _load_exporter()
        # 03:00 UTC - 5h = 22:00 the PREVIOUS day in COT.
        result = exporter._cot_date("2026-01-02T03:00:00+00:00")
        assert result == "2026-01-01"

    def test_converts_epoch_millis_decimal_to_cot_date(self):
        exporter = _load_exporter()
        from decimal import Decimal

        result = exporter._cot_date(Decimal("1735732800000"))
        assert len(result) == 10  # YYYY-MM-DD shape

    def test_returns_empty_string_when_unparseable(self):
        exporter = _load_exporter()
        assert exporter._cot_date("not-a-timestamp") == ""

    def test_naive_iso_string_defaults_to_utc_before_cot_conversion(self):
        exporter = _load_exporter()
        result = exporter._cot_date("2026-01-01T10:00:00")  # no tzinfo
        assert result == "2026-01-01"
