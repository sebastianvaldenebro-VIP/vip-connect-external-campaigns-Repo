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
# _extract_phones_from_filter
# ---------------------------------------------------------------------------

def _make_phone_segment(*phone_lists):
    """Build SegmentGroups in the phone-filter shape (phones_to_segment_groups output)."""
    dimensions = [
        {
            "ProfileAttributes": {
                "PhoneNumber": {
                    "DimensionType": "INCLUSIVE",
                    "Values": phones,
                }
            }
        }
        for phones in phone_lists
    ]
    return {"Groups": [{"Type": "ANY", "Dimensions": dimensions}]}


def test_extract_phones_from_filter_single_chunk():
    sg = _make_phone_segment(["+15550001111", "+15550002222"])
    result = handler_seeder._extract_phones_from_filter(sg)
    assert result == ["+15550001111", "+15550002222"]


def test_extract_phones_from_filter_multiple_chunks():
    sg = _make_phone_segment(["+15550001111"], ["+15550002222", "+15550003333"])
    result = handler_seeder._extract_phones_from_filter(sg)
    assert result == ["+15550001111", "+15550002222", "+15550003333"]


def test_extract_phones_from_filter_empty_groups():
    assert handler_seeder._extract_phones_from_filter({}) == []


def test_extract_phones_from_filter_skips_exclusive_dimension():
    """Only INCLUSIVE dimensions contribute phones; EXCLUSIVE ones are ignored."""
    sg = {"Groups": [{"Dimensions": [
        {"ProfileAttributes": {"PhoneNumber": {"DimensionType": "EXCLUSIVE", "Values": ["+15550009999"]}}},
        {"ProfileAttributes": {"PhoneNumber": {"DimensionType": "INCLUSIVE", "Values": ["+15550001111"]}}},
    ]}]}
    result = handler_seeder._extract_phones_from_filter(sg)
    assert result == ["+15550001111"]


def test_extract_phones_from_filter_id_segment_returns_empty():
    """ID-based segments (Attributes.ID) must not match this extractor."""
    sg = _make_segment_groups(["id-1", "id-2"])
    assert handler_seeder._extract_phones_from_filter(sg) == []


# ---------------------------------------------------------------------------
# _fetch_phones
# ---------------------------------------------------------------------------

def test_fetch_phones_skips_profiles_without_phone_number():
    customer_ids = ["cid-1", "cid-2", "cid-3"]
    mock_cp = MagicMock()
    # customerid accepts exactly 1 value per call — side_effect per call
    mock_cp.search_profiles.side_effect = [
        {"Items": [{"PhoneNumber": "+15550001111"}]},
        {"Items": [{}]},                                # no PhoneNumber — must be skipped
        {"Items": [{"PhoneNumber": "+15550002222"}]},
    ]
    with patch("handler_seeder._get_cp", return_value=mock_cp):
        phones = handler_seeder._fetch_phones(customer_ids)
    assert phones == ["+15550001111", "+15550002222"]
    assert mock_cp.search_profiles.call_count == 3


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
    mock_cp.search_profiles.side_effect = [
        {"Items": [{"PhoneNumber": "+15550001111"}]},
        {"Items": [{"PhoneNumber": "+15550002222"}]},
    ]
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


# ---------------------------------------------------------------------------
# lambda_handler — phone-filter segment fallback (no BatchGetProfile)
# ---------------------------------------------------------------------------

def test_lambda_handler_phone_filter_segment_seeds_without_batch_get_profile():
    """Segments built by executor._create_segment use PhoneNumber.INCLUSIVE.
    The seeder must extract phones directly without calling BatchGetProfile.
    """
    mock_cp = MagicMock()
    mock_cp.get_segment_definition.return_value = {
        "SegmentGroups": _make_phone_segment(["+15550001111", "+15550002222"])
    }
    mock_table = MagicMock()
    mock_batch_writer = MagicMock()
    mock_table.batch_writer.return_value.__enter__ = MagicMock(return_value=mock_batch_writer)
    mock_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)

    with patch("handler_seeder._get_cp", return_value=mock_cp), \
         patch("handler_seeder._get_table", return_value=mock_table):
        resp = handler_seeder.lambda_handler(
            _api_event("camp-branded", {"segmentName": "phone-seg"}), None
        )

    # Must NOT call BatchGetProfile — phones come directly from the segment definition
    mock_cp.batch_get_profile.assert_not_called()
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["seeded"] == 2
    assert mock_batch_writer.put_item.call_count == 2


