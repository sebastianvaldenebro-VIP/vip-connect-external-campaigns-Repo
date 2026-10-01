"""Quadrivia after-hours callback webhook.

Quadrivia hosts an AI voice agent on another cloud that answers VIP's
after-hours calls. When the caller asks to be rung back during business
hours, Quadrivia POSTs here and this Lambda creates a *scheduled* Amazon
Connect task so a human agent picks it up at the requested time.

Trust boundary: this is machine-to-machine traffic from a third party, NOT a
human admin-ui user, so it deliberately does not share vip-admin-ui-api's
HttpApi or its Cognito Lambda authorizer. Three independent layers guard it:

  1. mTLS on the API Gateway custom domain (transport; see
     infra/lib/stacks/quadrivia-webhook-stack.ts), PLUS a client-certificate
     subject check inline here (_verify_client_certificate_subject) — the
     truststore alone trusts any cert Quadrivia's own CA ever issues, not
     just theirs specifically.
  2. HMAC-SHA256 over the raw body + a timestamp window, verified inline
     *here* rather than in a separate Lambda authorizer — a second Lambda in
     the synchronous path of a live voice call costs a whole extra cold
     start's worth of latency while the caller waits on the line.
  3. Idempotency on X-Request-Id in DynamoDB, so a Quadrivia retry (or a
     replay that somehow cleared layers 1 and 2) cannot double-book an agent.

Separately from those three trust-boundary layers, _classify_request_quality
rejects requests where every field is individually well-formed but the data
still looks like placeholder/test input rather than a genuine callback (see
that function's docstring). This is a data-quality gate, not a security
control — a well-authenticated request can still fail it.

PHI: the payload carries a patient phone number. It is never logged in the
clear — only an HMAC-keyed hash prefix (for correlation, keyed with the same
webhook secret so it can't be brute-forced from CloudWatch access alone) and
the last 4 digits (for a human to match against a call record) ever reach
CloudWatch.
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
from botocore.config import Config
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
# task itself. Quadrivia confirmed (2026-09-30) a retry could arrive hours
# later, not just seconds/minutes later — 1 hour was too short and would
# have let a legitimately-delayed retry fall outside the window and create
# a genuine duplicate task. 24 hours comfortably covers "hours later" with
# real margin. Still short enough that this table holds no clinical record
# worth a Clinical backup tier (see the stack's tagging comment).
IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60

_E164 = re.compile(r"^\+[1-9]\d{7,14}$")

# _verify_signature signs "{timestamp}.{request_id}.{raw_body}" — a plain
# concatenation, not a length-prefixed or otherwise unambiguous encoding.
# raw_body is always strict, complete JSON (json.loads on the whole string in
# _parse_body), which in practice blocks constructing a colliding split, but
# excluding "." (the delimiter itself) from request_id removes the
# ambiguity outright rather than relying on that as the only mitigation.
_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# 4096 is StartTaskContact's real, documented limit for Description — not
# an arbitrary choice. reason is sent as Description (shown to the agent in
# the CCP without any extra flow work) AND duplicated into
# Attributes["callback_reason"] (32,767-byte real limit there, so
# Description is the binding constraint when the same value goes to both).
# This is large enough to carry a full Quadrivia call transcript, not just a
# one-line summary — confirm that's actually wanted before relying on it,
# since a full transcript is far more PHI-dense than a short reason.
_MAX_REASON_LEN = 4096
_ALLOWED_LANGUAGES = frozenset({"en", "es"})

_SOURCE = "quadrivia_afterhours"


class _ValidationError(Exception):
    """Client sent something we can describe back without leaking internals."""


class _LowQualityDataError(_ValidationError):
    """Every field was individually well-formed, but the request still looks

    like placeholder/test data rather than a genuine callback captured from
    a real conversation (see _classify_request_quality). Subclasses
    _ValidationError so it still gets the same 400 response, but is logged
    under its own event name so these can be tracked and the thresholds
    tuned separately from plain format/range failures.
    """


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


# Well under this Lambda's own 3s total timeout — a slow patient lookup
# must never be the reason the whole callback request times out. connect/
# read timeouts, not retries: a retry here would burn the remaining budget
# for nothing, since _lookup_patient_status treats any failure as "error"
# either way.
_PATIENT_LOOKUP_CLIENT_CONFIG = Config(connect_timeout=1, read_timeout=1.5, retries={"max_attempts": 0})


def _lambda_client():
    if "lambda" not in _clients:
        _clients["lambda"] = boto3.client("lambda", config=_PATIENT_LOOKUP_CLIENT_CONFIG)
    return _clients["lambda"]


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


def _verify_client_certificate_subject(event: dict) -> None:
    """Reject the request unless the mTLS client certificate's subject

    matches Quadrivia's exactly.

    The truststore alone only proves the certificate was issued by
    Quadrivia's own CA — it does NOT restrict *which* certificate that CA
    issued. Quadrivia operates a private CA used only for this integration
    and flagged this gap themselves (2026-09-30): without this check, any
    certificate that CA ever issues, for any purpose, present or future,
    would pass mTLS. This is what actually enforces "only this one client",
    not just "only this one CA".

    For HTTP APIs (payload format v2) API Gateway puts the authenticated
    client certificate's properties at
    event.requestContext.authentication.clientCert — present on every
    request when the custom domain requires mTLS, not only when a Lambda
    authorizer is configured (confirmed against AWS's Lambda-authorizer
    payload documentation, which describes the same requestContext shape
    this plain proxy-integration Lambda also receives).
    """
    cert = (
        (event.get("requestContext") or {}).get("authentication") or {}
    ).get("clientCert") or {}
    subject_dn = cert.get("subjectDN")
    expected = os.environ["QUADRIVIA_CLIENT_CERT_SUBJECT_DN"]
    if subject_dn != expected:
        # No detail about what was expected/received — same reasoning as
        # every other _AuthError: that would be a signing/identity oracle.
        raise _AuthError("client certificate subject does not match")


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


_MIN_MEANINGFUL_REASON_LEN = 8
# Common placeholder/test values seen in integration testing — not real
# callback reasons, even though each passes the plain non-empty + length
# checks above.
_PLACEHOLDER_REASONS = frozenset(
    {"n/a", "na", "none", "test", "testing", "callback", "-", "tbd", "asdf"}
)


def _classify_request_quality(phone: str, reason: str) -> None:
    """Reject requests where every individual field is well-formed but the

    data still looks like placeholder/test data, not a real callback
    captured from a conversation with a patient.

    Deterministic checks, not a model — this is a starting point. Sebastian,
    2026-09-28: gate should be based on whether what Quadrivia sent us is
    good data, not a specific rule list; these thresholds are expected to
    be tuned once real Quadrivia traffic exists (same caveat as volume/
    timing in the integration doc — neither side has real data yet).
    """
    normalized_reason = " ".join(reason.strip().lower().split())
    if len(reason.strip()) < _MIN_MEANINGFUL_REASON_LEN or normalized_reason in _PLACEHOLDER_REASONS:
        raise _LowQualityDataError("reason does not look like a real callback reason")
    # All the same character (ignoring spaces) — e.g. "xxxxxxxxxx" — passes
    # the length check above but is still not a real reason.
    if len(set(normalized_reason.replace(" ", ""))) <= 1:
        raise _LowQualityDataError("reason does not look like a real callback reason")

    # phone is already E.164-validated by this point. Every digit after the
    # leading "+" being identical (e.g. +11111111111) is a classic
    # placeholder/test number, not a real patient phone number.
    if len(set(phone[1:])) <= 1:
        raise _LowQualityDataError("customerPhone does not look like a real phone number")


def _require_language(body: dict) -> str:
    language = str(body.get("language") or "").strip().lower()
    if language not in _ALLOWED_LANGUAGES:
        raise _ValidationError(
            f"language must be one of {sorted(_ALLOWED_LANGUAGES)}"
        )
    return language


def _read_billing_flag(body: dict) -> bool:
    """Whether Quadrivia classified this call as a billing question.

    This is the one classification only Quadrivia can make — what the
    conversation was actually about. It's optional and defaults to False:
    the routing decision it drives (billing -> PST queue) happens inside the
    Connect flow, not here, and a caller that omits it should not be
    validation-rejected over a routing hint. If present, it must be an
    actual JSON boolean — "true"/"yes" as a string is intentionally rejected
    rather than silently treated as truthy, since that ambiguity is exactly
    how a routing bug would slip in unnoticed.
    """
    value = body.get("isBillingQuestion")
    if value is None:
        return False
    if not isinstance(value, bool):
        raise _ValidationError("isBillingQuestion must be a boolean if present")
    return value


def _read_scheduled_epoch(body: dict, now: int) -> int | None:
    """Returns the requested epoch, or None if the patient/Quadrivia didn't

    give a specific time. None means "create the task right now" — see the
    ContactFlowId call site, which omits ScheduledTime entirely in that
    case rather than defaulting to some arbitrary time. Once created, an
    immediate task still waits in its queue like any other contact until an
    agent is available — see the after-hours routing design, there is no
    separate "urgent" path.
    """
    raw = str(body.get("preferredCallbackTime") or "").strip()
    if not raw:
        return None
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


# ── Routing input: existing-patient vs new-lead ─────────────────────────
def _lookup_patient_status(phone: str) -> str:
    """Classify existing-patient ("existing") vs new-lead ("new") the same

    way *MainInboundVoice does for voice calls — by invoking the same
    SOPS-ConnectPatientLookup Lambda, directly (not via a Connect flow).

    That Lambda reads the caller's number from
    Details.ContactData.CustomerEndpoint.Address. StartTaskContact has no
    CustomerEndpoint parameter at all — a Task contact would never populate
    that field the way a voice contact does — so a Connect flow invoking it
    for our task would silently always get a missing-number "error"
    response there, which *MainInboundVoice's own closed-hours branch
    defaults to the New Lead queue. Every Quadrivia callback would be
    misclassified as New Lead, with no visible error anywhere. Building the
    exact same synthetic event shape ourselves and invoking the Lambda
    directly sidesteps that gap entirely.

    Never allowed to fail the whole callback over this: any error (timeout,
    throttle, malformed response) returns "error" — the same value the real
    Lambda itself returns on its own DB failures — and the routing flow is
    expected to handle "error" the same way *MainInboundVoice already does
    (default to New Lead), not treat it as a reason to fail the request.
    """
    try:
        response = _lambda_client().invoke(
            FunctionName=os.environ["PATIENT_LOOKUP_FUNCTION_ARN"],
            InvocationType="RequestResponse",
            Payload=json.dumps(
                {"Details": {"ContactData": {"CustomerEndpoint": {"Address": phone}}}}
            ).encode("utf-8"),
        )
        payload = json.loads(response["Payload"].read())
        exists = payload.get("exists")
    except Exception as exc:  # noqa: BLE001 - deliberately never fail the callback over this
        _log("WARN", "patient_lookup_failed", error=type(exc).__name__)
        return "error"

    if exists == "yes":
        return "existing"
    if exists == "no":
        return "new"
    return "error"


# ── Connect ──────────────────────────────────────────────────────────────
def _start_scheduled_task(
    *,
    phone: str,
    reason: str,
    language: str,
    scheduled_epoch: int | None,
    request_id: str,
    is_billing_question: bool,
    patient_status: str,
) -> str:
    kwargs: dict[str, Any] = {}
    if scheduled_epoch is not None:
        # ScheduledTime (not DelaySeconds) — mutually exclusive, and the
        # absolute time is what Quadrivia actually agreed with the patient.
        # Omitted entirely (not e.g. "now") when no time was requested —
        # StartTaskContact runs the flow immediately when ScheduledTime is
        # absent, which is exactly the fallback we want.
        kwargs["ScheduledTime"] = datetime.fromtimestamp(scheduled_epoch, tz=timezone.utc)

    response = _boto("connect").start_task_contact(
        InstanceId=os.environ["CONNECT_INSTANCE_ID"],
        # ContactFlowId, not TaskTemplateId (exactly one of
        # ContactFlowId/QuickConnectId/TaskTemplateId is allowed by the API).
        # Publishing the first-ever Task Template on this Connect instance
        # forces EVERY agent to pick a template for EVERY manually-created
        # task from then on — an instance-wide behavior change, decided
        # 2026-09-29 to avoid. This points at a dedicated flow instead; that
        # flow reads is_billing_question below (billing -> PST queue) and
        # otherwise calls the same patient-lookup Lambda *MainInboundVoice
        # already uses to route existing-patient vs new-lead to their
        # respective voicemail queues.
        ContactFlowId=os.environ["CONTACT_FLOW_ID"],
        Name="After-hours callback",
        # Shown directly to the agent in the CCP with no extra flow work —
        # the whole point of also putting reason here, not just in
        # Attributes below.
        Description=reason,
        **kwargs,
        Attributes={
            "callback_phone": phone,
            "callback_reason": reason,
            "callback_language": language,
            # Both read by the Connect flow (see ContactFlowId comment
            # above) to decide routing. Connect attributes are
            # string-valued only. patient_status is "existing" / "new" /
            # "error" — resolved by _lookup_patient_status before this
            # call, not by the flow itself (see that function's docstring
            # for why the flow can't do this lookup itself).
            "is_billing_question": "true" if is_billing_question else "false",
            "patient_status": patient_status,
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
        # First check, before anything else: the truststore only proves
        # Quadrivia's CA issued this certificate, not that it's THE
        # certificate — see _verify_client_certificate_subject's docstring.
        _verify_client_certificate_subject(event)

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
        scheduled_epoch = _read_scheduled_epoch(body, now)
        is_billing_question = _read_billing_flag(body)
        _classify_request_quality(phone, reason)

        _reserve_request_id(request_id, now)
        patient_status = _lookup_patient_status(phone)

        try:
            contact_id = _start_scheduled_task(
                phone=phone,
                reason=reason,
                language=language,
                scheduled_epoch=scheduled_epoch,
                request_id=request_id,
                is_billing_question=is_billing_question,
                patient_status=patient_status,
            )
        except Exception:
            # Bare Exception, not just (ClientError, BotoCoreError): nothing
            # after this point in the block can have created the Connect
            # task, so ANY failure here — a service throttle, a network
            # timeout, or a plain bug — means release is always the correct
            # action, never the wrong one. This is also the basis for
            # telling Quadrivia any 5xx/429/timeout/connection failure is
            # safe to retry with the same X-Request-Id, not only 502:
            # without this being bare Exception, a failure mode outside
            # (ClientError, BotoCoreError) would leave the reservation
            # claimed with no contactId for the full TTL, and every retry in
            # that window would hit _DuplicateError instead of ever reaching
            # Connect again.
            _release_reservation(request_id)
            raise

        try:
            _record_contact_id(request_id, contact_id, now)
        except Exception as exc:  # noqa: BLE001 - see comment: any exception here means the same thing
            # The Connect task WAS created — this is a bookkeeping-only
            # failure, whatever caused it. Bare Exception, not just
            # ClientError: a network timeout (BotoCoreError) or even a plain
            # bug here means exactly the same thing — the task exists — so
            # it must not fall through to the generic 500 below. That would
            # lie to Quadrivia about a call that actually succeeded, and
            # could provoke a retry that creates a genuine SECOND task once
            # whatever failed here recovers (the retry's
            # _reserve_request_id would succeed fresh, since this record
            # never got claimed as "completed"). Log at ERROR so the real
            # contactId is recoverable from CloudWatch, and still tell the
            # caller the truth: it succeeded.
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

    except _LowQualityDataError as exc:
        # Distinct event name from validation_failed (checked first — it's
        # a subclass) so these can be tracked and the thresholds in
        # _classify_request_quality tuned separately once real traffic
        # exists.
        _log("WARN", "low_quality_data_rejected", request_id=request_id, reason=str(exc))
        return _response(400, {"error": {"code": "VALIDATION_ERROR", "message": str(exc)}})

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
        # Safe to retry with the same X-Request-Id, same as 502: this can
        # only be reached from before _reserve_request_id runs (nothing
        # claimed yet) or from the two exception blocks above, both of
        # which already release the reservation before re-raising. There is
        # no code path left where a 500 means the request_id is stuck
        # claimed with no way forward.
        _log("ERROR", "unhandled_error", request_id=request_id, error=type(exc).__name__)
        return _response(500, {"error": {"code": "INTERNAL_ERROR", "message": "Unexpected error"}})
