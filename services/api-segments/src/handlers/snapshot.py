"""Snapshot handlers — export segment members to S3.

Used by the UI to preview the first N matching profiles in the segment
builder. The S3 bucket is provisioned by the data stack and encrypted
with the project CMK.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

import boto3

from vip_shared.application.http import extract_caller, json_response, parse_body
from vip_shared.infrastructure.persistence.audit import build_from_env as build_audit
from vip_shared.infrastructure.persistence.customer_profiles_client import (
    build_from_env as build_cp,
)


def create_snapshot(event: dict, path_params: dict) -> dict:
    """POST /segments/{id}/snapshot — kick off an async export to S3."""
    name = path_params["id"]
    caller = extract_caller(event)
    body = parse_body(event) if event.get("body") else {}

    cp = build_cp()

    bucket = os.environ["SNAPSHOT_BUCKET"]
    role_arn = os.environ["SNAPSHOT_ROLE_ARN"]
    kms_arn = os.environ.get("DATA_KEY_ARN")

    # snapshot key includes segment name + timestamp prefix for organization
    from datetime import datetime, timezone

    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination_uri = f"s3://{bucket}/{name}/{ts}/"

    response = cp.create_segment_snapshot(
        name=name,
        destination_uri=destination_uri,
        data_format=body.get("dataFormat", "CSV"),
        role_arn=role_arn,
        encryption_key_arn=kms_arn,
    )
    snapshot_id = response["SnapshotId"]

    build_audit().record(
        entity_type="segment",
        entity_id=name,
        action="snapshot",
        actor_sub=caller.sub,
        actor_email=caller.email,
        ip_address=caller.ip_address,
        user_agent=caller.user_agent,
        extra={"snapshotId": snapshot_id, "destination": destination_uri},
    )

    return json_response(
        202,
        {
            "snapshotId": snapshot_id,
            "destinationUri": destination_uri,
            "status": response.get("Status", "IN_PROGRESS"),
        },
    )


def get_snapshot(event: dict, path_params: dict) -> dict:
    """GET /segments/{id}/snapshot/{snapshotId} — poll status."""
    name = path_params["id"]
    snapshot_id = path_params["snapshotId"]
    cp = build_cp()
    response = cp.get_segment_snapshot(snapshot_id=snapshot_id, name=name)

    status = response.get("Status")
    destination_uri = response.get("DestinationUri")

    payload: dict = {
        "snapshotId": snapshot_id,
        "status": status,
        "destinationUri": destination_uri,
        "dataFormat": response.get("DataFormat"),
        "statusMessage": response.get("StatusMessage"),
    }

    if status == "COMPLETED" and destination_uri:
        payload["downloadUrls"] = _presigned_urls(destination_uri)

    return json_response(200, payload)


def _presigned_urls(destination_uri: str, expiry: int = 3600) -> list[str]:
    parsed = urlparse(destination_uri)
    bucket = parsed.netloc
    prefix = parsed.path.lstrip("/")
    s3 = boto3.client("s3")
    urls: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["Key"].lower().endswith(".csv"):
                urls.append(
                    s3.generate_presigned_url(
                        "get_object",
                        Params={"Bucket": bucket, "Key": obj["Key"]},
                        ExpiresIn=expiry,
                    )
                )
    return urls
