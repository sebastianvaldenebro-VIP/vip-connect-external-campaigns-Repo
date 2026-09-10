"""Tests for extract_caller's dual authorizer-shape support.

vip-admin-ui-api moved from HttpJwtAuthorizer (authorizer.jwt.claims) to a
custom Lambda authorizer (authorizer.lambda) for per-route Cognito group
enforcement — every existing Lambda's audit trail depends on this still
resolving actor_sub/actor_email correctly under the new shape.
"""

from __future__ import annotations

from vip_shared.application.http import extract_caller


def test_extract_caller_reads_jwt_authorizer_shape():
    event = {
        "requestContext": {
            "authorizer": {"jwt": {"claims": {"sub": "u-1", "email": "a@x.com"}}},
            "http": {"sourceIp": "1.2.3.4", "userAgent": "ua-jwt"},
        }
    }
    caller = extract_caller(event)
    assert caller.sub == "u-1"
    assert caller.email == "a@x.com"
    assert caller.ip_address == "1.2.3.4"
    assert caller.user_agent == "ua-jwt"


def test_extract_caller_reads_lambda_authorizer_shape():
    event = {
        "requestContext": {
            "authorizer": {"lambda": {"sub": "u-2", "email": "b@x.com"}},
            "http": {"sourceIp": "5.6.7.8", "userAgent": "ua-lambda"},
        }
    }
    caller = extract_caller(event)
    assert caller.sub == "u-2"
    assert caller.email == "b@x.com"


def test_extract_caller_prefers_jwt_shape_when_both_present():
    event = {
        "requestContext": {
            "authorizer": {
                "jwt": {"claims": {"sub": "jwt-sub", "email": "jwt@x.com"}},
                "lambda": {"sub": "lambda-sub", "email": "lambda@x.com"},
            },
        }
    }
    caller = extract_caller(event)
    assert caller.sub == "jwt-sub"


def test_extract_caller_falls_back_to_unknown_when_no_authorizer():
    caller = extract_caller({"requestContext": {}})
    assert caller.sub == "unknown"
    assert caller.email == "unknown"
