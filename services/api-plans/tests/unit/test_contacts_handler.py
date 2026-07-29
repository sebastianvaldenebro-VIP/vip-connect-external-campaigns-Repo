"""Tests for handlers/contacts.py."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

# ── Mock vip_shared before importing the handler ─────────────────────────────

_mock_caller = MagicMock()
_mock_caller.sub = "test-sub"
_mock_caller.email = "test@example.com"
_mock_caller.ip_address = "127.0.0.1"
_mock_caller.user_agent = "test-agent"

_mock_http = MagicMock()
_mock_http.json_response.side_effect = lambda status, body: {
    "statusCode": status,
    "body": body,
}
_mock_http.error_response.side_effect = lambda status, code, message, **_kw: {
    "statusCode": status,
    "body": {"error": {"code": code, "message": message}},
}
_mock_http.extract_caller.return_value = _mock_caller

_mock_audit_recorder = MagicMock()
_mock_audit_mod = MagicMock()
_mock_audit_mod.build_from_env.return_value = _mock_audit_recorder

sys.modules.setdefault("vip_shared", MagicMock())
sys.modules.setdefault("vip_shared.application", MagicMock())
sys.modules["vip_shared.application.http"] = _mock_http
sys.modules.setdefault("vip_shared.infrastructure", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure.persistence", MagicMock())
sys.modules["vip_shared.infrastructure.persistence.audit"] = _mock_audit_mod

_ENV = {
    "CONNECT_INSTANCE_ID": "6b3f17ba-68a4-472a-9b20-db1991507009",
    "RECORDINGS_BUCKET": "amazon-connect-c5a2158755eb",
    "VOICEMAIL_BUCKET": "vmx3-recordings-vipmedicalgroup",
}

_VALID_UUID = "002158e7-319f-447c-a866-400078debd34"
_INITIATION_TS = datetime(2026, 7, 15, 14, 30, 0, tzinfo=timezone.utc)


def _load_handler():
    with patch.dict(os.environ, _ENV):
        with patch("boto3.client"):
            import importlib
            import handlers.contacts as contacts

            importlib.reload(contacts)
            return contacts


# ── Input validation ──────────────────────────────────────────────────────────


def test_invalid_uuid_returns_400():
    handler = _load_handler()
    with patch.dict(os.environ, _ENV):
        result = handler.get_artifacts({}, {"contactId": "not-a-uuid"})
    assert result["statusCode"] == 400


def test_empty_contact_id_returns_400():
    handler = _load_handler()
    with patch.dict(os.environ, _ENV):
        result = handler.get_artifacts({}, {"contactId": ""})
    assert result["statusCode"] == 400


def test_missing_contact_id_returns_400():
    handler = _load_handler()
    with patch.dict(os.environ, _ENV):
        result = handler.get_artifacts({}, {})
    assert result["statusCode"] == 400


def test_missing_env_vars_returns_500():
    """Code-only deploy before CDK must return 500 on this route, not crash the Lambda."""
    handler = _load_handler()
    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_RECORDINGS_BUCKET", ""),
    ):
        result = handler.get_artifacts({}, {"contactId": _VALID_UUID})
    assert result["statusCode"] == 500


# ── describe_contact failures ─────────────────────────────────────────────────


def test_contact_not_found_returns_404():
    handler = _load_handler()
    mock_connect = MagicMock()
    mock_connect.describe_contact.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "Not found"}},
        "DescribeContact",
    )

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_connect_client", mock_connect),
    ):
        result = handler.get_artifacts({}, {"contactId": _VALID_UUID})

    assert result["statusCode"] == 404


def test_connect_error_returns_400():
    handler = _load_handler()
    mock_connect = MagicMock()
    mock_connect.describe_contact.side_effect = ClientError(
        {"Error": {"Code": "InternalServiceException", "Message": "boom"}},
        "DescribeContact",
    )

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_connect_client", mock_connect),
    ):
        result = handler.get_artifacts({}, {"contactId": _VALID_UUID})

    assert result["statusCode"] == 400


# ── Happy paths ───────────────────────────────────────────────────────────────


def test_all_artifacts_found_returns_200_with_three_urls():
    handler = _load_handler()

    mock_connect = MagicMock()
    mock_connect.describe_contact.return_value = {
        "Contact": {"InitiationTimestamp": _INITIATION_TS}
    }

    mock_s3 = MagicMock()
    mock_s3.list_objects_v2.return_value = {"Contents": [{"Key": "some/key.wav"}]}
    mock_s3.generate_presigned_url.return_value = "https://s3.example.com/presigned"

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_connect_client", mock_connect),
        patch.object(handler, "_s3_client", mock_s3),
    ):
        result = handler.get_artifacts({}, {"contactId": _VALID_UUID})

    assert result["statusCode"] == 200
    body = result["body"]
    assert body["contactId"] == _VALID_UUID
    assert body["voicemail"] == "https://s3.example.com/presigned"
    assert body["recording"] == "https://s3.example.com/presigned"
    assert body["transcript"] == "https://s3.example.com/presigned"
    assert body["expiresInSeconds"] == 900


def test_only_voicemail_exists_recording_and_transcript_null():
    handler = _load_handler()

    mock_connect = MagicMock()
    mock_connect.describe_contact.return_value = {
        "Contact": {"InitiationTimestamp": _INITIATION_TS}
    }

    mock_s3 = MagicMock()

    def list_side_effect(Bucket, Prefix, MaxKeys):  # noqa: N803
        if "vmx3" in Bucket:
            return {"Contents": [{"Key": f"{_VALID_UUID}.wav"}]}
        return {"Contents": []}

    mock_s3.list_objects_v2.side_effect = list_side_effect
    mock_s3.generate_presigned_url.return_value = "https://s3.example.com/voicemail"

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_connect_client", mock_connect),
        patch.object(handler, "_s3_client", mock_s3),
    ):
        result = handler.get_artifacts({}, {"contactId": _VALID_UUID})

    assert result["statusCode"] == 200
    body = result["body"]
    assert body["voicemail"] == "https://s3.example.com/voicemail"
    assert body["recording"] is None
    assert body["transcript"] is None


def test_no_artifacts_found_returns_200_all_null():
    handler = _load_handler()

    mock_connect = MagicMock()
    mock_connect.describe_contact.return_value = {
        "Contact": {"InitiationTimestamp": _INITIATION_TS}
    }

    mock_s3 = MagicMock()
    mock_s3.list_objects_v2.return_value = {"Contents": []}

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_connect_client", mock_connect),
        patch.object(handler, "_s3_client", mock_s3),
    ):
        result = handler.get_artifacts({}, {"contactId": _VALID_UUID})

    assert result["statusCode"] == 200
    body = result["body"]
    assert body["voicemail"] is None
    assert body["recording"] is None
    assert body["transcript"] is None


# ── Audit logging ─────────────────────────────────────────────────────────────


def test_audit_record_written_on_success():
    """PHI access must be recorded in the audit log on every successful lookup."""
    handler = _load_handler()

    mock_connect = MagicMock()
    mock_connect.describe_contact.return_value = {
        "Contact": {"InitiationTimestamp": _INITIATION_TS}
    }

    mock_s3 = MagicMock()
    mock_s3.list_objects_v2.return_value = {"Contents": [{"Key": "key.wav"}]}
    mock_s3.generate_presigned_url.return_value = "https://s3.example.com/url"

    _mock_audit_recorder.reset_mock()

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_connect_client", mock_connect),
        patch.object(handler, "_s3_client", mock_s3),
    ):
        handler.get_artifacts({}, {"contactId": _VALID_UUID})

    _mock_audit_recorder.record.assert_called_once()
    call_kwargs = _mock_audit_recorder.record.call_args.kwargs
    assert call_kwargs["entity_type"] == "contact_artifacts"
    assert call_kwargs["entity_id"] == _VALID_UUID
    assert call_kwargs["action"] == "READ"
    assert call_kwargs["actor_email"] == "test@example.com"


# ── Date prefix derivation ────────────────────────────────────────────────────


def test_date_prefix_derived_from_initiation_timestamp_in_utc():
    """S3 prefix must use the UTC date, not local time."""
    handler = _load_handler()

    # Contact initiated at 2026-03-05 23:00:00-05:00 → UTC = 2026-03-06 04:00:00
    import datetime as _dt

    eastern = datetime(
        2026,
        3,
        5,
        23,
        0,
        0,
        tzinfo=_dt.timezone(offset=_dt.timedelta(hours=-5)),
    )
    expected_prefix = "2026/03/06"

    mock_connect = MagicMock()
    mock_connect.describe_contact.return_value = {
        "Contact": {"InitiationTimestamp": eastern}
    }

    captured_prefixes: list[str] = []

    mock_s3 = MagicMock()

    def capture_list(Bucket, Prefix, MaxKeys):  # noqa: N803
        captured_prefixes.append(Prefix)
        return {"Contents": []}

    mock_s3.list_objects_v2.side_effect = capture_list

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_connect_client", mock_connect),
        patch.object(handler, "_s3_client", mock_s3),
    ):
        handler.get_artifacts({}, {"contactId": _VALID_UUID})

    recording_prefix = next(p for p in captured_prefixes if "CallRecordings" in p)
    assert expected_prefix in recording_prefix


# ── ResponseContentDisposition ────────────────────────────────────────────────


def test_presigned_url_includes_content_disposition():
    """Presigned URL must include ResponseContentDisposition so cross-origin download works."""
    handler = _load_handler()

    mock_connect = MagicMock()
    mock_connect.describe_contact.return_value = {
        "Contact": {"InitiationTimestamp": _INITIATION_TS}
    }

    mock_s3 = MagicMock()
    mock_s3.list_objects_v2.return_value = {"Contents": [{"Key": f"{_VALID_UUID}.wav"}]}
    mock_s3.generate_presigned_url.return_value = "https://s3.example.com/url"

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_connect_client", mock_connect),
        patch.object(handler, "_s3_client", mock_s3),
    ):
        handler.get_artifacts({}, {"contactId": _VALID_UUID})

    first_call = mock_s3.generate_presigned_url.call_args_list[0]
    params = first_call.kwargs.get("Params") or first_call.args[1]
    assert "ResponseContentDisposition" in params
    assert "attachment" in params["ResponseContentDisposition"]


# ── S3 error resilience ───────────────────────────────────────────────────────


def test_s3_access_denied_returns_null_not_crash():
    """AccessDenied from S3 must be logged at error level and return null — not 500."""
    handler = _load_handler()

    mock_connect = MagicMock()
    mock_connect.describe_contact.return_value = {
        "Contact": {"InitiationTimestamp": _INITIATION_TS}
    }

    mock_s3 = MagicMock()
    mock_s3.list_objects_v2.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "Access Denied"}},
        "ListObjectsV2",
    )

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_connect_client", mock_connect),
        patch.object(handler, "_s3_client", mock_s3),
    ):
        result = handler.get_artifacts({}, {"contactId": _VALID_UUID})

    assert result["statusCode"] == 200
    assert result["body"]["voicemail"] is None


def test_s3_generic_error_returns_null_for_that_artifact():
    handler = _load_handler()

    mock_connect = MagicMock()
    mock_connect.describe_contact.return_value = {
        "Contact": {"InitiationTimestamp": _INITIATION_TS}
    }

    mock_s3 = MagicMock()
    mock_s3.list_objects_v2.side_effect = Exception("network timeout")

    with (
        patch.dict(os.environ, _ENV),
        patch.object(handler, "_connect_client", mock_connect),
        patch.object(handler, "_s3_client", mock_s3),
    ):
        result = handler.get_artifacts({}, {"contactId": _VALID_UUID})

    assert result["statusCode"] == 200
    assert result["body"]["voicemail"] is None
    assert result["body"]["recording"] is None
    assert result["body"]["transcript"] is None
