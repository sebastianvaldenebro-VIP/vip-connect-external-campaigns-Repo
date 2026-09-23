"""Unit tests for the Quadrivia after-hours callback webhook.

All phone numbers here are synthetic (555 exchange, never-assigned range).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import BotoCoreError, ClientError

import handler

SIGNING_KEY = "unit-test-signing-key-not-a-real-secret"
SYNTHETIC_PHONE = "+15555550123"
INSTANCE_ID = "6b3f17ba-68a4-472a-9b20-db1991507009"
TASK_TEMPLATE_ID = "11111111-2222-3333-4444-555555555555"
TABLE = "VipQuadriviaCallbackIdempotency"


# ── Fixtures ─────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("HMAC_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:165505826690:secret:fake")
    monkeypatch.setenv("IDEMPOTENCY_TABLE", TABLE)
    monkeypatch.setenv("CONNECT_INSTANCE_ID", INSTANCE_ID)
    monkeypatch.setenv("TASK_TEMPLATE_ID", TASK_TEMPLATE_ID)
    handler._reset_caches()
    yield
    handler._reset_caches()


@pytest.fixture
def aws(monkeypatch):
    """Stub every boto3 client the handler builds, keyed by service name."""
    secrets = MagicMock()
    secrets.get_secret_value.return_value = {
        "SecretString": json.dumps({"signingKey": SIGNING_KEY})
    }
    ddb = MagicMock()
    ddb.put_item.return_value = {}
    ddb.get_item.return_value = {}
    connect = MagicMock()
    connect.start_task_contact.return_value = {"ContactId": "contact-abc-123"}

    clients = {"secretsmanager": secrets, "dynamodb": ddb, "connect": connect}
    monkeypatch.setattr(handler.boto3, "client", lambda service, **_: clients[service])
    return clients


# ── Helpers ──────────────────────────────────────────────────────────────
def _sign(raw_body: str, timestamp: int, request_id: str, key: str = SIGNING_KEY) -> str:
    return hmac.new(
        key.encode("utf-8"),
        f"{timestamp}.{request_id}.{raw_body}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _valid_payload(**overrides) -> dict:
    payload = {
        "customerPhone": SYNTHETIC_PHONE,
        "preferredCallbackTime": (
            datetime.now(tz=timezone.utc) + timedelta(hours=10)
        ).isoformat(),
        "reason": "Asked about next-day appointment availability",
        "language": "en",
    }
    payload.update(overrides)
    return payload


def _event(
    *,
    body: dict | str | None = None,
    timestamp: int | str | None = None,
    signature: str | None = None,
    request_id: str | None = "req-0001",
    signing_key: str = SIGNING_KEY,
    base64_encode: bool = False,
    omit_timestamp: bool = False,
    omit_signature: bool = False,
) -> dict:
    raw = body if isinstance(body, str) else json.dumps(_valid_payload() if body is None else body)
    ts = int(datetime.now(tz=timezone.utc).timestamp()) if timestamp is None else timestamp
    headers: dict[str, str] = {}
    if not omit_timestamp:
        headers["X-Timestamp"] = str(ts)
    if not omit_signature:
        headers["X-Signature"] = (
            signature
            if signature is not None
            else _sign(raw, ts, request_id or "", signing_key)
        )
    if request_id is not None:
        headers["X-Request-Id"] = request_id

    event: dict = {"headers": headers, "body": raw}
    if base64_encode:
        event["body"] = base64.b64encode(raw.encode("utf-8")).decode("ascii")
        event["isBase64Encoded"] = True
    return event


def _body(response: dict) -> dict:
    return json.loads(response["body"])


# ── Happy path ───────────────────────────────────────────────────────────
def test_valid_request_creates_scheduled_task_and_returns_202(aws):
    response = handler.lambda_handler(_event())

    assert response["statusCode"] == 202
    assert _body(response) == {"contactId": "contact-abc-123", "status": "SCHEDULED"}

    kwargs = aws["connect"].start_task_contact.call_args[1]
    assert kwargs["InstanceId"] == INSTANCE_ID
    assert kwargs["TaskTemplateId"] == TASK_TEMPLATE_ID
    assert kwargs["Attributes"]["callback_phone"] == SYNTHETIC_PHONE
    assert kwargs["Attributes"]["source"] == "quadrivia_afterhours"
    assert kwargs["Attributes"]["quadrivia_request_id"] == "req-0001"
    assert kwargs["References"] == {
        "quadriviaRequestId": {"Value": "req-0001", "Type": "STRING"}
    }
    assert kwargs["ClientToken"] == "req-0001"
    # Exactly one of ContactFlowId/QuickConnectId/TaskTemplateId, and never
    # both ScheduledTime and DelaySeconds — the API rejects either combination.
    assert "ContactFlowId" not in kwargs and "QuickConnectId" not in kwargs
    assert "DelaySeconds" not in kwargs
    assert kwargs["ScheduledTime"].tzinfo == timezone.utc


def test_base64_encoded_body_is_signed_over_the_decoded_form(aws):
    response = handler.lambda_handler(_event(base64_encode=True))
    assert response["statusCode"] == 202


def test_sha256_prefixed_signature_header_is_accepted(aws):
    raw = json.dumps(_valid_payload())
    ts = int(datetime.now(tz=timezone.utc).timestamp())
    event = _event(body=raw, timestamp=ts, signature=f"sha256={_sign(raw, ts, 'req-0001')}")
    assert handler.lambda_handler(event)["statusCode"] == 202


def test_plain_string_secret_without_json_envelope_still_verifies(aws):
    aws["secretsmanager"].get_secret_value.return_value = {"SecretString": SIGNING_KEY}
    handler._reset_caches()
    assert handler.lambda_handler(_event())["statusCode"] == 202


def test_secret_is_fetched_once_per_execution_environment(aws):
    handler.lambda_handler(_event(request_id="req-a"))
    handler.lambda_handler(_event(request_id="req-b"))
    assert aws["secretsmanager"].get_secret_value.call_count == 1


# ── Layer 2: signature ───────────────────────────────────────────────────
def test_wrong_signing_key_is_rejected_with_401(aws):
    response = handler.lambda_handler(_event(signing_key="attacker-key"))
    assert response["statusCode"] == 401
    assert _body(response)["error"]["code"] == "UNAUTHORIZED"
    aws["connect"].start_task_contact.assert_not_called()


def test_tampered_body_invalidates_the_signature(aws):
    raw = json.dumps(_valid_payload())
    ts = int(datetime.now(tz=timezone.utc).timestamp())
    tampered = json.dumps(_valid_payload(customerPhone="+15555559999"))
    event = _event(body=tampered, timestamp=ts, signature=_sign(raw, ts, "req-0001"))
    assert handler.lambda_handler(event)["statusCode"] == 401


def test_replaying_a_valid_signature_under_a_different_request_id_is_rejected(aws):
    """A signature is only valid for the request_id it was computed over.

    Otherwise anyone who observes one valid (timestamp, body, signature)
    triple could resend it with a new, attacker-chosen X-Request-Id and pass
    verification — creating a second scheduled task for the same original
    request and defeating layer 3's idempotency entirely.
    """
    raw = json.dumps(_valid_payload())
    ts = int(datetime.now(tz=timezone.utc).timestamp())
    signature_for_req_a = _sign(raw, ts, "req-a")
    event = _event(
        body=raw, timestamp=ts, request_id="req-b", signature=signature_for_req_a
    )
    response = handler.lambda_handler(event)
    assert response["statusCode"] == 401
    aws["connect"].start_task_contact.assert_not_called()


def test_missing_signature_header_is_rejected(aws):
    assert handler.lambda_handler(_event(omit_signature=True))["statusCode"] == 401


def test_auth_response_does_not_reveal_which_check_failed(aws):
    bad_sig = _body(handler.lambda_handler(_event(signature="deadbeef")))
    bad_ts = _body(handler.lambda_handler(_event(timestamp=1, signature="deadbeef")))
    assert bad_sig["error"] == bad_ts["error"]


# ── Layer 2: timestamp window ────────────────────────────────────────────
def test_timestamp_older_than_the_window_is_rejected(aws):
    stale = int(datetime.now(tz=timezone.utc).timestamp()) - (handler.TIMESTAMP_SKEW_SECONDS + 60)
    response = handler.lambda_handler(_event(timestamp=stale))
    assert response["statusCode"] == 401
    aws["connect"].start_task_contact.assert_not_called()


def test_timestamp_too_far_in_the_future_is_rejected(aws):
    ahead = int(datetime.now(tz=timezone.utc).timestamp()) + (handler.TIMESTAMP_SKEW_SECONDS + 60)
    assert handler.lambda_handler(_event(timestamp=ahead))["statusCode"] == 401


def test_missing_timestamp_header_is_rejected(aws):
    assert handler.lambda_handler(_event(omit_timestamp=True))["statusCode"] == 401


def test_non_integer_timestamp_is_rejected(aws):
    assert handler.lambda_handler(_event(timestamp="not-an-epoch"))["statusCode"] == 401


# ── Layer 3: idempotency ─────────────────────────────────────────────────
def _conditional_failure() -> ClientError:
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
        "PutItem",
    )


def test_duplicate_request_id_returns_409_without_creating_a_second_task(aws):
    aws["dynamodb"].put_item.side_effect = _conditional_failure()
    aws["dynamodb"].get_item.return_value = {
        "Item": {"requestId": {"S": "req-0001"}, "contactId": {"S": "contact-abc-123"}}
    }

    response = handler.lambda_handler(_event())

    assert response["statusCode"] == 409
    assert _body(response)["error"]["code"] == "DUPLICATE_REQUEST"
    assert _body(response)["contactId"] == "contact-abc-123"
    aws["connect"].start_task_contact.assert_not_called()


def test_duplicate_still_returns_409_when_the_prior_contact_id_is_unknown(aws):
    aws["dynamodb"].put_item.side_effect = _conditional_failure()
    aws["dynamodb"].get_item.return_value = {}
    response = handler.lambda_handler(_event())
    assert response["statusCode"] == 409
    assert _body(response)["contactId"] is None


def test_genuinely_live_reservation_still_409s(aws):
    """A single failed conditional PutItem — real DynamoDB evaluates the

    OR'd condition (attribute_not_exists(requestId) OR #ttl < :now)
    atomically in one call; there is no second attempt to make here. This
    proves a still-live duplicate correctly 409s without ever reaching
    Connect.
    """
    aws["dynamodb"].put_item.side_effect = _conditional_failure()
    aws["dynamodb"].get_item.return_value = {
        "Item": {"requestId": {"S": "req-0001"}, "contactId": {"S": "contact-abc-123"}}
    }
    response = handler.lambda_handler(_event())
    assert response["statusCode"] == 409
    assert _body(response)["contactId"] == "contact-abc-123"
    aws["connect"].start_task_contact.assert_not_called()


def test_reservation_condition_covers_fresh_and_logically_expired_cases(aws):
    """One atomic conditional PutItem, not two sequential ones — see the

    module docstring on _reserve_request_id for why two sequential attempts
    would have a real TOCTOU race against a concurrent release. `ttl` and
    `status` are DynamoDB reserved words, so both must be escaped via
    ExpressionAttributeNames (#ttl, #status) — a bare name here passes every
    mocked test and fails with ValidationException on every real call.
    """
    handler.lambda_handler(_event())
    reserve = aws["dynamodb"].put_item.call_args_list[0][1]
    assert reserve["ConditionExpression"] == "attribute_not_exists(requestId) OR #ttl < :now"
    assert reserve["ExpressionAttributeNames"] == {"#ttl": "ttl"}
    assert "N" in reserve["ExpressionAttributeValues"][":now"]
    assert int(reserve["Item"]["ttl"]["N"]) > int(reserve["Item"]["createdAt"]["N"])
    assert reserve["Item"]["status"]["S"] == "in_progress"

    completed = aws["dynamodb"].put_item.call_args_list[1][1]
    assert completed["Item"]["contactId"]["S"] == "contact-abc-123"
    assert completed["Item"]["status"]["S"] == "completed"


def test_missing_request_id_header_is_a_400(aws):
    response = handler.lambda_handler(_event(request_id=None))
    assert response["statusCode"] == 400
    aws["connect"].start_task_contact.assert_not_called()


def test_request_id_outside_the_safe_charset_is_rejected(aws):
    """Restricting the charset removes any signing-canonicalization

    ambiguity outright, rather than relying solely on raw_body's strict
    whole-string JSON parsing to block a colliding split.
    """
    raw = json.dumps(_valid_payload())
    ts = int(datetime.now(tz=timezone.utc).timestamp())
    event = _event(
        body=raw,
        timestamp=ts,
        request_id="req.with.dots.that.could.confuse.the.delimiter",
        signature=_sign(raw, ts, "req.with.dots.that.could.confuse.the.delimiter"),
    )
    response = handler.lambda_handler(event)
    assert response["statusCode"] == 400
    aws["connect"].start_task_contact.assert_not_called()


def test_non_conditional_dynamodb_error_is_not_swallowed_as_a_duplicate(aws):
    aws["dynamodb"].put_item.side_effect = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow down"}},
        "PutItem",
    )
    response = handler.lambda_handler(_event())
    assert response["statusCode"] == 502
    assert _body(response)["error"]["code"] == "UPSTREAM_ERROR"


# ── Body validation ──────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "bad_phone",
    [
        "5555550123",  # no leading +
        "+1555",  # shorter than E.164's 8-digit minimum
        "+12345678901234567",  # longer than E.164's 15-digit maximum
        "not-a-phone",
        "",
        "+0555550123",  # country code cannot start with 0
        "+1 555 555 0123",  # spaces are not E.164
        "+1555555012x",  # trailing extension
    ],
)
def test_malformed_phone_is_rejected_with_400(aws, bad_phone):
    response = handler.lambda_handler(_event(body=_valid_payload(customerPhone=bad_phone)))
    assert response["statusCode"] == 400
    assert _body(response)["error"]["code"] == "VALIDATION_ERROR"
    aws["connect"].start_task_contact.assert_not_called()


def test_validation_error_message_never_echoes_the_rejected_phone(aws):
    response = handler.lambda_handler(_event(body=_valid_payload(customerPhone="5555550123")))
    assert "5555550123" not in response["body"]


def test_callback_time_in_the_past_is_rejected(aws):
    past = (datetime.now(tz=timezone.utc) - timedelta(minutes=5)).isoformat()
    response = handler.lambda_handler(_event(body=_valid_payload(preferredCallbackTime=past)))
    assert response["statusCode"] == 400
    assert "past" in _body(response)["error"]["message"]
    aws["connect"].start_task_contact.assert_not_called()


def test_callback_time_beyond_six_days_is_rejected(aws):
    far = (datetime.now(tz=timezone.utc) + timedelta(days=6, hours=1)).isoformat()
    response = handler.lambda_handler(_event(body=_valid_payload(preferredCallbackTime=far)))
    assert response["statusCode"] == 400
    assert "6 days" in _body(response)["error"]["message"]
    aws["connect"].start_task_contact.assert_not_called()


def test_callback_time_just_inside_six_days_is_accepted(aws):
    near = (datetime.now(tz=timezone.utc) + timedelta(days=5, hours=23)).isoformat()
    response = handler.lambda_handler(_event(body=_valid_payload(preferredCallbackTime=near)))
    assert response["statusCode"] == 202


def test_naive_callback_time_without_timezone_is_rejected(aws):
    naive = (datetime.now(tz=timezone.utc) + timedelta(hours=4)).replace(tzinfo=None).isoformat()
    response = handler.lambda_handler(_event(body=_valid_payload(preferredCallbackTime=naive)))
    assert response["statusCode"] == 400
    assert "timezone" in _body(response)["error"]["message"]


def test_zulu_suffix_callback_time_is_accepted(aws):
    zulu = (
        (datetime.now(tz=timezone.utc) + timedelta(hours=6))
        .replace(microsecond=0, tzinfo=None)
        .isoformat()
        + "Z"
    )
    assert handler.lambda_handler(_event(body=_valid_payload(preferredCallbackTime=zulu)))["statusCode"] == 202


def test_non_utc_offset_is_converted_to_utc_epoch(aws):
    ny = timezone(timedelta(hours=-4))
    target = (datetime.now(tz=ny) + timedelta(hours=8)).replace(microsecond=0)
    handler.lambda_handler(_event(body=_valid_payload(preferredCallbackTime=target.isoformat())))
    scheduled = aws["connect"].start_task_contact.call_args[1]["ScheduledTime"]
    assert scheduled == target.astimezone(timezone.utc).replace(microsecond=0)


@pytest.mark.parametrize("bad_time", ["", "tomorrow at 9", "2026-13-45T99:00:00Z"])
def test_unparseable_callback_time_is_rejected(aws, bad_time):
    response = handler.lambda_handler(_event(body=_valid_payload(preferredCallbackTime=bad_time)))
    assert response["statusCode"] == 400


def test_missing_reason_is_rejected(aws):
    response = handler.lambda_handler(_event(body=_valid_payload(reason="  ")))
    assert response["statusCode"] == 400
    assert "reason" in _body(response)["error"]["message"]


def test_overlong_reason_is_rejected(aws):
    response = handler.lambda_handler(_event(body=_valid_payload(reason="x" * 201)))
    assert response["statusCode"] == 400


@pytest.mark.parametrize("bad_language", ["", "fr", "klingon"])
def test_unsupported_language_is_rejected(aws, bad_language):
    response = handler.lambda_handler(_event(body=_valid_payload(language=bad_language)))
    assert response["statusCode"] == 400


def test_language_is_case_insensitive(aws):
    assert handler.lambda_handler(_event(body=_valid_payload(language="ES")))["statusCode"] == 202


def test_non_json_body_is_rejected_with_400(aws):
    raw = "this is not json"
    ts = int(datetime.now(tz=timezone.utc).timestamp())
    response = handler.lambda_handler(_event(body=raw, timestamp=ts, signature=_sign(raw, ts, "req-0001")))
    assert response["statusCode"] == 400


def test_json_array_body_is_rejected_with_400(aws):
    raw = json.dumps([1, 2, 3])
    ts = int(datetime.now(tz=timezone.utc).timestamp())
    response = handler.lambda_handler(_event(body=raw, timestamp=ts, signature=_sign(raw, ts, "req-0001")))
    assert response["statusCode"] == 400


def test_missing_headers_key_entirely_is_rejected_not_crashed(aws):
    # request_id is now checked before signature/timestamp (it's part of the
    # signed payload — see _verify_signature), so an entirely headerless
    # request is a 400 for the missing X-Request-Id, not a 401.
    response = handler.lambda_handler({"body": json.dumps(_valid_payload())})
    assert response["statusCode"] == 400


def test_uppercase_header_names_are_normalised(aws):
    event = _event()
    event["headers"] = {k.upper(): v for k, v in event["headers"].items()}
    assert handler.lambda_handler(event)["statusCode"] == 202


# ── Connect error handling ───────────────────────────────────────────────
def test_connect_failure_returns_502_without_leaking_the_aws_message(aws):
    aws["connect"].start_task_contact.side_effect = ClientError(
        {
            "Error": {
                "Code": "InvalidRequestException",
                "Message": "arn:aws:connect:us-east-1:165505826690:instance/secret is bad",
            }
        },
        "StartTaskContact",
    )
    response = handler.lambda_handler(_event())
    assert response["statusCode"] == 502
    assert "arn:aws:connect" not in response["body"]
    assert "165505826690" not in response["body"]


def test_connect_failure_releases_the_reservation_so_a_retry_can_reclaim_it(aws):
    """Without this release, the request_id stays claimed with no contactId

    for the full TTL and every retry in that window gets 409'd instead of
    ever reaching Connect again — a transient throttle silently drops the
    callback for up to an hour.
    """
    aws["connect"].start_task_contact.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
        "StartTaskContact",
    )
    response = handler.lambda_handler(_event(request_id="req-retry-me"))
    assert response["statusCode"] == 502

    delete_call = aws["dynamodb"].delete_item.call_args
    assert delete_call is not None
    assert delete_call.kwargs["Key"] == {"requestId": {"S": "req-retry-me"}}
    # `status` is a DynamoDB reserved word — must be escaped via
    # ExpressionAttributeNames, not used bare (fails with ValidationException
    # on every real call despite passing a mocked test).
    assert delete_call.kwargs["ConditionExpression"] == "#status = :in_progress"
    assert delete_call.kwargs["ExpressionAttributeNames"] == {"#status": "status"}


def test_connect_network_failure_also_releases_the_reservation(aws):
    """BotoCoreError (a connection/read timeout talking to Connect), not

    just ClientError (a service-side throttle/5xx), must also release the
    reservation and return 502 — a network-level failure is just as
    upstream-not-our-bug as a service error, and without this it bypasses
    the release entirely and falls through to a generic 500.
    """
    aws["connect"].start_task_contact.side_effect = BotoCoreError()
    response = handler.lambda_handler(_event(request_id="req-network-blip"))
    assert response["statusCode"] == 502
    assert _body(response)["error"]["code"] == "UPSTREAM_ERROR"

    delete_call = aws["dynamodb"].delete_item.call_args
    assert delete_call is not None
    assert delete_call.kwargs["Key"] == {"requestId": {"S": "req-network-blip"}}


def test_reservation_release_failure_does_not_mask_the_original_502(aws):
    aws["connect"].start_task_contact.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
        "StartTaskContact",
    )
    aws["dynamodb"].delete_item.side_effect = RuntimeError("network blip")
    response = handler.lambda_handler(_event())
    assert response["statusCode"] == 502
    assert _body(response)["error"]["code"] == "UPSTREAM_ERROR"


def test_record_contact_id_failure_after_a_real_connect_success_still_returns_202(aws, capsys):
    """The Connect task was actually created — this must not be reported as

    a failure. Returning 502/409 here would lie to Quadrivia about a call
    that succeeded, and could provoke a retry that creates a genuine SECOND
    task once the DynamoDB write recovers (this record never reached
    "completed", so a fresh reservation would succeed). The real contactId
    must still be recoverable from CloudWatch.
    """
    aws["dynamodb"].put_item.side_effect = [
        {},  # the reservation (_reserve_request_id) succeeds
        ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow down"}},
            "PutItem",
        ),  # the completion write (_record_contact_id) fails
    ]
    response = handler.lambda_handler(_event())
    assert response["statusCode"] == 202
    assert _body(response)["contactId"] == "contact-abc-123"

    logs = capsys.readouterr().out
    assert "record_contact_id_failed" in logs
    assert "contact-abc-123" in logs


# ── PHI in logs ──────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "scenario",
    ["success", "bad_signature", "duplicate", "bad_phone", "connect_error"],
)
def test_no_log_line_ever_contains_the_full_phone_number(aws, capsys, scenario):
    """The phone number must never reach CloudWatch in the clear.

    Covers the failure paths too — a rejected request still holds a real
    patient number, and error handlers are the usual place a redaction slips.
    """
    if scenario == "bad_signature":
        event = _event(signing_key="attacker-key")
    elif scenario == "duplicate":
        aws["dynamodb"].put_item.side_effect = _conditional_failure()
        event = _event()
    elif scenario == "bad_phone":
        event = _event(body=_valid_payload(customerPhone="5555550123"))
    else:
        if scenario == "connect_error":
            aws["connect"].start_task_contact.side_effect = ClientError(
                {"Error": {"Code": "InternalServiceException", "Message": "boom"}},
                "StartTaskContact",
            )
        event = _event()

    handler.lambda_handler(event)

    # Read once — a second readouterr() would return an empty buffer and make
    # this assertion pass for the wrong reason.
    outerr = capsys.readouterr()
    captured = outerr.out + outerr.err
    assert SYNTHETIC_PHONE not in captured
    assert SYNTHETIC_PHONE.lstrip("+") not in captured
    # The digits without the country code must not leak either.
    assert "5555550123" not in captured


def test_success_log_carries_a_hash_and_redacted_last4_for_correlation(aws, capsys):
    handler.lambda_handler(_event())
    lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    scheduled = next(entry for entry in lines if entry["event"] == "callback_scheduled")
    assert scheduled["phone_last4"] == "***REDACTED***0123"
    assert scheduled["phone_hash"] == hmac.new(
        SIGNING_KEY.encode("utf-8"),
        f"phone-log-correlation:{SYNTHETIC_PHONE}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:12]
    assert "customerPhone" not in json.dumps(scheduled)


def test_phone_hash_is_keyed_not_a_bare_hash_an_attacker_could_brute_force(aws, capsys):
    """phone_hash is logged next to phone_last4 (cleartext last 4 digits),

    which knocks 4 digits off an E.164 number's unknown-digit space. If the
    hash were a bare, unsalted sha256(phone), anyone with CloudWatch read
    access (no Secrets Manager access) could brute force the remaining ~6
    digits offline against it — at most ~10^6 guesses — and recover the full
    patient phone number. Keying it with the webhook secret means the hash
    is different for the same phone number under a different key, so a
    CloudWatch-only reader cannot verify a brute-force guess at all.
    """
    handler.lambda_handler(_event())
    logged_hash = next(
        json.loads(line)
        for line in capsys.readouterr().out.strip().splitlines()
        if json.loads(line)["event"] == "callback_scheduled"
    )["phone_hash"]

    bare_sha256 = hashlib.sha256(SYNTHETIC_PHONE.encode("utf-8")).hexdigest()[:12]
    assert logged_hash != bare_sha256

    differently_keyed = hmac.new(
        b"a-different-secret-entirely",
        f"phone-log-correlation:{SYNTHETIC_PHONE}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:12]
    assert logged_hash != differently_keyed


def test_log_never_contains_the_raw_request_body(aws, capsys):
    reason = "Patient Jane Doe wants a callback about her MRI"
    handler.lambda_handler(_event(body=_valid_payload(reason=reason)))
    assert reason not in capsys.readouterr().out


def test_phone_last4_helper_degrades_safely_on_short_input():
    assert handler._phone_last4("+1") == "***REDACTED***"
