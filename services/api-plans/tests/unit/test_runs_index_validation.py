"""Tests for bucketIndex/campaignIndex bounds validation in handlers/runs.py.

Audit finding #018: a negative index previously indexed from the end of the
list silently, an out-of-range positive index raised an unguarded
IndexError (500), and a non-numeric value raised a bare ValueError (500).
"""

from __future__ import annotations

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
        "bucketStates": [
            {"campaignStates": [{"campaignId": f"c{j}"} for j in range(n)]}
            for n in campaign_counts
        ],
    }


def _load_runs_module():
    with patch.dict(sys.modules, _stub_modules):
        import importlib

        import handlers.runs as runs_mod

        importlib.reload(runs_mod)
        runs_mod.executor.reset_mock()
        runs_mod.store.reset_mock()
        runs_mod.json_response = lambda code, body: {"statusCode": code, "body": body}
        runs_mod.extract_caller.return_value = MagicMock(
            sub="s", email="e", ip_address="1.2.3.4", user_agent="ua"
        )
        return runs_mod


def test_force_start_bucket_rejects_negative_index():
    # ValueError is raised, not returned — the outer Lambda router (handler.py)
    # converts it to an HTTP 400, matching this module's existing convention
    # (e.g. trigger_run's "already has an active run" ValueError).
    runs_mod = _load_runs_module()
    runs_mod.store.get_run.return_value = _make_run(3)

    with pytest.raises(ValueError, match="bucketIndex"):
        runs_mod.force_start_bucket(
            {}, {"id": "plan-1", "runId": "run-1", "bucketIndex": "-1"}
        )
    runs_mod.executor.force_start_bucket.assert_not_called()


def test_force_start_bucket_rejects_out_of_range_index():
    runs_mod = _load_runs_module()
    runs_mod.store.get_run.return_value = _make_run(3)

    with pytest.raises(ValueError, match="bucketIndex"):
        runs_mod.force_start_bucket(
            {}, {"id": "plan-1", "runId": "run-1", "bucketIndex": "99"}
        )
    runs_mod.executor.force_start_bucket.assert_not_called()


def test_force_start_bucket_rejects_non_numeric_index():
    runs_mod = _load_runs_module()
    runs_mod.store.get_run.return_value = _make_run(3)

    with pytest.raises(ValueError, match="bucketIndex"):
        runs_mod.force_start_bucket(
            {}, {"id": "plan-1", "runId": "run-1", "bucketIndex": "not-a-number"}
        )
    runs_mod.executor.force_start_bucket.assert_not_called()


def test_force_start_bucket_404s_when_run_missing():
    runs_mod = _load_runs_module()
    runs_mod.store.get_run.return_value = None

    result = runs_mod.force_start_bucket(
        {}, {"id": "plan-1", "runId": "missing-run", "bucketIndex": "0"}
    )

    assert result["statusCode"] == 404
    runs_mod.executor.force_start_bucket.assert_not_called()


def test_force_start_bucket_accepts_valid_index():
    runs_mod = _load_runs_module()
    runs_mod.store.get_run.return_value = _make_run(3)
    runs_mod.executor.force_start_bucket.return_value = {"runId": "run-1"}

    result = runs_mod.force_start_bucket(
        {}, {"id": "plan-1", "runId": "run-1", "bucketIndex": "1"}
    )

    assert result["statusCode"] == 200
    runs_mod.executor.force_start_bucket.assert_called_once_with("plan-1", "run-1", 1)


def test_force_start_campaign_rejects_out_of_range_campaign_index():
    runs_mod = _load_runs_module()
    runs_mod.store.get_run.return_value = _make_run(2, campaign_counts=[2, 0])

    with pytest.raises(ValueError, match="campaignIndex"):
        runs_mod.force_start_campaign(
            {},
            {
                "id": "plan-1",
                "runId": "run-1",
                "bucketIndex": "0",
                "campaignIndex": "5",
            },
        )
    runs_mod.executor.force_start_campaign.assert_not_called()


def test_force_start_campaign_accepts_valid_indices():
    runs_mod = _load_runs_module()
    runs_mod.store.get_run.return_value = _make_run(2, campaign_counts=[2, 1])
    runs_mod.executor.force_start_campaign.return_value = {"runId": "run-1"}

    result = runs_mod.force_start_campaign(
        {},
        {
            "id": "plan-1",
            "runId": "run-1",
            "bucketIndex": "1",
            "campaignIndex": "0",
        },
    )

    assert result["statusCode"] == 200
    runs_mod.executor.force_start_campaign.assert_called_once_with(
        "plan-1", "run-1", 1, 0
    )


def test_skip_campaign_rejects_negative_bucket_index():
    runs_mod = _load_runs_module()
    runs_mod.store.get_run.return_value = _make_run(2, campaign_counts=[2, 1])

    with pytest.raises(ValueError, match="bucketIndex"):
        runs_mod.skip_campaign(
            {},
            {
                "id": "plan-1",
                "runId": "run-1",
                "bucketIndex": "-2",
                "campaignIndex": "0",
            },
        )
    runs_mod.executor.skip_campaign.assert_not_called()
