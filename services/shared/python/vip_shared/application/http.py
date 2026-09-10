from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Caller:
    """Who is making the API call — extracted from Cognito JWT claims."""

    sub: str
    email: str
    ip_address: str | None
    user_agent: str | None


def extract_caller(event: dict) -> Caller:
    """Extract Cognito identity + request metadata from an API Gateway v2 event.

    Supports two authorizer shapes because vip-admin-ui-api moved from a
    plain HttpJwtAuthorizer (which populates authorizer.jwt.claims) to a
    custom Lambda authorizer that also enforces per-route Cognito group
    membership (which can only populate authorizer.lambda, API Gateway's
    fixed key for a Lambda-authorizer's returned context — see
    api-authorizer/src/handler.py). Without this fallback every audit record
    written since that switch would silently log actor_sub/actor_email as
    "unknown".
    """
    ctx = event.get("requestContext", {})
    authorizer = ctx.get("authorizer", {}) or {}
    jwt_claims = (authorizer.get("jwt", {}) or {}).get("claims", {}) or {}
    lambda_context = authorizer.get("lambda", {}) or {}

    identity = ctx.get("identity", {}) or {}
    http = ctx.get("http", {}) or {}

    return Caller(
        sub=str(jwt_claims.get("sub") or lambda_context.get("sub") or "unknown"),
        email=str(jwt_claims.get("email") or lambda_context.get("email") or "unknown"),
        ip_address=http.get("sourceIp") or identity.get("sourceIp"),
        user_agent=http.get("userAgent") or event.get("headers", {}).get("user-agent"),
    )


def json_response(status: int, body: Any) -> dict:
    """API Gateway v2 HTTP API response envelope."""
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        },
        "body": json.dumps(body, default=str),
    }


def error_response(
    status: int,
    code: str,
    message: str,
    details: dict | None = None,
    request_id: str | None = None,
) -> dict:
    payload: dict[str, Any] = {
        "error": {
            "code": code,
            "message": message,
        }
    }
    if details:
        payload["error"]["details"] = details
    if request_id:
        payload["error"]["requestId"] = request_id
    return json_response(status, payload)


def parse_body(event: dict) -> dict:
    """Parse JSON body from API Gateway event, tolerant of missing/empty."""
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64

        raw = base64.b64decode(raw).decode("utf-8")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON body: {exc}") from exc
