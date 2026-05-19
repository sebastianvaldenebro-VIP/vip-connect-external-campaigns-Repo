"""Read the CSV members out of a completed Customer Profiles segment snapshot.

``CreateSegmentSnapshot`` writes one or more CSV files under the destination
S3 prefix; each row is a profile in the segment. We only need two columns —
``ProfileId`` and ``customerid`` — to diff against Redis.
"""
from __future__ import annotations

import csv
import io
from typing import Any
from urllib.parse import urlparse

import boto3


class SnapshotReader:
    def __init__(self, s3_client: Any | None = None) -> None:
        self._s3 = s3_client or boto3.client("s3")

    def load_members(self, destination_uri: str) -> list[dict[str, str]]:
        """Return one dict per member row under the snapshot's S3 prefix."""
        bucket, prefix = _parse_s3_uri(destination_uri)
        members: list[dict[str, str]] = []
        for key in self._iter_object_keys(bucket, prefix):
            if not key.lower().endswith(".csv"):
                continue
            body = self._s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            text = body.decode("utf-8", errors="replace")
            reader = csv.DictReader(io.StringIO(text))
            for row in reader:
                members.append(row)
        return members

    def extract_customer_ids(
        self, rows: list[dict[str, str]], *, field: str = "customerid"
    ) -> set[str]:
        """Pull a set of customerIds out of snapshot rows.

        The snapshot exports standard profile fields plus any custom
        attributes configured on the object type. We look for a
        case-insensitive match so we survive AWS casing quirks.
        """
        lowered = field.lower()
        ids: set[str] = set()
        for row in rows:
            for key, value in row.items():
                if key.lower() == lowered and value:
                    ids.add(str(value).strip())
                    break
        return ids

    def _iter_object_keys(self, bucket: str, prefix: str):
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                yield obj["Key"]


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3":
        raise ValueError(f"Not an S3 URI: {uri}")
    bucket = parsed.netloc
    prefix = parsed.path.lstrip("/")
    return bucket, prefix
