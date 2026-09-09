"""Tests for handlers/branded.py's non-roster endpoints and small helpers:
get_today_summary, get_campaign_metrics, get_history, _parse_ts,
_agent_display_name, and _status_type_for_arn's error path.

handlers.branded reads BRANDED_RUN_SUMMARY_TABLE / BRANDED_CAMPAIGN_METRICS_TABLE /
CONNECT_INSTANCE_ID as module-level constants at import time, so — same as
test_branded_roster.py — monkeypatch.setattr on the module is used instead of
(or alongside) monkeypatch.setenv to keep these tests independent of import order.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    from handlers import branded

    monkeypatch.setattr(branded, "_RUN_SUMMARY_TABLE", "VipBrandedRunSummary")
    monkeypatch.setattr(branded, "_METRICS_TABLE", "VipBrandedCampaignMetrics")
    monkeypatch.setattr(branded, "_CONNECT_INSTANCE_ID", "instance-1")
    branded._user_name_cache.clear()
    branded._status_type_cache.clear()
    yield
    branded._user_name_cache.clear()
    branded._status_type_cache.clear()


class TestGetTodaySummary:
    def test_returns_503_when_table_not_configured(self, monkeypatch):
        from handlers import branded

        monkeypatch.setattr(branded, "_RUN_SUMMARY_TABLE", "")
        response = branded.get_today_summary({"queryStringParameters": {}}, {})
        assert response["statusCode"] == 503

    def test_aggregates_counts_for_the_given_date(self):
        from handlers import branded

        mock_table = MagicMock()
        mock_table.scan.return_value = {
            "Items": [
                {"status": "RUNNING", "totalDialed": 10},
                {"status": "COMPLETED", "totalDialed": 20},
                {"status": "RUNNING", "totalDialed": "5"},
            ]
        }
        mock_ddb = MagicMock()
        mock_ddb.Table.return_value = mock_table

        with patch.object(branded, "_ddb", mock_ddb):
            response = branded.get_today_summary(
                {"queryStringParameters": {"date": "2026-08-27"}}, {}
            )

        body = json.loads(response["body"])
        assert body["date"] == "2026-08-27"
        assert body["total"] == 3
        assert body["active"] == 2
        assert body["completed"] == 1
        assert body["contactsDialed"] == 35

    def test_defaults_to_today_when_date_not_provided(self):
        from handlers import branded

        mock_table = MagicMock()
        mock_table.scan.return_value = {"Items": []}
        mock_ddb = MagicMock()
        mock_ddb.Table.return_value = mock_table

        with patch.object(branded, "_ddb", mock_ddb):
            response = branded.get_today_summary({"queryStringParameters": {}}, {})

        body = json.loads(response["body"])
        assert body["date"] == branded._today_str()

    def test_paginates_through_all_scan_pages(self):
        from handlers import branded

        mock_table = MagicMock()
        mock_table.scan.side_effect = [
            {
                "Items": [{"status": "RUNNING", "totalDialed": 1}],
                "LastEvaluatedKey": {"pk": "x"},
            },
            {"Items": [{"status": "COMPLETED", "totalDialed": 2}]},
        ]
        mock_ddb = MagicMock()
        mock_ddb.Table.return_value = mock_table

        with patch.object(branded, "_ddb", mock_ddb):
            response = branded.get_today_summary(
                {"queryStringParameters": {"date": "2026-08-27"}}, {}
            )

        assert mock_table.scan.call_count == 2
        body = json.loads(response["body"])
        assert body["total"] == 2


class TestGetCampaignMetrics:
    def test_requires_branded_campaign_id_path_param(self):
        from handlers import branded

        response = branded.get_campaign_metrics({"pathParameters": {}}, {})
        assert response["statusCode"] == 400

    def test_returns_503_when_table_not_configured(self, monkeypatch):
        from handlers import branded

        monkeypatch.setattr(branded, "_METRICS_TABLE", "")
        response = branded.get_campaign_metrics(
            {"pathParameters": {"brandedCampaignId": "bc-1"}}, {}
        )
        assert response["statusCode"] == 503

    def test_returns_time_series_snapshots(self):
        from handlers import branded

        mock_table = MagicMock()
        mock_table.query.return_value = {
            "Items": [{"snapshotAt": "2026-08-27T15:00:00Z", "contactsPlaced": 10}]
        }
        mock_ddb = MagicMock()
        mock_ddb.Table.return_value = mock_table

        with patch.object(branded, "_ddb", mock_ddb):
            response = branded.get_campaign_metrics(
                {
                    "pathParameters": {"brandedCampaignId": "bc-1"},
                    "queryStringParameters": {"limit": "10"},
                },
                {},
            )

        body = json.loads(response["body"])
        assert body["campaignId"] == "bc-1"
        assert len(body["metrics"]) == 1
        call_kwargs = mock_table.query.call_args.kwargs
        assert call_kwargs["Limit"] == 10
        assert call_kwargs["ScanIndexForward"] is False

    def test_limit_is_capped_at_100(self):
        from handlers import branded

        mock_table = MagicMock()
        mock_table.query.return_value = {"Items": []}
        mock_ddb = MagicMock()
        mock_ddb.Table.return_value = mock_table

        with patch.object(branded, "_ddb", mock_ddb):
            branded.get_campaign_metrics(
                {
                    "pathParameters": {"brandedCampaignId": "bc-1"},
                    "queryStringParameters": {"limit": "500"},
                },
                {},
            )

        assert mock_table.query.call_args.kwargs["Limit"] == 100


class TestGetHistory:
    def test_returns_503_when_table_not_configured(self, monkeypatch):
        from handlers import branded

        monkeypatch.setattr(branded, "_RUN_SUMMARY_TABLE", "")
        response = branded.get_history({"queryStringParameters": {"planId": "p-1"}}, {})
        assert response["statusCode"] == 503

    def test_requires_plan_id(self):
        from handlers import branded

        response = branded.get_history({"queryStringParameters": {}}, {})
        assert response["statusCode"] == 400

    def test_returns_history_capped_at_90_days(self):
        from handlers import branded

        mock_table = MagicMock()
        mock_table.query.return_value = {
            "Items": [{"startedAt": "2026-08-01T00:00:00+00:00"}]
        }
        mock_ddb = MagicMock()
        mock_ddb.Table.return_value = mock_table

        with patch.object(branded, "_ddb", mock_ddb):
            response = branded.get_history(
                {"queryStringParameters": {"planId": "p-1", "days": "365"}}, {}
            )

        body = json.loads(response["body"])
        assert body["planId"] == "p-1"
        assert body["days"] == 90
        assert len(body["history"]) == 1


class TestParseTs:
    def test_returns_none_for_falsy_input(self):
        from handlers import branded

        assert branded._parse_ts(None) is None
        assert branded._parse_ts("") is None

    def test_returns_datetime_unchanged(self):
        from handlers import branded
        from datetime import datetime, timezone

        dt = datetime(2026, 8, 27, 15, 0, tzinfo=timezone.utc)
        assert branded._parse_ts(dt) is dt

    def test_parses_iso_string(self):
        from handlers import branded

        result = branded._parse_ts("2026-08-27T15:00:00+00:00")
        assert result is not None
        assert result.year == 2026

    def test_returns_none_for_malformed_string(self):
        from handlers import branded

        assert branded._parse_ts("not-a-timestamp") is None


class TestAgentDisplayName:
    def test_returns_empty_string_for_falsy_user_id(self):
        from handlers import branded

        assert branded._agent_display_name("") == ""
        assert branded._agent_display_name(None) == ""

    def test_returns_cached_name_without_calling_describe_user(self):
        from handlers import branded

        branded._user_name_cache["u-1"] = "Cached Name"
        mock_connect = MagicMock()

        with patch.object(branded, "_connect", mock_connect):
            result = branded._agent_display_name("u-1")

        assert result == "Cached Name"
        mock_connect.describe_user.assert_not_called()

    def test_builds_name_from_first_and_last_name(self):
        from handlers import branded

        mock_connect = MagicMock()
        mock_connect.describe_user.return_value = {
            "User": {
                "IdentityInfo": {"FirstName": "Alex", "LastName": "Doe"},
                "Username": "adoe",
            }
        }

        with patch.object(branded, "_connect", mock_connect):
            result = branded._agent_display_name("u-1")

        assert result == "Alex Doe"
        assert branded._user_name_cache["u-1"] == "Alex Doe"

    def test_falls_back_to_username_when_no_name(self):
        from handlers import branded

        mock_connect = MagicMock()
        mock_connect.describe_user.return_value = {
            "User": {"IdentityInfo": {}, "Username": "adoe"}
        }

        with patch.object(branded, "_connect", mock_connect):
            result = branded._agent_display_name("u-1")

        assert result == "adoe"

    def test_falls_back_to_user_id_when_describe_user_fails(self):
        from handlers import branded

        mock_connect = MagicMock()
        mock_connect.describe_user.side_effect = RuntimeError("AccessDenied")

        with patch.object(branded, "_connect", mock_connect):
            result = branded._agent_display_name("u-1")

        assert result == "u-1"
        assert branded._user_name_cache["u-1"] == "u-1"


class TestStatusTypeForArnErrorHandling:
    def test_swallows_error_and_falls_back_to_custom(self):
        from handlers import branded

        mock_connect = MagicMock()
        mock_connect.list_agent_statuses.side_effect = RuntimeError("Throttled")

        with patch.object(branded, "_connect", mock_connect):
            result = branded._status_type_for_arn("arn:.../agent-status/s-1")

        assert result == "CUSTOM"
