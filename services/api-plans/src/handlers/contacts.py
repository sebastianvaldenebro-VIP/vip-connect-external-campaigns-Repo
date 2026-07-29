"""Handler for GET /contacts/{contactId}/artifacts.

Returns presigned S3 URLs (15 min TTL) for voicemail, recording, and chat transcript.
URLs are generated server-side so the browser can download directly from S3 — avoids
API Gateway's 6 MB response-body limit for binary files.

PHI note: contactId is a system-generated UUID — not a HIPAA Safe Harbor identifier.
Full phone numbers are never present here. Every access is written to the HIPAA audit
log (6-year retention) per 45 CFR §164.312(b).
"""

from __future__ import annotations

import logging
import os
import re
from datetime import timezone

import boto3
from botocore.exceptions import ClientError

from vip_shared.application.http import error_response, extract_caller, json_response
from vip_shared.infrastructure.persistence.audit import build_from_env as build_audit

_LOG = logging.getLogger(__name__)
_LOG.setLevel(logging.INFO)

_CONNECT_INSTANCE_ID = os.environ["CONNECT_INSTANCE_ID"]
# Use .get() so a code-only deploy before CDK doesn't crash every route in this Lambda.
_RECORDINGS_BUCKET = os.environ.get("RECORDINGS_BUCKET", "")
_VOICEMAIL_BUCKET = os.environ.get("VOICEMAIL_BUCKET", "")
_PRESIGNED_TTL = 900  # 15 minutes

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_connect_client = None
_s3_client = None


def _get_connect():
    global _connect_client
    if _connect_client is None:
        _connect_client = boto3.client("connect")
    return _connect_client


def _get_s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def get_artifacts(event: dict, path_params: dict) -> dict:
    if not _RECORDINGS_BUCKET or not _VOICEMAIL_BUCKET:
        _LOG.error(
            "contacts_handler_misconfigured | RECORDINGS_BUCKET or VOICEMAIL_BUCKET not set"
        )
        return error_response(500, "MISCONFIGURED", "Service misconfigured")

    contact_id = path_params.get("contactId", "")

    if not _UUID_RE.match(contact_id):
        return error_response(400, "VALIDATION_ERROR", "Invalid contactId format")

    caller = extract_caller(event)

    try:
        resp = _get_connect().describe_contact(
            InstanceId=_CONNECT_INSTANCE_ID,
            ContactId=contact_id,
        )
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code == "ResourceNotFoundException":
            return error_response(404, "ResourceNotFoundException", "Contact not found")
        _LOG.error(
            "describe_contact failed | contactId=%s code=%s",
            contact_id,
            code,
        )
        return error_response(400, "ResolutionFailed", "Could not resolve contact")

    initiation_ts = resp["Contact"]["InitiationTimestamp"]
    utc_ts = initiation_ts.astimezone(timezone.utc)
    date_prefix = utc_ts.strftime("%Y/%m/%d")

    s3 = _get_s3()

    voicemail_url = _presign_first_match(
        s3, _VOICEMAIL_BUCKET, f"{contact_id}.wav", "voicemail.wav"
    )
    recording_url = _presign_first_match(
        s3,
        _RECORDINGS_BUCKET,
        f"connect/vipmedicalgroup/CallRecordings/{date_prefix}/{contact_id}",
        "recording.wav",
    )
    transcript_url = _presign_first_match(
        s3,
        _RECORDINGS_BUCKET,
        f"connect/vipmedicalgroup/ChatTranscripts/{date_prefix}/{contact_id}",
        "transcript.json",
    )

    artifacts_found = [
        name
        for name, url in (
            ("voicemail", voicemail_url),
            ("recording", recording_url),
            ("transcript", transcript_url),
        )
        if url
    ]

    _LOG.info(
        "contact_artifacts_access | caller_email=%s contactId=%s date=%s found=%s",
        caller.email,
        contact_id,
        date_prefix,
        artifacts_found,
    )

    build_audit().record(
        entity_type="contact_artifacts",
        entity_id=contact_id,
        action="READ",
        actor_sub=caller.sub,
        actor_email=caller.email,
        ip_address=caller.ip_address,
        user_agent=caller.user_agent,
        extra={"artifacts_found": artifacts_found, "date_prefix": date_prefix},
    )

    return json_response(
        200,
        {
            "contactId": contact_id,
            "voicemail": voicemail_url,
            "recording": recording_url,
            "transcript": transcript_url,
            "expiresInSeconds": _PRESIGNED_TTL,
        },
    )


def _presign_first_match(
    s3_client, bucket: str, prefix: str, filename: str
) -> str | None:
    """List up to 1 object matching prefix. If found, return a presigned GET URL with
    Content-Disposition set so browsers download instead of navigating to the file."""
    try:
        resp = s3_client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code in ("AccessDenied", "403"):
            _LOG.error(
                "s3_access_denied | bucket=%s prefix=%s — verify IAM/KMS grants",
                bucket,
                prefix,
            )
        else:
            _LOG.warning(
                "s3_list_failed | bucket=%s prefix=%s code=%s", bucket, prefix, code
            )
        return None
    except Exception as exc:
        _LOG.warning(
            "s3_list_failed | bucket=%s prefix=%s error=%s",
            bucket,
            prefix,
            type(exc).__name__,
        )
        return None

    contents = resp.get("Contents", [])
    if not contents:
        return None

    key = contents[0]["Key"]
    try:
        return s3_client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": bucket,
                "Key": key,
                # ResponseContentDisposition makes cross-origin <a download> work —
                # the `download` attribute is ignored for cross-origin URLs without this.
                "ResponseContentDisposition": f'attachment; filename="{filename}"',
            },
            ExpiresIn=_PRESIGNED_TTL,
        )
    except Exception as exc:
        _LOG.warning(
            "presign_failed | bucket=%s key=%s error=%s",
            bucket,
            key,
            type(exc).__name__,
        )
        return None
