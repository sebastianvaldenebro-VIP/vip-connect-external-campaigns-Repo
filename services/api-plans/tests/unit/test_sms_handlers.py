"""Tests for handlers/sms.py — origination number list + SMS run history."""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

_stub_modules = {
    "store": MagicMock(),
    "scheduler_manager": MagicMock(),
    "vip_shared": MagicMock(),
    "vip_shared.application": MagicMock(),
    "vip_shared.application.http": MagicMock(),
    "vip_shared.infrastructure": MagicMock(),
    "vip_shared.infrastructure.persistence": MagicMock(),
    "vip_shared.infrastructure.persistence.audit": MagicMock(),
}
with patch.dict(sys.modules, _stub_modules):
    with patch("boto3.client"), patch("boto3.resource"):
        from handlers import sms as sms_handler  # noqa: E402

# json_response was stubbed as MagicMock — replace with a real implementation
# so tests can parse the returned body.
import json as _json  # noqa: E402

sms_handler.json_response = lambda status_code, data: {  # type: ignore[attr-defined]
    "statusCode": status_code,
    "body": _json.dumps(data),
}


# ── list_origination_numbers ──────────────────────────────────────────────────


def _make_phone_number(arn: str, number: str, number_type: str = "TEN_DLC") -> dict:
    return {
        "PhoneNumberArn": arn,
        "PhoneNumber": number,
        "NumberType": number_type,
        "CountryCode": "US",
        "TwoWayEnabled": False,
        "OptOutListName": "vip-sms-opt-out",
        "Status": "ACTIVE",
    }


def _paginator(pages: list[list[dict]]) -> MagicMock:
    pager = MagicMock()
    pager.paginate.return_value = [
        {"PhoneNumbers": page} for page in pages
    ]
    return pager


def test_list_origination_numbers_returns_all_pages():
    numbers = [
        _make_phone_number("arn:aws:sms-voice:us-east-1:123:phone-number/p-1", "+15125551111"),
        _make_phone_number("arn:aws:sms-voice:us-east-1:123:phone-number/p-2", "+15125552222", "TOLL_FREE"),
    ]
    mock_client = MagicMock()
    mock_client.get_paginator.return_value = _paginator([[numbers[0]], [numbers[1]]])

    with patch.object(sms_handler, "_sms", mock_client):
        resp = sms_handler.list_origination_numbers({}, {})

    body = json.loads(resp["body"])
    assert resp["statusCode"] == 200
    assert len(body["originationNumbers"]) == 2
    assert body["originationNumbers"][0]["phoneNumber"] == "+15125551111"
    assert body["originationNumbers"][1]["numberType"] == "TOLL_FREE"


def test_list_origination_numbers_empty():
    mock_client = MagicMock()
    mock_client.get_paginator.return_value = _paginator([[]])

    with patch.object(sms_handler, "_sms", mock_client):
        resp = sms_handler.list_origination_numbers({}, {})

    body = json.loads(resp["body"])
    assert resp["statusCode"] == 200
    assert body["originationNumbers"] == []


def test_list_origination_numbers_fields():
    numbers = [_make_phone_number("arn:1", "+15125551234", "LONG_CODE")]
    mock_client = MagicMock()
    mock_client.get_paginator.return_value = _paginator([numbers])

    with patch.object(sms_handler, "_sms", mock_client):
        resp = sms_handler.list_origination_numbers({}, {})

    body = json.loads(resp["body"])
    num = body["originationNumbers"][0]
    assert num["arn"] == "arn:1"
    assert num["phoneNumber"] == "+15125551234"
    assert num["numberType"] == "LONG_CODE"
    assert num["countryCode"] == "US"
    assert num["status"] == "ACTIVE"
    assert num["twoWayEnabled"] is False
    assert num["optOutListName"] == "vip-sms-opt-out"


def test_list_origination_numbers_filter_active_sent_to_paginator():
    mock_client = MagicMock()
    mock_client.get_paginator.return_value = _paginator([[]])

    with patch.object(sms_handler, "_sms", mock_client):
        sms_handler.list_origination_numbers({}, {})

    mock_client.get_paginator.assert_called_once_with("describe_phone_numbers")
    call_kwargs = mock_client.get_paginator.return_value.paginate.call_args
    filters = call_kwargs.kwargs.get("Filters") or call_kwargs.args[0] if call_kwargs.args else call_kwargs.kwargs["Filters"]
    assert any(f["Name"] == "status" and "ACTIVE" in f["Values"] for f in filters)


# ── get_sms_runs ──────────────────────────────────────────────────────────────


def test_get_sms_runs_returns_items_for_plan():
    mock_table = MagicMock()
    mock_table.query.return_value = {
        "Items": [
            {"planId": "plan-1", "sk": "run-1#cmp-1", "smsCampaignId": "cmp-1", "status": "COMPLETED"},
        ]
    }
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    with (
        patch.object(sms_handler, "boto3") as mock_boto3,
        patch.dict(os.environ, {"SMS_CAMPAIGN_RUNS_TABLE": "VipSmsCampaignRuns"}),
    ):
        mock_boto3.resource.return_value = mock_resource
        resp = sms_handler.get_sms_runs({}, {"id": "plan-1"})

    body = json.loads(resp["body"])
    assert resp["statusCode"] == 200
    assert len(body["runs"]) == 1
    assert body["runs"][0]["smsCampaignId"] == "cmp-1"


def test_get_sms_runs_empty():
    mock_table = MagicMock()
    mock_table.query.return_value = {"Items": []}
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    with (
        patch.object(sms_handler, "boto3") as mock_boto3,
        patch.dict(os.environ, {"SMS_CAMPAIGN_RUNS_TABLE": "VipSmsCampaignRuns"}),
    ):
        mock_boto3.resource.return_value = mock_resource
        resp = sms_handler.get_sms_runs({}, {"id": "plan-unknown"})

    body = json.loads(resp["body"])
    assert resp["statusCode"] == 200
    assert body["runs"] == []


def test_get_sms_runs_queries_by_plan_id():
    mock_table = MagicMock()
    mock_table.query.return_value = {"Items": []}
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    with (
        patch.object(sms_handler, "boto3") as mock_boto3,
        patch.dict(os.environ, {"SMS_CAMPAIGN_RUNS_TABLE": "VipSmsCampaignRuns"}),
    ):
        mock_boto3.resource.return_value = mock_resource
        sms_handler.get_sms_runs({}, {"id": "plan-42"})

    mock_table.query.assert_called_once()
    _, kwargs = mock_table.query.call_args
    expr = kwargs.get("KeyConditionExpression")
    assert expr is not None
