"""Quadrivia after-hours callback webhook.

Quadrivia hosts an AI voice agent on another cloud that answers VIP's
after-hours calls. When the caller asks to be rung back during business
hours, Quadrivia POSTs here and this Lambda creates a *scheduled* Amazon
Connect task so a human agent picks it up at the requested time.

Trust boundary: this is machine-to-machine traffic from a third party, NOT a
human admin-ui user, so it deliberately does not share vip-admin-ui-api's
HttpApi or its Cognito Lambda authorizer. Three independent layers guard it:

  1. mTLS on the API Gateway custom domain (transport; see
     infra/lib/stacks/quadrivia-webhook-stack.ts).
  2. HMAC-SHA256 over the raw body + a timestamp window, verified inline
     *here* rather than in a separate Lambda authorizer — a second Lambda in
     the synchronous path of a live voice call costs a whole extra cold
     start's worth of latency while the caller waits on the line.
  3. Idempotency on X-Request-Id in DynamoDB, so a Quadrivia retry (or a
     replay that somehow cleared layers 1 and 2) cannot double-book an agent.

PHI: the payload carries a patient phone number. It is never logged in the
clear — only a salted-free SHA-256 prefix (for correlation) and the last 4
digits (for a human to match against a call record) ever reach CloudWatch.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

# ── Constants ────────────────────────────────────────────────────────────
# Anti-replay window. ±5 min tolerates realistic clock skew between
# Quadrivia's cloud and AWS without leaving a wide replay window open.
TIMESTAMP_SKEW_SECONDS = 300

# Amazon Connect's own documented ceiling for StartTaskContact scheduling:
# ScheduledTime must be within 6 days of now. Rejecting here (400) instead of
# letting boto3 raise gives Quadrivia an actionable error instead of a 500.
MAX_SCHEDULE_AHEAD_SECONDS = 6 * 24 * 60 * 60

# Idempotency records only need to outlive a client's retry budget, not the
# task itself — 1 hour. Short TTL is also why this table holds no clinical
# record worth a Clinical backup tier (see the stack's tagging comment).
IDEMPOTENCY_TTL_SECONDS = 3600

_E164 = re.compile(r"^\+[1-9]\d{7,14}$")

# _verify_signature signs "{timestamp}.{request_id}.{raw_body}" — a plain
# concatenation, not a length-prefixed or otherwise unambiguous encoding.
# raw_body is always strict, complete JSON (json.loads on the whole string in
# _parse_body), which in practice blocks constructing a colliding split, but
# excluding "." (the delimiter itself) from request_id removes the
# ambiguity outright rather than relying on that as the only mitigation.
_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

_MAX_REASON_LEN = 200
_ALLOWED_LANGUAGES = frozenset({"en", "es"})

_SOURCE = "quadrivia_afterhours"


class _ValidationError(Exception):
    """Client sent something we can describe back without leaking internals."""


class _AuthError(Exception):
    """Signature/timestamp verification failed."""


class _DuplicateError(Exception):
    def __init__(self, contact_id: str | None) -> None:
        super().__init__("duplicate request id")
        self.contact_id = contact_id


# ── Module-level caches ──────────────────────────────────────────────────
# Fetched once per execution environment, not once per request: Secrets
# Manager adds ~30-80ms and this handler sits in the synchronous path of a
# live call. Rotating the secret therefore takes effect on the next cold
# start (or after a deliberate function-config touch), which is acceptable
# for a shared webhook secret and documented for whoever rotates it.
_secret_cache: str | None = None
_clients: dict[str, Any] = {}


def _boto(service: str):
    if service not in _clients:
        _clients[service] = boto3.client(service)
    return _clients[service]


def _reset_caches() -> None:
    """Test seam — production code never calls this."""
    global _secret_cache
    _secret_cache = None
    _clients.clear()


def _log(level: str, event: str, **fields: Any) -> None:
    """Minimal structured logger.

    Intentionally not vip_shared.StructuredLogger: this Lambda is bundled
    standalone (no shared layer) so a bug in shared code cannot take down the
    only inbound path for after-hours callbacks. Nothing PHI-bearing may be
    passed in — callers pass phone_hash/phone_last4, never `phone`.
    """
    print(json.dumps({"service": "quadrivia-callback", "level": level, "event": event, **fields}))


def _phone_hash(phone: str) -> str:
    """Keyed correlation hash — NOT a bare sha256.

    Logged alongside phone_last4 (see below), which knocks 4 digits off an
    E.164 number's unknown-digit space. A bare, unsalted sha256(phone) turns
    that into a real re-identification attack: anyone with CloudWatch read
    access to this log group (no Secrets Manager access needed) could brute
    force the remaining ~6 digits offline against the hash — at most ~10^6
    guesses, trivial to compute — and recover the full patient phone number
    from a log this module's own docstring says carries no PHI. Keying the
    hash with the webhook's own HMAC secret closes that: brute forcing now
    also requires the secret, which CloudWatch access alone doesn't grant.
    """
    return hmac.new(
        _get_secret().encode("utf-8"),
        f"phone-log-correlation:{phone}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:12]


def _phone_last4(phone: str) -> str:
    return f"***REDACTED***{phone[-4:]}" if len(phone) >= 4 else "***REDACTED***"


# ── Layer 2: HMAC + timestamp ────────────────────────────────────────────
def _get_secret() -> str:
    global _secret_cache
    if _secret_cache is None:
        arn = os.environ["HMAC_SECRET_ARN"]
        raw = _boto("secretsmanager").get_secret_value(SecretId=arn)["SecretString"]
        try:
            _secret_cache = str(json.loads(raw)["signingKey"])
        except (json.JSONDecodeError, KeyError, TypeError):
            # Tolerate a plain-string secret so a manual rotation that pastes
            # the raw key (instead of the {"signingKey": ...} envelope) does
            # not silently 500 every callback.
            _secret_cache = raw
    return _secret_cache


def _lower_headers(event: dict) -> dict[str, str]:
    # API Gateway v2 lowercases header names, but a direct/console test invoke
    # or a future REST-API front door does not — normalise rather than trust.
    return {str(k).lower(): str(v) for k, v in (event.get("headers") or {}).items()}


def _verify_timestamp(raw_timestamp: str | None, now: int) -> None:
    if not raw_timestamp:
        raise _AuthError("missing X-Timestamp")
    try:
        sent = int(raw_timestamp)
    except (TypeError, ValueError):
        raise _AuthError("X-Timestamp is not an epoch integer") from None
    if abs(now - sent) > TIMESTAMP_SKEW_SECONDS:
        raise _AuthError("X-Timestamp outside the accepted window")


def _verify_signature(
    raw_body: str, raw_timestamp: str, request_id: str, provided: str | None
) -> None:
    if not provided:
        raise _AuthError("missing X-Signature")
    # Timestamp AND request_id are inside the signed payload. Timestamp alone
    # would let an attacker keep a captured body/signature pair and just
    # refresh the header; request_id alone being unsigned would let anyone
    # who observes one valid (timestamp, body, signature) triple within the
    # skew window replay it under a NEW request_id and pass verification —
    # defeating layer 3's idempotency claim with a second, attacker-chosen
    # scheduled task for the same original request.
    expected = hmac.new(
        _get_secret().encode("utf-8"),
        f"{raw_timestamp}.{request_id}.{raw_body}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    supplied = provided[7:] if provided.startswith("sha256=") else provided
    if not hmac.compare_digest(expected, supplied):
        raise _AuthError("signature mismatch")


# ── Body validation ──────────────────────────────────────────────────────
def _parse_body(event: dict) -> tuple[str, dict]:
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        import base64

        # Decode for JSON parsing, but keep the *decoded* bytes as the signed
        # payload: API Gateway base64-encodes at its discretion, so signing
        # over the encoded form would break unpredictably.
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        raise _ValidationError("body is not valid JSON") from None
    if not isinstance(parsed, dict):
        raise _ValidationError("body must be a JSON object")
    return raw, parsed


def _require_phone(body: dict) -> str:
    phone = str(body.get("customerPhone") or "").strip()
    if not _E164.match(phone):
        # No echo of the value — an invalid string is still a phone number.
        raise _ValidationError("customerPhone must be E.164, e.g. +15551234567")
    return phone


def _require_reason(body: dict) -> str:
    reason = str(body.get("reason") or "").strip()
    if not reason:
        raise _ValidationError("reason is required")
    if len(reason) > _MAX_REASON_LEN:
        raise _ValidationError(f"reason must be <= {_MAX_REASON_LEN} characters")
    return reason


def _require_language(body: dict) -> str:
    language = str(body.get("language") or "").strip().lower()
    if language not in _ALLOWED_LANGUAGES:
        raise _ValidationError(
            f"language must be one of {sorted(_ALLOWED_LANGUAGES)}"
        )
    return language


def _require_scheduled_epoch(body: dict, now: int) -> int:
    raw = str(body.get("preferredCallbackTime") or "").strip()
    if not raw:
        raise _ValidationError("preferredCallbackTime is required")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise _ValidationError(
            "preferredCallbackTime must be ISO-8601 with a timezone offset"
        ) from None
    if parsed.tzinfo is None:
        # A naive timestamp would be silently assumed UTC and could schedule a
        # callback hours off — an agent calling a patient at 3am.
        raise _ValidationError("preferredCallbackTime must include a timezone offset")
    epoch = int(parsed.astimezone(timezone.utc).timestamp())
    if epoch <= now:
        raise _ValidationError("preferredCallbackTime is in the past")
    if epoch - now > MAX_SCHEDULE_AHEAD_SECONDS:
        raise _ValidationError(
            "preferredCallbackTime is more than 6 days ahead "
            "(Amazon Connect StartTaskContact limit)"
        )
    return epoch


# ── Layer 3: idempotency ─────────────────────────────────────────────────
def _reserve_request_id(request_id: str, now: int) -> None:
    """Claim request_id, or raise _DuplicateError if already claimed.

    One atomic conditional PutItem (never read-then-write, and never two
    sequential PutItems — see below) so concurrent retries can't both pass
    a check and book two tasks. The condition is an OR of two cases:

    1. attribute_not_exists(requestId) — nothing claimed it yet (including a
       slot a concurrent _release_reservation just freed).
    2. #ttl < :now — something claimed it, but that claim's own logical TTL
       has already passed. DynamoDB's background TTL sweep deletes expired
       items on its own schedule (documented as "usually within 48 hours",
       not instantly at expiry), so without this a legitimate retry sent
       just after the intended window can still hit a stale, not-yet-swept
       item and get a spurious 409 — reclaiming it here means a retry only
       ever waits out the real IDEMPOTENCY_TTL_SECONDS, not DynamoDB's own
       sweep latency on top of it.

    Both branches in ONE ConditionExpression, not two sequential PutItems:
    two separate attempts have a real TOCTOU race — if a concurrent
    _release_reservation deletes the item between this call's first
    (failed) attempt and its second, the second's `#ttl < :now` compares
    against a now-nonexistent item, evaluates false, and misreports a freed
    slot as still claimed. A single OR'd condition is race-free because
    DynamoDB evaluates it atomically against one consistent item state.

    `ttl` and `status` are DynamoDB reserved words and cannot appear as bare
    attribute names in an expression — that fails with ValidationException
    on every real DynamoDB call despite passing a mocked unit test, which
    never validates expression syntax. Both are placeholder-escaped (#ttl)
    here for that reason.
    """
    table = os.environ["IDEMPOTENCY_TABLE"]
    item = {
        "requestId": {"S": request_id},
        "status": {"S": "in_progress"},
        "createdAt": {"N": str(now)},
        "ttl": {"N": str(now + IDEMPOTENCY_TTL_SECONDS)},
    }
    try:
        _boto("dynamodb").put_item(
            TableName=table,
            Item=item,
            ConditionExpression="attribute_not_exists(requestId) OR #ttl < :now",
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues={":now": {"N": str(now)}},
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        raise _DuplicateError(_lookup_contact_id(table, request_id)) from None


def _release_reservation(request_id: str) -> None:
    """Undo a provisional claim after start_task_contact fails, so an

    immediate retry with the same request_id can re-claim it instead of
    waiting out the full TTL. Conditioned on status=in_progress: a record
    that already reached "completed" (a concurrent success racing this
    cleanup) must never be deleted out from under a caller who already has
    a real contactId. Best-effort — a failure here must not mask the
    original error that triggered the release, so it never raises.
    """
    try:
        _boto("dynamodb").delete_item(
            TableName=os.environ["IDEMPOTENCY_TABLE"],
            Key={"requestId": {"S": request_id}},
            # `status` is a DynamoDB reserved word — bare use fails every
            # real call with ValidationException despite passing a mocked
            # unit test. Escaped via #status the same way #ttl is above.
            ConditionExpression="#status = :in_progress",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":in_progress": {"S": "in_progress"}},
        )
    except Exception as exc:  # noqa: BLE001 - deliberately swallow, see docstring
        _log(
            "WARN",
            "reservation_release_failed",
            request_id=request_id,
            error=type(exc).__name__,
        )


def _lookup_contact_id(table: str, request_id: str) -> str | None:
    item = _boto("dynamodb").get_item(
        TableName=table,
        Key={"requestId": {"S": request_id}},
        ConsistentRead=True,
    ).get("Item") or {}
    return item.get("contactId", {}).get("S")


def _record_contact_id(request_id: str, contact_id: str, now: int) -> None:
    _boto("dynamodb").put_item(
        TableName=os.environ["IDEMPOTENCY_TABLE"],
        Item={
            "requestId": {"S": request_id},
            "status": {"S": "completed"},
            "contactId": {"S": contact_id},
            "createdAt": {"N": str(now)},
            "ttl": {"N": str(now + IDEMPOTENCY_TTL_SECONDS)},
        },
    )


# ── Connect ──────────────────────────────────────────────────────────────
def _start_scheduled_task(
    *,
    phone: str,
    reason: str,
    language: str,
    scheduled_epoch: int,
    request_id: str,
) -> str:
    response = _boto("connect").start_task_contact(
        InstanceId=os.environ["CONNECT_INSTANCE_ID"],
        # Exactly one of ContactFlowId / QuickConnectId / TaskTemplateId is
        # allowed by the API — the template owns the flow and field layout.
        TaskTemplateId=os.environ["TASK_TEMPLATE_ID"],
        Name="After-hours callback",
        Description=f"Callback requested via Quadrivia AI ({language})",
        # ScheduledTime (not DelaySeconds) — mutually exclusive, and the
        # absolute time is what Quadrivia actually agreed with the patient.
        ScheduledTime=datetime.fromtimestamp(scheduled_epoch, tz=timezone.utc),
        Attributes={
            "callback_phone": phone,
            "callback_reason": reason,
            "callback_language": language,
            "source": _SOURCE,
            "quadrivia_request_id": request_id,
        },
        References={
            "quadriviaRequestId": {"Value": request_id, "Type": "STRING"},
        },
        # Connect's own dedupe on top of our DynamoDB claim.
        ClientToken=request_id,
    )
    return response["ContactId"]


# ── Entrypoint ───────────────────────────────────────────────────────────
def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps(body),
    }


def lambda_handler(event: dict, context=None) -> dict:
    now = int(datetime.now(tz=timezone.utc).timestamp())
    headers = _lower_headers(event)
    request_id = str(headers.get("x-request-id") or "").strip()

    try:
        raw_body, body = _parse_body(event)

        # Checked before signature verification (not just as a matter of
        # order): request_id is now part of the signed payload below, so a
        # request missing it entirely cannot be signature-checked at all —
        # this stays a clear 400 rather than becoming a confusing 401.
        if not request_id:
            raise _ValidationError("X-Request-Id header is required")
        if not _REQUEST_ID.match(request_id):
            raise _ValidationError("X-Request-Id must match ^[A-Za-z0-9_-]{1,128}$")

        raw_timestamp = headers.get("x-timestamp")
        _verify_timestamp(raw_timestamp, now)
        _verify_signature(
            raw_body, str(raw_timestamp), request_id, headers.get("x-signature")
        )

        phone = _require_phone(body)
        reason = _require_reason(body)
        language = _require_language(body)
        scheduled_epoch = _require_scheduled_epoch(body, now)

        _reserve_request_id(request_id, now)

        try:
            contact_id = _start_scheduled_task(
                phone=phone,
                reason=reason,
                language=language,
                scheduled_epoch=scheduled_epoch,
                request_id=request_id,
            )
        except (ClientError, BotoCoreError):
            # Without this, a transient Connect failure leaves the
            # request_id claimed with no contactId for the full TTL — every
            # retry in that window hits _DuplicateError instead of ever
            # reaching Connect again. Release so the very next retry can
            # re-claim and actually try the call. BotoCoreError (not just
            # ClientError) matters here: a connection timeout or read
            # timeout talking to Connect raises BotoCoreError, not
            # ClientError — service-side throttles/5xx are ClientError, but
            # network-level failures are not, and both must release the
            # reservation the same way.
            _release_reservation(request_id)
            raise

        try:
            _record_contact_id(request_id, contact_id, now)
        except ClientError as exc:
            # The Connect task WAS created — this is a bookkeeping-only
            # failure. Do not let it fall through to the generic "except
            # ClientError -> 502" below: that would lie to Quadrivia about a
            # call that actually succeeded, and could provoke a retry that
            # creates a genuine SECOND task once the DynamoDB write recovers
            # (the retry's _reserve_request_id would succeed fresh, since
            # this record never got claimed as "completed"). Log at ERROR so
            # the real contactId is recoverable from CloudWatch, and still
            # tell the caller the truth: it succeeded.
            _log(
                "ERROR",
                "record_contact_id_failed",
                request_id=request_id,
                contact_id=contact_id,
                error=type(exc).__name__,
            )

        _log(
            "INFO",
            "callback_scheduled",
            request_id=request_id,
            contact_id=contact_id,
            scheduled_epoch=scheduled_epoch,
            language=language,
            phone_hash=_phone_hash(phone),
            phone_last4=_phone_last4(phone),
        )
        return _response(202, {"contactId": contact_id, "status": "SCHEDULED"})

    except _AuthError as exc:
        # 401, not 403: the request failed to authenticate itself. No detail
        # about which check failed — that is a signing oracle.
        _log("WARN", "auth_failed", request_id=request_id, reason=str(exc))
        return _response(401, {"error": {"code": "UNAUTHORIZED", "message": "Invalid signature or timestamp"}})

    except _DuplicateError as exc:
        _log("INFO", "duplicate_request", request_id=request_id)
        return _response(
            409,
            {
                "error": {"code": "DUPLICATE_REQUEST", "message": "requestId already processed"},
                "contactId": exc.contact_id,
            },
        )

    except _ValidationError as exc:
        _log("WARN", "validation_failed", request_id=request_id, reason=str(exc))
        return _response(400, {"error": {"code": "VALIDATION_ERROR", "message": str(exc)}})

    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "ClientError")
        # Never return the raw AWS message — it leaks ARNs and account ids.
        _log("ERROR", "aws_error", request_id=request_id, code=code)
        return _response(
            502,
            {"error": {"code": "UPSTREAM_ERROR", "message": "Could not schedule the callback"}},
        )

    except BotoCoreError as exc:
        # Network-level failure talking to an AWS service (e.g. a connection
        # or read timeout), not a service-side error — still an upstream
        # problem from the caller's point of view, not this Lambda's own
        # bug, so it gets the same 502 as ClientError rather than falling
        # through to the generic 500 below.
        _log("ERROR", "aws_error", request_id=request_id, code=type(exc).__name__)
        return _response(
            502,
            {"error": {"code": "UPSTREAM_ERROR", "message": "Could not schedule the callback"}},
        )

    except Exception as exc:  # pragma: no cover - defensive catch-all
        _log("ERROR", "unhandled_error", request_id=request_id, error=type(exc).__name__)
        return _response(500, {"error": {"code": "INTERNAL_ERROR", "message": "Unexpected error"}})
