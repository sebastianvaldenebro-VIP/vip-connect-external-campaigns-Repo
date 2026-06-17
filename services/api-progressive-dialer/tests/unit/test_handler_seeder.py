# services/api-progressive-dialer/tests/unit/test_handler_seeder.py
import json
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Minimal env vars so the module-level os.environ reads don't KeyError on import
# ---------------------------------------------------------------------------
import os
os.environ.setdefault("PROFILES_DOMAIN_NAME", "test-domain")
os.environ.setdefault("CAMPAIGN_QUEUE_TABLE", "test-table")

import handler_seeder  # noqa: E402  — imported after env setup


# ---------------------------------------------------------------------------
# _extract_profile_ids
# ---------------------------------------------------------------------------

def _make_segment_groups(*id_lists):
    """Build SegmentGroups in the shape produced by SegmentGroupsTranslator."""
    dimensions = []
    for values in id_lists:
        dimensions.append({
            "ProfileAttributes": {
                "Attributes": {
                    "ID": {
                        "DimensionType": "INCLUSIVE",
                        "Values": values,
                    }
                }
            }
        })
    return {"Groups": [{"Dimensions": dimensions}]}


def test_extract_profile_ids_correct_nesting():
    """Verifies the Attributes key is traversed (the B2 bug was missing this level)."""
    sg = _make_segment_groups(["id-1", "id-2"], ["id-3"])
    result = handler_seeder._extract_profile_ids(sg)
    assert result == ["id-1", "id-2", "id-3"]


def test_extract_profile_ids_empty_groups():
    assert handler_seeder._extract_profile_ids({}) == []


def test_extract_profile_ids_missing_attributes_key():
    """Old (wrong) structure — ProfileAttributes.ID — must return empty, not crash."""
    sg = {"Groups": [{"Dimensions": [{"ProfileAttributes": {"ID": {"Values": ["x"]}}}]}]}
    result = handler_seeder._extract_profile_ids(sg)
    assert result == []


# ---------------------------------------------------------------------------
# _fetch_phones
# ---------------------------------------------------------------------------

def test_fetch_phones_skips_profiles_without_phone_number():
    profile_ids = ["p1", "p2", "p3"]
    mock_cp = MagicMock()
    mock_cp.batch_get_profile.return_value = {
        "Profiles": [
            {"ProfileId": "p1", "PhoneNumber": "+15550001111"},
            {"ProfileId": "p2"},                        # no PhoneNumber — must be skipped
            {"ProfileId": "p3", "PhoneNumber": "+15550002222"},
        ],
        "Errors": [],
    }
    with patch("handler_seeder._get_cp", return_value=mock_cp):
        phones = handler_seeder._fetch_phones(profile_ids)
    assert phones == ["+15550001111", "+15550002222"]


def test_fetch_phones_empty_list():
    # _fetch_phones has an early-return guard — _get_cp() must never be called with empty input
    with patch("handler_seeder._get_cp") as mock_get_cp:
        phones = handler_seeder._fetch_phones([])
    assert phones == []
    mock_get_cp.assert_not_called()


# ---------------------------------------------------------------------------
# lambda_handler — validation
# ---------------------------------------------------------------------------

def _api_event(campaign_id=None, body=None):
    event = {"pathParameters": {}, "body": None}
    if campaign_id:
        event["pathParameters"]["id"] = campaign_id
    if body is not None:
        event["body"] = json.dumps(body)
    return event


def test_lambda_handler_missing_campaign_id():
    resp = handler_seeder.lambda_handler(_api_event(), None)
    assert resp["statusCode"] == 400
    assert "missing campaign id" in json.loads(resp["body"])["error"]


def test_lambda_handler_missing_segment_name():
    resp = handler_seeder.lambda_handler(_api_event("camp-1", {}), None)
    assert resp["statusCode"] == 400
    assert "missing segmentName" in json.loads(resp["body"])["error"]


# ---------------------------------------------------------------------------
# lambda_handler — segment not found (W4)
# ---------------------------------------------------------------------------

def test_lambda_handler_segment_not_found_returns_404():
    from botocore.exceptions import ClientError
    mock_cp = MagicMock()
    mock_cp.get_segment_definition.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "not found"}},
        "GetSegmentDefinition",
    )
    with patch("handler_seeder._get_cp", return_value=mock_cp):
        resp = handler_seeder.lambda_handler(
            _api_event("camp-1", {"segmentName": "missing-seg"}), None
        )
    assert resp["statusCode"] == 404
    assert "segment not found" in json.loads(resp["body"])["error"]


def test_lambda_handler_access_denied_returns_403():
    from botocore.exceptions import ClientError
    mock_cp = MagicMock()
    mock_cp.get_segment_definition.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "not authorized"}},
        "GetSegmentDefinition",
    )
    with patch("handler_seeder._get_cp", return_value=mock_cp):
        resp = handler_seeder.lambda_handler(
            _api_event("camp-1", {"segmentName": "restricted-seg"}), None
        )
    assert resp["statusCode"] == 403
    assert "access denied" in json.loads(resp["body"])["error"]


# ---------------------------------------------------------------------------
# lambda_handler — success path
# ---------------------------------------------------------------------------

def test_lambda_handler_success_seeds_contacts():
    from unittest.mock import MagicMock, patch, call
    mock_cp = MagicMock()
    mock_cp.get_segment_definition.return_value = {
        "SegmentGroups": {
            "Groups": [{
                "Dimensions": [{
                    "ProfileAttributes": {
                        "Attributes": {
                            "ID": {"DimensionType": "INCLUSIVE", "Values": ["p1", "p2"]}
                        }
                    }
                }]
            }]
        }
    }
    mock_cp.batch_get_profile.return_value = {
        "Profiles": [
            {"ProfileId": "p1", "PhoneNumber": "+15550001111"},
            {"ProfileId": "p2", "PhoneNumber": "+15550002222"},
        ],
        "Errors": [],
    }
    mock_table = MagicMock()
    mock_batch_writer = MagicMock()
    mock_table.batch_writer.return_value.__enter__ = MagicMock(return_value=mock_batch_writer)
    mock_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)

    with patch("handler_seeder._get_cp", return_value=mock_cp), \
         patch("handler_seeder._get_table", return_value=mock_table):
        resp = handler_seeder.lambda_handler(
            _api_event("camp-1", {"segmentName": "my-seg"}), None
        )

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["seeded"] == 2
    assert body["profilesFound"] == 2
    assert body["contactsWithPhone"] == 2
    assert mock_batch_writer.put_item.call_count == 2
