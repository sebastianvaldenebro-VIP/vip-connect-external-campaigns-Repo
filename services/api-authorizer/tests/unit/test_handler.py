"""Tests for the Cognito-JWT + group-authorization Lambda authorizer.

Uses a locally generated RSA keypair to sign real JWTs end-to-end (not just
the pure routing logic) — this authorizer gates every route on the admin
API, so the crypto path itself must be covered, not just is_route_allowed.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwk, jwt

USER_POOL_ID = "us-east-1_TESTPOOL"
CLIENT_ID = "test-client-id"
KID = "test-key-1"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("USER_POOL_ID", USER_POOL_ID)
    monkeypatch.setenv("USER_POOL_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("AWS_REGION", "us-east-1")


@pytest.fixture()
def keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_private = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_key = private_key.public_key()
    pem_public = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    jwk_dict = jwk.construct(pem_public.decode(), algorithm="RS256").to_dict()
    jwk_dict["kid"] = KID
    jwk_dict["alg"] = "RS256"
    jwk_dict["use"] = "sig"
    return {"private_pem": pem_private.decode(), "jwks": [jwk_dict]}


def _issuer():
    return f"https://cognito-idp.us-east-1.amazonaws.com/{USER_POOL_ID}"


def _make_token(
    private_pem, *, groups=None, token_use="id", aud=CLIENT_ID, iss=None, exp_delta=3600
):
    claims = {
        "sub": "user-123",
        "email": "user@vip.com",
        "iss": iss or _issuer(),
        "aud": aud,
        "token_use": token_use,
        "exp": int(time.time()) + exp_delta,
        "iat": int(time.time()),
    }
    if groups is not None:
        claims["cognito:groups"] = groups
    return jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": KID})


def _event(token: str | None, path: str = "/deny-list"):
    headers = {"authorization": f"Bearer {token}"} if token else {}
    return {"headers": headers, "rawPath": path}


# ── is_route_allowed — pure authorization matrix ──────────────────────────


def test_admin_group_allowed_on_deny_list():
    from handler import is_route_allowed

    assert is_route_allowed("/deny-list", ["Admin"]) is True


def test_admin_group_allowed_on_any_other_route():
    from handler import is_route_allowed

    assert is_route_allowed("/campaigns", ["Admin"]) is True
    assert is_route_allowed("/plans/123/runs", ["Admin"]) is True


def test_agent_group_allowed_on_deny_list_only():
    from handler import is_route_allowed

    assert is_route_allowed("/deny-list", ["Agent"]) is True


def test_agent_group_denied_on_other_routes():
    from handler import is_route_allowed

    assert is_route_allowed("/campaigns", ["Agent"]) is False
    assert is_route_allowed("/plans", ["Agent"]) is False


def test_no_group_denied_everywhere():
    from handler import is_route_allowed

    assert is_route_allowed("/deny-list", []) is False
    assert is_route_allowed("/campaigns", []) is False


def test_agent_and_admin_both_present_allowed_everywhere():
    from handler import is_route_allowed

    assert is_route_allowed("/campaigns", ["Agent", "Admin"]) is True


def test_agent_group_allowed_on_deny_list_subpath():
    from handler import is_route_allowed

    assert is_route_allowed("/deny-list/anything", ["Agent"]) is True


def test_agent_group_denied_on_route_with_matching_prefix_but_no_boundary():
    """A route literally starting with the same characters as an allowed
    prefix (but not the same path segment) must NOT be granted — unanchored
    str.startswith would silently expose any future '/deny-list*' route to
    the Agent group with zero code change."""
    from handler import is_route_allowed

    assert is_route_allowed("/deny-listing-report", ["Agent"]) is False
    assert is_route_allowed("/deny-list-export", ["Agent"]) is False


# ── lambda_handler — end-to-end with real signed JWTs ─────────────────────


def test_admin_token_authorized_for_protected_route(keypair):
    import handler

    token = _make_token(keypair["private_pem"], groups=["Admin"])
    with patch.object(handler, "_fetch_jwks", return_value=keypair["jwks"]):
        handler._jwks_cache["keys"] = None
        result = handler.lambda_handler(_event(token, "/campaigns"), None)

    assert result["isAuthorized"] is True
    assert result["context"]["sub"] == "user-123"
    assert result["context"]["email"] == "user@vip.com"


def test_agent_token_authorized_for_deny_list(keypair):
    import handler

    token = _make_token(keypair["private_pem"], groups=["Agent"])
    with patch.object(handler, "_fetch_jwks", return_value=keypair["jwks"]):
        handler._jwks_cache["keys"] = None
        result = handler.lambda_handler(_event(token, "/deny-list"), None)

    assert result["isAuthorized"] is True


def test_agent_token_denied_for_other_route(keypair):
    import handler

    token = _make_token(keypair["private_pem"], groups=["Agent"])
    with patch.object(handler, "_fetch_jwks", return_value=keypair["jwks"]):
        handler._jwks_cache["keys"] = None
        result = handler.lambda_handler(_event(token, "/campaigns"), None)

    assert result == {"isAuthorized": False}


def test_token_with_no_groups_denied(keypair):
    import handler

    token = _make_token(keypair["private_pem"], groups=None)
    with patch.object(handler, "_fetch_jwks", return_value=keypair["jwks"]):
        handler._jwks_cache["keys"] = None
        result = handler.lambda_handler(_event(token, "/deny-list"), None)

    assert result == {"isAuthorized": False}


def test_expired_token_denied(keypair):
    import handler

    token = _make_token(keypair["private_pem"], groups=["Admin"], exp_delta=-10)
    with patch.object(handler, "_fetch_jwks", return_value=keypair["jwks"]):
        handler._jwks_cache["keys"] = None
        result = handler.lambda_handler(_event(token, "/campaigns"), None)

    assert result == {"isAuthorized": False}


def test_wrong_audience_denied(keypair):
    import handler

    token = _make_token(
        keypair["private_pem"], groups=["Admin"], aud="some-other-client"
    )
    with patch.object(handler, "_fetch_jwks", return_value=keypair["jwks"]):
        handler._jwks_cache["keys"] = None
        result = handler.lambda_handler(_event(token, "/campaigns"), None)

    assert result == {"isAuthorized": False}


def test_wrong_issuer_denied(keypair):
    import handler

    token = _make_token(
        keypair["private_pem"], groups=["Admin"], iss="https://evil.example.com"
    )
    with patch.object(handler, "_fetch_jwks", return_value=keypair["jwks"]):
        handler._jwks_cache["keys"] = None
        result = handler.lambda_handler(_event(token, "/campaigns"), None)

    assert result == {"isAuthorized": False}


def test_access_token_use_rejected(keypair):
    """Only ID tokens carry cognito:groups reliably and match this app's aud
    usage — the frontend sends the ID token, so anything else must be
    rejected even if otherwise validly signed."""
    import handler

    token = _make_token(keypair["private_pem"], groups=["Admin"], token_use="access")
    with patch.object(handler, "_fetch_jwks", return_value=keypair["jwks"]):
        handler._jwks_cache["keys"] = None
        result = handler.lambda_handler(_event(token, "/campaigns"), None)

    assert result == {"isAuthorized": False}


def test_tampered_signature_denied(keypair):
    import handler

    token = _make_token(keypair["private_pem"], groups=["Admin"])
    tampered = token[:-4] + ("A" * 4)
    with patch.object(handler, "_fetch_jwks", return_value=keypair["jwks"]):
        handler._jwks_cache["keys"] = None
        result = handler.lambda_handler(_event(tampered, "/campaigns"), None)

    assert result == {"isAuthorized": False}


def test_missing_authorization_header_denied():
    import handler

    result = handler.lambda_handler(_event(None), None)
    assert result == {"isAuthorized": False}


def test_non_bearer_scheme_denied():
    import handler

    result = handler.lambda_handler(
        {"headers": {"authorization": "Basic abc"}, "rawPath": "/campaigns"}, None
    )
    assert result == {"isAuthorized": False}


def test_jwks_network_failure_fails_closed_not_500(keypair):
    """A transient JWKS-endpoint failure (network error, timeout, malformed
    JSON) must deny cleanly rather than propagate as an unhandled exception
    — this Lambda gates every route on the admin API, so an unguarded
    exception here would 500 the whole app on any JWKS blip."""
    import handler

    token = _make_token(keypair["private_pem"], groups=["Admin"])

    from urllib.error import URLError

    with patch.object(handler, "_fetch_jwks", side_effect=URLError("connection reset")):
        handler._jwks_cache["keys"] = None
        result = handler.lambda_handler(_event(token, "/campaigns"), None)

    assert result == {"isAuthorized": False}


def test_jwks_key_rotation_triggers_one_refetch(keypair):
    """If the token's kid isn't in the cached JWKS, refetch once before
    denying — a routine Cognito key rotation shouldn't cause a rejection
    window."""
    import handler

    token = _make_token(keypair["private_pem"], groups=["Admin"])
    stale_jwks = [{**keypair["jwks"][0], "kid": "old-kid"}]

    fetch_calls = {"count": 0}

    def fake_fetch():
        fetch_calls["count"] += 1
        return keypair["jwks"] if fetch_calls["count"] > 1 else stale_jwks

    with patch.object(handler, "_fetch_jwks", side_effect=fake_fetch):
        handler._jwks_cache["keys"] = None
        result = handler.lambda_handler(_event(token, "/campaigns"), None)

    assert result["isAuthorized"] is True
    assert fetch_calls["count"] == 2
