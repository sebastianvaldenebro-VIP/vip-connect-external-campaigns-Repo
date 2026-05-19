"""Tests for SnapshotReader — the CSV parser for Customer Profiles snapshot exports."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from vip_shared.infrastructure.persistence.snapshot_reader import (
    SnapshotReader,
    _parse_s3_uri,
)


class TestParseS3Uri:
    def test_parses_bucket_and_prefix(self):
        assert _parse_s3_uri("s3://bucket/foo/bar/") == ("bucket", "foo/bar/")

    def test_rejects_non_s3_scheme(self):
        with pytest.raises(ValueError):
            _parse_s3_uri("https://bucket/foo")


class TestSnapshotReader:
    def test_load_members_concatenates_csv_parts(self):
        csv1 = b"ProfileId,customerid\nA,cust-1\nB,cust-2\n"
        csv2 = b"ProfileId,customerid\nC,cust-3\n"

        s3 = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {"Contents": [{"Key": "prefix/part-1.csv"}, {"Key": "prefix/part-2.csv"}, {"Key": "prefix/_SUCCESS"}]}
        ]
        s3.get_paginator.return_value = paginator
        s3.get_object.side_effect = [
            {"Body": _BodyStub(csv1)},
            {"Body": _BodyStub(csv2)},
        ]

        reader = SnapshotReader(s3_client=s3)
        rows = reader.load_members("s3://bucket/prefix/")
        # _SUCCESS was skipped (not .csv); three rows across two parts.
        assert len(rows) == 3
        assert rows[0]["customerid"] == "cust-1"
        assert rows[-1]["customerid"] == "cust-3"

    def test_extract_customer_ids_is_case_insensitive(self):
        rows = [
            {"ProfileId": "A", "CustomerID": "cust-1"},
            {"ProfileId": "B", "customerid": "cust-2"},
            {"ProfileId": "C"},  # no customerid → skipped
            {"ProfileId": "D", "customerid": ""},  # blank → skipped
        ]
        ids = SnapshotReader(s3_client=MagicMock()).extract_customer_ids(rows)
        assert ids == {"cust-1", "cust-2"}


class _BodyStub:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data