def test_lambda_handler_phone_filter_empty_returns_zero_seeded_on_direct_invoke():
    """Phone-filter segment with no phones → seeded:0 on direct invoke (not HTTP error)."""
    mock_cp = MagicMock()
    mock_cp.get_segment_definition.return_value = {
        "SegmentGroups": _make_phone_segment([])  # segment exists but has no phones
    }

    direct_invoke_event = {
        "campaignId": "camp-branded",
        "segmentName": "empty-phone-seg",
        "contactFlowId": "flow-abc",
        "sourcePhone": "+15550001234",
    }

    with patch("handler_seeder._get_cp", return_value=mock_cp):
        result = handler_seeder.lambda_handler(direct_invoke_event, None)

    assert result == {"seeded": 0}
    mock_cp.batch_get_profile.assert_not_called()


# ---------------------------------------------------------------------------
# H-6: direct-invoke mode raises RuntimeError on segment ClientError
# ---------------------------------------------------------------------------

def test_direct_invoke_raises_on_segment_not_found():
    """When get_segment_definition raises ResourceNotFoundException in direct-invoke mode
    (no pathParameters in event), the handler must raise RuntimeError so the executor
    treats it as a real error instead of silently producing 0 seeded contacts.
    """
    from botocore.exceptions import ClientError

    mock_cp = MagicMock()
    mock_cp.get_segment_definition.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "not found"}},
        "GetSegmentDefinition",
    )

    # Direct-invoke event: flat dict (no pathParameters, campaignId at top level)
    direct_invoke_event = {
        "campaignId": "bc-h6",
        "segmentName": "missing-seg",
        "contactFlowId": "flow-abc",
        "sourcePhone": "+15550001234",
    }

    with patch("handler_seeder._get_cp", return_value=mock_cp):
        with pytest.raises(RuntimeError, match="segment lookup failed"):
            handler_seeder.lambda_handler(direct_invoke_event, None)


def test_direct_invoke_raises_on_segment_access_denied():
    """When get_segment_definition raises AccessDeniedException in direct-invoke mode,
    the handler must raise RuntimeError (not return an HTTP 403 dict).
    """
    from botocore.exceptions import ClientError

    mock_cp = MagicMock()
    mock_cp.get_segment_definition.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "not authorized"}},
        "GetSegmentDefinition",
    )

    # Direct-invoke event: flat dict (no pathParameters, campaignId at top level)
    direct_invoke_event = {
        "campaignId": "bc-h6b",
        "segmentName": "restricted-seg",
        "contactFlowId": "flow-abc",
        "sourcePhone": "+15550001234",
    }

    with patch("handler_seeder._get_cp", return_value=mock_cp):
        with pytest.raises(RuntimeError, match="segment lookup failed"):
            handler_seeder.lambda_handler(direct_invoke_event, None)


# ---------------------------------------------------------------------------
# lambda_handler — direct Lambda invocation (not via API Gateway)
# ---------------------------------------------------------------------------

class TestDirectInvocation:
    """Seeder invoked directly from executor Lambda (not via API Gateway)."""

    def test_direct_payload_extracts_campaign_id(self, mocker):
        mock_cp = MagicMock()
        mock_cp.get_segment_definition.return_value = {
            "SegmentGroups": {
                "Groups": [{
                    "Dimensions": [{
                        "ProfileAttributes": {
                            "Attributes": {
                                "ID": {"DimensionType": "INCLUSIVE", "Values": ["p1"]}
                            }
                        }
                    }]
                }]
            }
        }
        mock_cp.search_profiles.return_value = {"Items": [{"PhoneNumber": "+15551234567"}]}
        mocker.patch("handler_seeder._get_cp", return_value=mock_cp)
        mock_table = MagicMock()
        mock_batch_writer = MagicMock()
        mock_table.batch_writer.return_value.__enter__ = MagicMock(return_value=mock_batch_writer)
        mock_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)
        mocker.patch("handler_seeder._get_table", return_value=mock_table)

        event = {
            "campaignId": "camp-direct-001",
            "segmentName": "test-segment",
            "contactFlowId": "flow-abc",
            "sourcePhone": "+12125550199",
        }
        result = handler_seeder.lambda_handler(event, None)
        assert result == {"seeded": 1}

    def test_direct_payload_missing_campaign_id_raises_value_error(self):
        """Direct-invoke with no campaignId must raise ValueError (not return a 400 dict)."""
        event = {"segmentName": "test-segment"}
        with pytest.raises(ValueError, match="missing campaignId"):
            handler_seeder.lambda_handler(event, None)
