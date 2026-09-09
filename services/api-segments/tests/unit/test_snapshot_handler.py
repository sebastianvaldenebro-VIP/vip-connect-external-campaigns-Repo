"""Tests for POST /segments/{id}/snapshot and GET /segments/{id}/snapshot/{snapshotId}."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("PROFILES_DOMAIN_NAME", "amazon-connect-vipmedicalgroup")
    monkeypatch.setenv("SNAPSHOT_BUCKET", "vip-segment-snapshots")
    monkeypatch.setenv("SNAPSHOT_ROLE_ARN", "arn:aws:iam::123:role/snapshot-role")
    monkeypatch.setenv("DATA_KEY_ARN", "arn:aws:kms:us-east-1:123:key/abc")
    monkeypatch.setenv("AUDIT_TABLE", "AdminAuditLog")


def _event(body=None):
    e = {
        "requestContext": {
            "authorizer": {"jwt": {"claims": {"sub": "u1", "email": "u@example.com"}}},
            "http": {"sourceIp": "10.0.0.1", "userAgent": "test-agent"},
        }
    }
    if body is not None:
        e["body"] = json.dumps(body)
    return e


class TestCreateSnapshot:
    def test_creates_snapshot_and_records_audit(self):
        from handlers import snapshot

        cp = MagicMock()
        cp.create_segment_snapshot.return_value = {
            "SnapshotId": "snap-1",
            "Status": "IN_PROGRESS",
        }
        audit = MagicMock()

        with (
            patch("handlers.snapshot.build_cp", return_value=cp),
            patch("handlers.snapshot.build_audit", return_value=audit),
        ):
            response = snapshot.create_snapshot(_event(), {"id": "nj-v1"})

        body = json.loads(response["body"])
        assert response["statusCode"] == 202
        assert body["snapshotId"] == "snap-1"
        assert body["status"] == "IN_PROGRESS"
        assert body["destinationUri"].startswith("s3://vip-segment-snapshots/nj-v1/")

        audit.record.assert_called_once()
        audit_kwargs = audit.record.call_args.kwargs
        assert audit_kwargs["entity_id"] == "nj-v1"
        assert audit_kwargs["action"] == "snapshot"
        assert audit_kwargs["extra"]["snapshotId"] == "snap-1"

    def test_defaults_data_format_to_csv_when_body_absent(self):
        from handlers import snapshot

        cp = MagicMock()
        cp.create_segment_snapshot.return_value = {"SnapshotId": "snap-2"}

        with (
            patch("handlers.snapshot.build_cp", return_value=cp),
            patch("handlers.snapshot.build_audit", return_value=MagicMock()),
        ):
            snapshot.create_snapshot(_event(), {"id": "nj-v1"})

        call_kwargs = cp.create_segment_snapshot.call_args.kwargs
        assert call_kwargs["data_format"] == "CSV"

    def test_respects_data_format_from_body(self):
        from handlers import snapshot

        cp = MagicMock()
        cp.create_segment_snapshot.return_value = {"SnapshotId": "snap-3"}

        with (
            patch("handlers.snapshot.build_cp", return_value=cp),
            patch("handlers.snapshot.build_audit", return_value=MagicMock()),
        ):
            snapshot.create_snapshot(
                _event(body={"dataFormat": "JSONL"}), {"id": "nj-v1"}
            )

        call_kwargs = cp.create_segment_snapshot.call_args.kwargs
        assert call_kwargs["data_format"] == "JSONL"


class TestGetSnapshot:
    def test_returns_status_without_download_urls_when_in_progress(self):
        from handlers import snapshot

        cp = MagicMock()
        cp.get_segment_snapshot.return_value = {
            "Status": "IN_PROGRESS",
            "DestinationUri": "s3://bucket/prefix/",
        }

        with patch("handlers.snapshot.build_cp", return_value=cp):
            response = snapshot.get_snapshot({}, {"id": "nj-v1", "snapshotId": "snap-1"})

        body = json.loads(response["body"])
        assert body["status"] == "IN_PROGRESS"
        assert "downloadUrls" not in body

    def test_returns_presigned_urls_when_completed(self):
        from handlers import snapshot

        cp = MagicMock()
        cp.get_segment_snapshot.return_value = {
            "Status": "COMPLETED",
            "DestinationUri": "s3://vip-segment-snapshots/nj-v1/20260827T150000Z/",
            "DataFormat": "CSV",
        }

        mock_s3 = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {
                "Contents": [
                    {"Key": "nj-v1/20260827T150000Z/part-0.csv"},
                    {"Key": "nj-v1/20260827T150000Z/part-0.csv.crc"},
                ]
            }
        ]
        mock_s3.get_paginator.return_value = paginator
        mock_s3.generate_presigned_url.return_value = "https://signed-url/part-0.csv"

        with (
            patch("handlers.snapshot.build_cp", return_value=cp),
            patch("handlers.snapshot.boto3.client", return_value=mock_s3),
        ):
            response = snapshot.get_snapshot({}, {"id": "nj-v1", "snapshotId": "snap-1"})

        body = json.loads(response["body"])
        assert body["downloadUrls"] == ["https://signed-url/part-0.csv"]

    def test_skips_download_urls_when_completed_but_no_destination_uri(self):
        from handlers import snapshot

        cp = MagicMock()
        cp.get_segment_snapshot.return_value = {"Status": "COMPLETED"}

        with patch("handlers.snapshot.build_cp", return_value=cp):
            response = snapshot.get_snapshot({}, {"id": "nj-v1", "snapshotId": "snap-1"})

        body = json.loads(response["body"])
        assert "downloadUrls" not in body


class TestPresignedUrls:
    def test_filters_to_csv_files_only(self):
        from handlers import snapshot

        mock_s3 = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {
                "Contents": [
                    {"Key": "prefix/part-0.CSV"},
                    {"Key": "prefix/_SUCCESS"},
                    {"Key": "prefix/part-1.csv"},
                ]
            }
        ]
        mock_s3.get_paginator.return_value = paginator
        mock_s3.generate_presigned_url.return_value = "https://signed"

        with patch("handlers.snapshot.boto3.client", return_value=mock_s3):
            urls = snapshot._presigned_urls("s3://bucket/prefix/")

        assert urls == ["https://signed", "https://signed"]

    def test_returns_empty_list_when_no_objects(self):
        from handlers import snapshot

        mock_s3 = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [{}]  # no "Contents" key
        mock_s3.get_paginator.return_value = paginator

        with patch("handlers.snapshot.boto3.client", return_value=mock_s3):
            urls = snapshot._presigned_urls("s3://bucket/prefix/")

        assert urls == []
