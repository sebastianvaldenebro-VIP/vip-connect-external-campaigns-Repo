"""Phone lookup — cross-checks Redis wait-list leads against Customer Profiles.

Invokes the sibling repo's already-deployed `connectcampaignRedisAuxiliar`
Lambda (own naming prefix, not part of this app's stacks) via
lambda:InvokeFunction, scoped to the exact ARN granted in
infra/lib/stacks/api-profiles-stack.ts (REDIS_AUXILIAR_LOOKUP_ARN env var).

Fail-closed: any invoke/parse error returns 503 rather than propagating an
unhandled exception, since this endpoint depends entirely on a Lambda this
service does not own.
"""

from __future__ import annotations

import json
import os

import boto3
from botocore.exceptions import ClientError

from vip_shared.application.http import error_response, json_response

_lambda_client = None


def _get_lambda_client():
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda")
    return _lambda_client


def phone_lookup(event: dict, _path_params: dict) -> dict:
    """GET /phone-lookup?phone=<digits or E.164>

    Proxies connectcampaignRedisAuxiliar and returns its response as-is —
    that Lambda already shapes {phone_searched, redis, profiles, cross_check}.
    """
    qs = event.get("queryStringParameters") or {}
    phone = qs.get("phone")
    if not phone:
        raise ValueError("Missing required query param: phone")

    function_arn = os.environ.get("REDIS_AUXILIAR_LOOKUP_ARN")
    if not function_arn:
        return error_response(
            503,
            "PHONE_LOOKUP_UNAVAILABLE",
            "Phone lookup is not configured in this environment.",
        )

    try:
        response = _get_lambda_client().invoke(
            FunctionName=function_arn,
            InvocationType="RequestResponse",
            Payload=json.dumps({"phone": phone}).encode(),
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "ClientError")
        return error_response(
            503,
            "PHONE_LOOKUP_INVOKE_FAILED",
            f"Could not reach the phone lookup service ({code}).",
        )

    if response.get("FunctionError"):
        # Do NOT log the payload — the target Lambda's stack frames may
        # contain PHI (lead phone numbers, names).
        return error_response(
            503,
            "PHONE_LOOKUP_INVOKE_FAILED",
            "Phone lookup service returned an error.",
        )

    try:
        result = json.loads(response["Payload"].read())
    except (KeyError, ValueError, TypeError):
        return error_response(
            503,
            "PHONE_LOOKUP_INVALID_RESPONSE",
            "Phone lookup service returned an unreadable response.",
        )

    if not isinstance(result, dict):
        return error_response(
            503,
            "PHONE_LOOKUP_INVALID_RESPONSE",
            "Phone lookup service returned an unexpected response shape.",
        )

    return json_response(200, result)
