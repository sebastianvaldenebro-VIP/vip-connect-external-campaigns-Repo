"""SMS resource handlers: origination number discovery, SMS run history."""

from __future__ import annotations

import os

import boto3

from vip_shared.application.http import json_response

_sms = boto3.client("pinpoint-sms-voice-v2", region_name="us-east-1")


def list_origination_numbers(event: dict, _path_params: dict) -> dict:
    """
    Return active EUM SMS origination numbers available for sending.
    No PHI — returns only number metadata (ARN, phone number, type).
    """
    paginator = _sms.get_paginator("describe_phone_numbers")
    numbers: list[dict] = []

    for page in paginator.paginate(Filters=[{"Name": "status", "Values": ["ACTIVE"]}]):
        for n in page.get("PhoneNumbers", []):
            numbers.append(
                {
                    "arn": n["PhoneNumberArn"],
                    "phoneNumber": n["PhoneNumber"],
                    "numberType": n.get("NumberType", ""),
                    "countryCode": n.get("CountryCode", "US"),
                    "twoWayEnabled": n.get("TwoWayEnabled", False),
                    "optOutListName": n.get("OptOutListName", ""),
                    "status": n.get("Status", "ACTIVE"),
                }
            )

    return json_response(200, {"originationNumbers": numbers})


def get_sms_runs(event: dict, path_params: dict) -> dict:
    """Return VipSmsCampaignRuns records for a plan. No phone numbers — non-PHI."""
    from boto3.dynamodb.conditions import Key

    plan_id = path_params.get("id", "")
    table_name = os.environ.get("SMS_CAMPAIGN_RUNS_TABLE", "VipSmsCampaignRuns")
    table = boto3.resource("dynamodb").Table(table_name)

    resp = table.query(
        KeyConditionExpression=Key("planId").eq(plan_id),
        ScanIndexForward=False,
    )
    return json_response(200, {"runs": resp.get("Items", [])})
