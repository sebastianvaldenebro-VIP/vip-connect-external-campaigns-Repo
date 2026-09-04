"""Tests for branded_progress handler in handlers/runs.py."""
from __future__ import annotations

import sys
import os
from unittest.mock import MagicMock, patch


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


def _make_run(campaign_states_per_bucket):
    """Helper to build a minimal run dict."""
    return {
        "planId": "plan-1",
        "runId": "run-1",
        "status": "running",
        "bucketStates": [
            {"campaignStates": cs_list}
            for cs_list in campaign_states_per_bucket
        ],
    }


def test_returns_counts_for_branded_campaign():
    with patch.dict(sys.modules, _stub_modules):
        import importlib
        import handlers.runs as runs_mod
        importlib.reload(runs_mod)

        runs_mod.executor.reset_mock()
        runs_mod.store.get_run.return_value = _make_run([
            [
                {"campaignId": "NY-NL_13", "brandedCampaignId": "abc-123", "status": "running"},
                {"campaignId": "NJ-NL_5", "status": "running"},  # no brandedCampaignId
            ]
        ])
        runs_mod.executor.get_branded_queue_counts.side_effect = None
        runs_mod.executor.get_branded_queue_counts.return_value = (20, 12)

        runs_mod.json_response = lambda code, body: {"statusCode": code, "body": body}

        event = {}
        result = runs_mod.branded_progress(event, {"id": "plan-1", "runId": "run-1"})

        assert result["statusCode"] == 200
        body = result["body"]
        assert "NY-NL_13" in body["progress"]
        assert body["progress"]["NY-NL_13"] == {"pending": 20, "dialed": 12, "total": 32}
        assert "NJ-NL_5" not in body["progress"]
        runs_mod.executor.get_branded_queue_counts.assert_called_once_with("abc-123")


def test_returns_404_when_run_not_found():
    with patch.dict(sys.modules, _stub_modules):
        import importlib
        import handlers.runs as runs_mod
        importlib.reload(runs_mod)

        runs_mod.store.get_run.return_value = None
        runs_mod.json_response = lambda code, body: {"statusCode": code, "body": body}

        result = runs_mod.branded_progress({}, {"id": "plan-1", "runId": "missing"})
        assert result["statusCode"] == 404


def test_returns_empty_progress_when_no_branded_campaigns():
    with patch.dict(sys.modules, _stub_modules):
        import importlib
        import handlers.runs as runs_mod
        importlib.reload(runs_mod)

        runs_mod.store.get_run.return_value = _make_run([
            [{"campaignId": "NJ-NL_5", "status": "running"}]
        ])
        runs_mod.json_response = lambda code, body: {"statusCode": code, "body": body}

        result = runs_mod.branded_progress({}, {"id": "plan-1", "runId": "run-1"})
        assert result["statusCode"] == 200
        assert result["body"]["progress"] == {}


def test_counts_error_treated_as_zero():
    """If get_branded_queue_counts raises, that campaign is skipped — no 500."""
    with patch.dict(sys.modules, _stub_modules):
        import importlib
        import handlers.runs as runs_mod
        importlib.reload(runs_mod)

        runs_mod.store.get_run.return_value = _make_run([
            [{"campaignId": "NY-NL_13", "brandedCampaignId": "abc-123", "status": "running"}]
        ])
        runs_mod.executor.get_branded_queue_counts.side_effect = Exception("DDB error")
        runs_mod.json_response = lambda code, body: {"statusCode": code, "body": body}

        result = runs_mod.branded_progress({}, {"id": "plan-1", "runId": "run-1"})
        assert result["statusCode"] == 200
        # Campaign skipped on error — not included in progress, no 500
        assert result["body"]["progress"] == {}
