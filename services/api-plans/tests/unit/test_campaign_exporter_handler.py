"""Tests for campaign_exporter_handler.py.

Audit follow-up (2026-08-21): this handler used to catch every exception and
return {"ok": False, "error": ...} instead of raising — so the Lambda's own
AWS/Lambda Errors metric stayed at 0 for 14+ days of 100% export failures,
with no alarm able to ever detect it. These tests lock in the fix: every
action branch must re-raise so the standard per-function Errors alarm fires.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

_stub_modules = {
    "vip_shared": MagicMock(),
    "vip_shared.infrastructure": MagicMock(),
    "vip_shared.infrastructure.telemetry": MagicMock(),
    "vip_shared.infrastructure.telemetry.structured_logger": MagicMock(),
    "exporter": MagicMock(),
}
with patch.dict(sys.modules, _stub_modules):
    import campaign_exporter_handler  # noqa: E402


def test_campaign_export_reraises_instead_of_swallowing():
    with patch.object(
        campaign_exporter_handler.exporter,
        "export_campaign_mappings",
        side_effect=RuntimeError("KMS access denied"),
    ):
        with pytest.raises(RuntimeError, match="KMS access denied"):
            campaign_exporter_handler.lambda_handler({"action": "campaign_export"}, None)


def test_branded_export_reraises_instead_of_swallowing():
    branded_exporter_mock = MagicMock()
    branded_exporter_mock.export_branded_runs.side_effect = RuntimeError(
        "KMS access denied"
    )
    with patch.dict(sys.modules, {"branded_exporter": branded_exporter_mock}):
        with pytest.raises(RuntimeError, match="KMS access denied"):
            campaign_exporter_handler.lambda_handler({"action": "branded_export"}, None)


def test_sms_export_reraises_instead_of_swallowing():
    sms_exporter_mock = MagicMock()
    sms_exporter_mock.export_sms_runs.side_effect = RuntimeError("KMS access denied")
    with patch.dict(sys.modules, {"sms_exporter": sms_exporter_mock}):
        with pytest.raises(RuntimeError, match="KMS access denied"):
            campaign_exporter_handler.lambda_handler({"action": "sms_export"}, None)


def test_campaign_export_still_returns_ok_on_success():
    with patch.object(
        campaign_exporter_handler.exporter,
        "export_campaign_mappings",
        return_value={"exported": 5},
    ):
        result = campaign_exporter_handler.lambda_handler(
            {"action": "campaign_export"}, None
        )
    assert result == {"ok": True, "exported": 5}
