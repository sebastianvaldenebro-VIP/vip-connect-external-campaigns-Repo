"""Targeted tests for executor._create_and_start_campaign's real body past the
cutoff guard (previously only exercised up to the _CutoffTooCloseError raise) —
success path (native + journey delivery), both missing-flow-arn ValueErrors,
and the legacy-bucket segment-filter branch.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

sys.modules.setdefault("vip_shared", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure.persistence", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure.persistence.audit", MagicMock())

import executor  # noqa: E402

_NOW_UTC = datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)  # 7am COT — safe daytime


def _stub_vip_shared(mock_oc):
    vip_stub = MagicMock()
    vip_stub.build.return_value = mock_oc
    modules_to_stub = [
        "vip_shared",
        "vip_shared.infrastructure",
        "vip_shared.infrastructure.persistence",
        "vip_shared.infrastructure.persistence.outbound_campaigns_client",
    ]
    originals = {m: sys.modules.get(m) for m in modules_to_stub}
    for m in modules_to_stub:
        sys.modules[m] = vip_stub
    return originals


def _unstub_vip_shared(originals):
    for m, orig in originals.items():
        if orig is None:
            sys.modules.pop(m, None)
        else:
            sys.modules[m] = orig


class TestCreateAndStartCampaignSuccess:
    def test_native_delivery_creates_and_starts(self):
        bucket = {"id": "B1", "name": "B1", "segmentFilters": {"state": ["TX"]}}
        campaign = {"id": "c1", "name": "TX-NL", "states": ["TX"], "run_type": "full"}
        mock_oc = MagicMock()
        mock_oc.create_campaign.return_value = {"id": "connect-9"}
        originals = _stub_vip_shared(mock_oc)
        try:
            with (
                patch("executor.resolve_campaign_flow_arn", return_value="arn:flow"),
                patch(
                    "executor.build_campaign_params",
                    return_value={"connectCampaignFlowArn": "arn:flow"},
                ),
                patch("executor._account_id", return_value="123456789012"),
            ):
                connect_id, seg_name = executor._create_and_start_campaign(
                    bucket, campaign, "arn:seg", "seg-name", _NOW_UTC
                )
        finally:
            _unstub_vip_shared(originals)

        assert connect_id == "connect-9"
        assert seg_name == "seg-name"
        mock_oc.start_campaign.assert_called_once_with("connect-9")

    def test_journey_delivery_resolves_journey_flow(self):
        bucket = {"id": "B1", "name": "B1", "segmentFilters": {"state": ["TX"]}}
        campaign = {
            "id": "c1", "name": "TX-J", "states": ["TX"], "deliveryType": "journey",
        }
        mock_oc = MagicMock()
        mock_oc.create_campaign.return_value = {"id": "connect-j9"}
        originals = _stub_vip_shared(mock_oc)
        try:
            with (
                patch("executor.resolve_journey_flow_arn", return_value="arn:journey-flow") as mock_journey,
                patch("executor.resolve_campaign_flow_arn") as mock_native,
                patch(
                    "executor.build_campaign_params",
                    return_value={"connectCampaignFlowArn": "arn:journey-flow"},
                ),
                patch("executor._account_id", return_value="123456789012"),
            ):
                connect_id, _seg_name = executor._create_and_start_campaign(
                    bucket, campaign, "arn:seg", "seg-name", _NOW_UTC
                )
        finally:
            _unstub_vip_shared(originals)

        assert connect_id == "connect-j9"
        mock_journey.assert_called_once()
        mock_native.assert_not_called()

    def test_legacy_bucket_skips_campaign_filter_translation(self):
        bucket = {"id": "B1", "name": "B1", "segmentFilters": {"state": ["NJ"]}}
        campaign = {"id": "c1", "name": "NJ-legacy", "_legacyBucket": True}
        mock_oc = MagicMock()
        mock_oc.create_campaign.return_value = {"id": "connect-legacy"}
        originals = _stub_vip_shared(mock_oc)
        try:
            with (
                patch("executor.resolve_campaign_flow_arn", return_value="arn:flow"),
                patch(
                    "executor.build_campaign_params",
                    return_value={"connectCampaignFlowArn": "arn:flow"},
                ),
                patch("executor._account_id", return_value="123456789012"),
                patch("executor.campaign_to_segment_filters") as mock_translate,
            ):
                executor._create_and_start_campaign(
                    bucket, campaign, "arn:seg", "seg-name", _NOW_UTC
                )
        finally:
            _unstub_vip_shared(originals)

        mock_translate.assert_not_called()


class TestCreateAndStartCampaignMissingFlowArn:
    def test_raises_value_error_for_missing_journey_flow(self):
        bucket = {"id": "B1", "name": "B1", "segmentFilters": {"state": ["TX"]}}
        campaign = {"id": "c1", "name": "TX-J", "states": ["TX"], "deliveryType": "journey"}
        mock_oc = MagicMock()
        originals = _stub_vip_shared(mock_oc)
        try:
            with (
                patch("executor.resolve_journey_flow_arn", return_value=None),
                patch("executor.build_campaign_params", return_value={}),
                patch("executor._account_id", return_value="123456789012"),
                pytest.raises(ValueError, match="Journey flow"),
            ):
                executor._create_and_start_campaign(
                    bucket, campaign, "arn:seg", "seg-name", _NOW_UTC
                )
        finally:
            _unstub_vip_shared(originals)

    def test_raises_value_error_for_missing_native_flow(self):
        bucket = {"id": "B1", "name": "B1", "segmentFilters": {"state": ["ZZ"]}}
        campaign = {"id": "c1", "name": "ZZ-NL", "states": ["ZZ"]}
        mock_oc = MagicMock()
        originals = _stub_vip_shared(mock_oc)
        try:
            with (
                patch("executor.resolve_campaign_flow_arn", return_value=None),
                patch("executor.build_campaign_params", return_value={}),
                patch("executor._account_id", return_value="123456789012"),
                pytest.raises(ValueError, match="No campaign flow ARN"),
            ):
                executor._create_and_start_campaign(
                    bucket, campaign, "arn:seg", "seg-name", _NOW_UTC
                )
        finally:
            _unstub_vip_shared(originals)
