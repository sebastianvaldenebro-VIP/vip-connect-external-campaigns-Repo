"""Profile read handlers — search, batch get, objects, calculated attributes."""
from __future__ import annotations

import os

from vip_shared.application.http import json_response, parse_body
from vip_shared.infrastructure.persistence.customer_profiles_client import (
    build_from_env as build_cp,
)

DEFAULT_OBJECT_TYPE = os.environ.get("PROFILE_OBJECT_TYPE", "leads-data-mapping")


def search_profiles(event: dict, _path_params: dict) -> dict:
    """GET /profiles/search?key=<KeyName>&value=<Value>&max=<N>

    Keys supported (per domain's Object Type):
      customerid, _phone, _email, _fullName, _profileId
    """
    qs = event.get("queryStringParameters") or {}
    key_name = qs.get("key")
    value = qs.get("value")
    if not key_name or not value:
        raise ValueError("Missing required query params: key, value")

    max_results = int(qs.get("max", 20))

    cp = build_cp()
    response = cp.search_profiles(
        key_name=key_name, values=[value], max_results=max_results
    )

    items = [_serialize_profile(p) for p in response.get("Items", [])]
    return json_response(200, {"profiles": items, "count": len(items)})


def batch_get_profiles(event: dict, _path_params: dict) -> dict:
    """POST /profiles/batch — body: {"profileIds": [...]}."""
    body = parse_body(event)
    profile_ids = body.get("profileIds")
    if not profile_ids or not isinstance(profile_ids, list):
        raise ValueError("Missing required field: profileIds (list)")
    if len(profile_ids) > 100:
        raise ValueError("Max 100 profile IDs per batch")

    cp = build_cp()
    response = cp.batch_get_profile(profile_ids=profile_ids)

    profiles = response.get("Profiles", [])
    errors = response.get("Errors", [])

    return json_response(
        200,
        {
            "profiles": [_serialize_profile(p) for p in profiles],
            "errors": errors,
        },
    )


def get_profile(event: dict, path_params: dict) -> dict:
    """GET /profiles/{profileId} — single profile detail."""
    profile_id = path_params["profileId"]
    cp = build_cp()
    response = cp.batch_get_profile(profile_ids=[profile_id])
    profiles = response.get("Profiles", [])
    if not profiles:
        return json_response(404, {"error": {"code": "NOT_FOUND", "message": "Profile not found"}})
    return json_response(200, {"profile": _serialize_profile(profiles[0])})


def list_objects(event: dict, path_params: dict) -> dict:
    """GET /profiles/{profileId}/objects?objectType=<type>&max=<N>"""
    profile_id = path_params["profileId"]
    qs = event.get("queryStringParameters") or {}
    object_type = qs.get("objectType", DEFAULT_OBJECT_TYPE)
    max_results = int(qs.get("max", 10))

    cp = build_cp()
    response = cp.list_profile_objects(
        profile_id=profile_id,
        object_type_name=object_type,
        max_results=max_results,
    )

    return json_response(
        200,
        {
            "profileId": profile_id,
            "objectType": object_type,
            "objects": response.get("Items", []),
            "nextToken": response.get("NextToken"),
        },
    )


def list_calculated_attrs(event: dict, path_params: dict) -> dict:
    """GET /profiles/{profileId}/calculated-attributes"""
    profile_id = path_params["profileId"]
    cp = build_cp()
    response = cp.list_calculated_attributes_for_profile(profile_id=profile_id)
    return json_response(
        200,
        {
            "profileId": profile_id,
            "calculatedAttributes": response.get("Items", []),
        },
    )


def get_calculated_attr(event: dict, path_params: dict) -> dict:
    """GET /profiles/{profileId}/calculated-attributes/{attrName}"""
    profile_id = path_params["profileId"]
    attr_name = path_params["attrName"]
    cp = build_cp()
    response = cp.get_calculated_attribute_for_profile(
        profile_id=profile_id, calculated_attribute_name=attr_name
    )
    return json_response(
        200,
        {
            "profileId": profile_id,
            "calculatedAttribute": {
                "name": attr_name,
                "value": response.get("Value"),
                "displayName": response.get("DisplayName"),
                "isDataPartial": response.get("IsDataPartial"),
            },
        },
    )


def _serialize_profile(profile: dict) -> dict:
    """Normalize Customer Profiles response for our UI."""
    return {
        "profileId": profile.get("ProfileId"),
        "firstName": profile.get("FirstName"),
        "lastName": profile.get("LastName"),
        "email": profile.get("EmailAddress"),
        "phoneNumber": profile.get("PhoneNumber"),
        "attributes": profile.get("Attributes", {}),
        "createdAt": profile.get("CreatedAt"),
        "lastUpdatedAt": profile.get("LastUpdatedAt"),
    }
