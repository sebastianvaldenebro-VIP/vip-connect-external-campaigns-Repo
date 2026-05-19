"""Tests for profiles handlers."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("PROFILES_DOMAIN_NAME", "amazon-connect-vipmedicalgroup")
    monkeypatch.setenv("PROFILE_OBJECT_TYPE", "leads-data-mapping")


def _event(body=None, qs=None):
    e = {"requestContext": {"authorizer": {"jwt": {"claims": {"sub": "u", "email": "u@x"}}}}}
    if body is not None:
        e["body"] = json.dumps(body)
    if qs is not None:
        e["queryStringParameters"] = qs
    return e


def test_search_profiles_returns_normalized_items():
    from handlers import profiles

    mock_cp = MagicMock()
    mock_cp.search_profiles.return_value = {
        "Items": [
            {
                "ProfileId": "p-1",
                "FirstName": "Patrina",
                "LastName": "Crawford",
                "PhoneNumber": "+12017801027",
                "Attributes": {"available": "False", "groups": "New Lead / 2nd Attempt"},
            }
        ]
    }

    event = _event(qs={"key": "_phone", "value": "+12017801027", "max": "5"})

    with patch("handlers.profiles.build_cp", return_value=mock_cp):
        response = profiles.search_profiles(event, {})

    body = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert body["count"] == 1
    assert body["profiles"][0]["firstName"] == "Patrina"
    assert body["profiles"][0]["phoneNumber"] == "+12017801027"
    mock_cp.search_profiles.assert_called_once_with(
        key_name="_phone", values=["+12017801027"], max_results=5
    )


def test_search_profiles_requires_key_and_value():
    from handlers import profiles

    with pytest.raises(ValueError, match="key, value"):
        profiles.search_profiles(_event(qs={"key": "phone"}), {})


def test_batch_get_rejects_empty_list():
    from handlers import profiles

    with pytest.raises(ValueError, match="profileIds"):
        profiles.batch_get_profiles(_event(body={}), {})


def test_batch_get_rejects_over_100():
    from handlers import profiles

    with pytest.raises(ValueError, match="Max 100"):
        profiles.batch_get_profiles(
            _event(body={"profileIds": ["id"] * 101}), {}
        )


def test_batch_get_returns_serialized_profiles():
    from handlers import profiles

    mock_cp = MagicMock()
    mock_cp.batch_get_profile.return_value = {
        "Profiles": [
            {
                "ProfileId": "p-1",
                "FirstName": "A",
                "LastName": "B",
                "Attributes": {},
            }
        ],
        "Errors": [],
    }

    with patch("handlers.profiles.build_cp", return_value=mock_cp):
        response = profiles.batch_get_profiles(
            _event(body={"profileIds": ["p-1"]}), {}
        )

    body = json.loads(response["body"])
    assert len(body["profiles"]) == 1
    assert body["profiles"][0]["profileId"] == "p-1"
    assert body["errors"] == []


def test_get_profile_returns_404_when_not_found():
    from handlers import profiles

    mock_cp = MagicMock()
    mock_cp.batch_get_profile.return_value = {"Profiles": [], "Errors": []}

    with patch("handlers.profiles.build_cp", return_value=mock_cp):
        response = profiles.get_profile(_event(), {"profileId": "p-missing"})

    assert response["statusCode"] == 404


def test_list_objects_uses_default_object_type():
    from handlers import profiles

    mock_cp = MagicMock()
    mock_cp.list_profile_objects.return_value = {"Items": [{"Object": "..."}]}

    with patch("handlers.profiles.build_cp", return_value=mock_cp):
        response = profiles.list_objects(_event(qs={}), {"profileId": "p-1"})

    body = json.loads(response["body"])
    assert body["objectType"] == "leads-data-mapping"
    mock_cp.list_profile_objects.assert_called_once()


def test_list_objects_respects_custom_object_type():
    from handlers import profiles

    mock_cp = MagicMock()
    mock_cp.list_profile_objects.return_value = {"Items": []}

    with patch("handlers.profiles.build_cp", return_value=mock_cp):
        profiles.list_objects(
            _event(qs={"objectType": "custom-ot", "max": "5"}),
            {"profileId": "p-1"},
        )

    call_kwargs = mock_cp.list_profile_objects.call_args.kwargs
    assert call_kwargs["object_type_name"] == "custom-ot"
    assert call_kwargs["max_results"] == 5


def test_get_calculated_attr():
    from handlers import profiles

    mock_cp = MagicMock()
    mock_cp.get_calculated_attribute_for_profile.return_value = {
        "Value": "42",
        "DisplayName": "Total contacts",
        "IsDataPartial": "false",
    }

    with patch("handlers.profiles.build_cp", return_value=mock_cp):
        response = profiles.get_calculated_attr(
            _event(), {"profileId": "p-1", "attrName": "total_contacts"}
        )

    body = json.loads(response["body"])
    assert body["calculatedAttribute"]["name"] == "total_contacts"
    assert body["calculatedAttribute"]["value"] == "42"
