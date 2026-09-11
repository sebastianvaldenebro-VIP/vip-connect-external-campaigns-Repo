"""HTTP API Lambda authorizer — Cognito ID token verification + per-route
group authorization.

Replaces the plain HttpJwtAuthorizer for every route on vip-admin-ui-api.
HttpJwtAuthorizer only proves "this is a valid token for this user pool" —
it cannot express "but only let the Agent group reach /deny-list". Cognito
User Pool Groups + this authorizer add that second dimension.

Rule (checked in this order):
  1. `Admin` group        -> allowed on every route.
  2. `Agent` group        -> allowed only on AGENT_ALLOWED_PREFIXES.
  3. no matching group    -> denied.

There is deliberately no grandfather/default-allow branch for users with no
group claim — every existing admin-ui user must be added to the `Admin`
Cognito group as part of shipping this (see auth-stack.ts), or they lose
access the moment this authorizer goes live.

Group membership is baked into the ID token at sign-in/refresh time: adding
someone to a group doesn't take effect until their token refreshes.

Logging deliberately uses stdlib `logging`, not vip_shared's
StructuredLogger — this Lambda is intentionally NOT on the shared layer (it
must keep working even if a bug ships in shared code that breaks every
other Lambda's import), and that isolation shouldn't cost it all
diagnostic signal. Never logs the raw token or full phone/PHI — only sub,
email, path, and failure reasons.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from urllib.error import URLError

from jose import jwk, jwt
from jose.exceptions import JOSEError
from jose.utils import base64url_decode

USER_POOL_ID = os.environ["USER_POOL_ID"]
CLIENT_ID = os.environ["USER_POOL_CLIENT_ID"]
REGION = os.environ.get("AWS_REGION", "us-east-1")

ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{USER_POOL_ID}"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"
JWKS_TTL_SECONDS = 3600

ADMIN_GROUP = "Admin"
AGENT_GROUP = "Agent"
AGENT_ALLOWED_PREFIXES = ("/deny-list",)

_jwks_cache: dict = {"keys": None, "fetched_at": 0.0}

logger = logging.getLogger("api-authorizer")
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


class TokenError(Exception):
    pass


def _fetch_jwks() -> list[dict]:
    with urllib.request.urlopen(JWKS_URL, timeout=5) as resp:  # fixed AWS-owned HTTPS URL, not user input  # noqa: S310  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        return json.loads(resp.read())["keys"]


def _get_signing_key(kid: str) -> dict:
    now = time.time()
    if (
        _jwks_cache["keys"] is None
        or now - _jwks_cache["fetched_at"] > JWKS_TTL_SECONDS
    ):
        _jwks_cache["keys"] = _fetch_jwks()
        _jwks_cache["fetched_at"] = now

    key = next((k for k in _jwks_cache["keys"] if k["kid"] == kid), None)
    if key is not None:
        return key

    # Key not found — could be a genuine rotation. Refresh once and retry
    # before giving up, so a routine Cognito key rotation doesn't cause a
    # window of false rejections.
    _jwks_cache["keys"] = _fetch_jwks()
    _jwks_cache["fetched_at"] = now
    key = next((k for k in _jwks_cache["keys"] if k["kid"] == kid), None)
    if key is None:
        raise TokenError("Signing key not found")
    return key


def verify_token(token: str) -> dict:
    """Verify signature, issuer, audience, token_use, and expiry. Returns claims."""
    try:
        header = jwt.get_unverified_header(token)
        claims = jwt.get_unverified_claims(token)
    except Exception as exc:
        raise TokenError(f"Malformed token: {exc}") from exc

    kid = header.get("kid")
    if not kid:
        raise TokenError("Missing kid header")

    # A JWKS-endpoint blip (network error, timeout, malformed JSON) must
    # fail closed as a clean denial, not propagate as an unhandled
    # exception — this Lambda now gates every route on the admin API, so an
    # unhandled error here would 500 the whole app instead of denying one
    # request.
    try:
        signing_key = jwk.construct(_get_signing_key(kid))
    except TokenError:
        raise
    except (
        URLError,
        TimeoutError,
        OSError,
        json.JSONDecodeError,
        KeyError,
        JOSEError,
    ) as exc:
        logger.error("jwks_fetch_failed", extra={"error": str(exc)})
        raise TokenError(f"JWKS fetch failed: {exc}") from exc

    message, encoded_sig = token.rsplit(".", 1)
    signature = base64url_decode(encoded_sig.encode("utf-8"))
    if not signing_key.verify(message.encode("utf-8"), signature):
        raise TokenError("Invalid signature")

    if claims.get("iss") != ISSUER:
        raise TokenError("Invalid issuer")
    if claims.get("token_use") != "id":
        raise TokenError("Not an ID token")
    if claims.get("aud") != CLIENT_ID:
        raise TokenError("Invalid audience")
    if claims.get("exp", 0) < time.time():
        raise TokenError("Token expired")

    return claims


def is_route_allowed(path: str, groups: list[str]) -> bool:
    if ADMIN_GROUP in groups:
        return True
    if AGENT_GROUP in groups:
        return any(
            path == prefix or path.startswith(prefix + "/")
            for prefix in AGENT_ALLOWED_PREFIXES
        )
    return False


def lambda_handler(event: dict, context) -> dict:
    headers = event.get("headers") or {}
    auth_header = headers.get("authorization") or headers.get("Authorization") or ""
    path = event.get("rawPath", "")

    if not auth_header.startswith("Bearer "):
        logger.info("denied_no_bearer_token", extra={"path": path})
        return {"isAuthorized": False}

    token = auth_header[len("Bearer ") :]
    try:
        claims = verify_token(token)
    except TokenError as exc:
        logger.info("denied_invalid_token", extra={"path": path, "reason": str(exc)})
        return {"isAuthorized": False}

    groups = claims.get("cognito:groups") or []
    sub = claims.get("sub", "")

    if not is_route_allowed(path, groups):
        logger.info(
            "denied_insufficient_group",
            extra={"path": path, "sub": sub, "groups": groups},
        )
        return {"isAuthorized": False}

    return {
        "isAuthorized": True,
        "context": {
            "sub": sub,
            "email": claims.get("email", ""),
        },
    }
