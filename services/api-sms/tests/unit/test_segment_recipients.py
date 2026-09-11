"""AWS contract and complete-cohort tests; no network calls."""

from __future__ import annotations

import io
import uuid
from decimal import Decimal
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber

from segment_recipients import (
    SegmentRecipientsError,
    SegmentRecipientsPending,
    load_segment_recipients,
)

DOMAIN = "test-domain"
SEGMENT = "test-segment"
BUCKET = "test-encrypted-snapshots"
ROLE = "arn:aws:iam::123456789012:role/snapshot-role"
KEY = "arn:aws:kms:us-east-1:123456789012:key/" + str(uuid.UUID(int=9))
PHONE = "+12125551111"


def _pid(value=1):
    return str(uuid.UUID(int=value))


def _definition(field="PhoneNumber", values=None):
    value_dimension = {"DimensionType": "INCLUSIVE", "Values": values or [PHONE]}
    attrs = (
        {field: value_dimension}
        if field == "PhoneNumber"
        else {"Attributes": {field: value_dimension}}
    )
    return {
        "SegmentDefinitionName": SEGMENT,
        "SegmentDefinitionArn": f"arn:aws:profile:us-east-1:123456789012:domains/{DOMAIN}/segment-definitions/{SEGMENT}",
        "SegmentGroups": {
            "Include": "ALL",
            "Groups": [{"Type": "ANY", "Dimensions": [{"ProfileAttributes": attrs}]}],
        },
    }


@pytest.fixture
def aws():
    args = dict(
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    cp, s3 = boto3.client("customer-profiles", **args), boto3.client("s3", **args)
    with Stubber(cp) as cp_stub, Stubber(s3) as s3_stub:
        yield cp, cp_stub, s3, s3_stub
        cp_stub.assert_no_pending_responses()
        s3_stub.assert_no_pending_responses()


def _run(aws, metadata=None, publish=None):
    return load_segment_recipients(
        cp=aws[0],
        s3=aws[2],
        domain=DOMAIN,
        segment_name=SEGMENT,
        snapshot_bucket=BUCKET,
        snapshot_role_arn=ROLE,
        encryption_key_arn=KEY,
        load_snapshot=lambda: metadata,
        publish_snapshot=publish or (lambda candidate: candidate),
    )


def _get_definition(stub, definition=None):
    stub.add_response(
        "get_segment_definition",
        definition or _definition(),
        {
            "DomainName": DOMAIN,
            "SegmentDefinitionName": SEGMENT,
        },
    )


def _search(stub, ids, key="_phone", value=PHONE, token=None, next_token=None):
    params = {
        "DomainName": DOMAIN,
        "KeyName": key,
        "Values": [value],
        "MaxResults": 100,
    }
    if token:
        params["NextToken"] = token
    response = {"Items": [{"ProfileId": profile_id} for profile_id in ids]}
    if next_token:
        response["NextToken"] = next_token
    stub.add_response("search_profiles", response, params)


def _profile(profile_id, **extra):
    return {"ProfileId": profile_id, "PhoneNumber": PHONE, "FirstName": "Jane", **extra}


def _membership(stub, ids, absent=(), missing=(), failures=None):
    profiles = [
        {"ProfileId": profile_id, "QueryResult": "ABSENT"}
        if profile_id in absent
        else {
            "ProfileId": profile_id,
            "QueryResult": "PRESENT",
            "Profile": _profile(profile_id),
        }
        for profile_id in ids
        if profile_id not in missing
    ]
    response = {"SegmentDefinitionName": SEGMENT, "Profiles": profiles}
    if failures:
        response["Failures"] = failures
    stub.add_response(
        "get_segment_membership",
        response,
        {
            "DomainName": DOMAIN,
            "SegmentDefinitionName": SEGMENT,
            "ProfileIds": ids,
        },
    )


@pytest.mark.parametrize(
    "field,key",
    [("PhoneNumber", "_phone"), ("customerid", "customerid"), ("ID", "customerid")],
)
def test_canonical_search_filters_authoritative_membership(aws, field, key):
    stub = aws[1]
    _get_definition(stub, _definition(field))
    _search(stub, [_pid(1), _pid(2)], key=key)
    _membership(stub, [_pid(1), _pid(2)], absent=[_pid(2)])
    assert _run(aws) == [{"phone": PHONE, "FirstName": "Jane"}]


def test_search_pagination_deduplicates_and_membership_uses_max_100(aws):
    stub = aws[1]
    ids = [_pid(i) for i in range(1, 102)]
    _get_definition(stub)
    _search(stub, ids[:100], next_token="page-2")
    _search(stub, [ids[0], ids[100]], token="page-2")
    _membership(stub, ids[:100])
    _membership(stub, ids[100:])
    assert len(_run(aws)) == 101


@pytest.mark.parametrize("failures", [False, True])
def test_membership_missing_or_failed_member_never_returns_partial_list(aws, failures):
    stub = aws[1]
    _get_definition(stub)
    _search(stub, [_pid(1), _pid(2)])
    _membership(
        stub,
        [_pid(1), _pid(2)],
        missing=[_pid(2)],
        failures=[
            {
                "ProfileId": _pid(2),
                "Status": 500,
                "Message": "synthetic sensitive provider detail",
            }
        ]
        if failures
        else None,
    )
    with pytest.raises(SegmentRecipientsError, match="incomplete|omitted"):
        _run(aws)


def test_later_search_page_failure_is_not_an_empty_or_partial_success(aws):
    stub = aws[1]
    _get_definition(stub)
    _search(stub, [_pid()], next_token="page-2")
    stub.add_client_error(
        "search_profiles",
        service_error_code="InternalServerException",
        service_message="synthetic sensitive provider detail",
    )
    with pytest.raises(SegmentRecipientsError) as error:
        _run(aws)
    assert "sensitive" not in str(error.value)


def _metadata(value="a"):
    return {
        "snapshotId": uuid.UUID(int=9).hex,
        "segmentName": SEGMENT,
        "destinationUri": f"s3://{BUCKET}/precall-sms/{value * 32}/",
        "requestedAtEpoch": Decimal("1000"),
    }


def _snapshot(stub, metadata, status="COMPLETED", **overrides):
    stub.add_response(
        "get_segment_snapshot",
        {
            "SnapshotId": metadata["snapshotId"],
            "Status": status,
            "DestinationUri": metadata["destinationUri"],
            "DataFormat": "CSV",
            **overrides,
        },
        {
            "DomainName": DOMAIN,
            "SegmentDefinitionName": SEGMENT,
            "SnapshotId": metadata["snapshotId"],
        },
    )


def _csv(stub, metadata, body):
    prefix = metadata["destinationUri"].split(f"s3://{BUCKET}/", 1)[1]
    key = prefix + "part-1.csv"
    stub.add_response(
        "list_objects_v2",
        {"Contents": [{"Key": key}], "IsTruncated": False},
        {
            "Bucket": BUCKET,
            "Prefix": prefix,
        },
    )
    stub.add_response(
        "get_object",
        {"Body": StreamingBody(io.BytesIO(body), len(body))},
        {
            "Bucket": BUCKET,
            "Key": key,
        },
    )


def _batch(stub, ids, profiles=None, errors=None):
    response = {
        "Profiles": [_profile(profile_id) for profile_id in ids]
        if profiles is None
        else profiles
    }
    if errors:
        response["Errors"] = errors
    stub.add_response(
        "batch_get_profile", response, {"DomainName": DOMAIN, "ProfileIds": ids}
    )


@pytest.mark.parametrize(
    "unsupported", ["additional-dimension", "intersection", "large-list", "sql"]
)
def test_unsupported_definition_starts_encrypted_export_and_returns_pending(
    aws, unsupported
):
    stub = aws[1]
    definition = _definition()
    group = definition["SegmentGroups"]["Groups"][0]
    if unsupported == "additional-dimension":
        group["Dimensions"][0]["ProfileAttributes"]["FirstName"] = {
            "DimensionType": "INCLUSIVE",
            "Values": ["Jane"],
        }
    elif unsupported == "intersection":
        group["Type"] = "ALL"
        group["Dimensions"] *= 2
    elif unsupported == "large-list":
        definition = _definition(values=[f"+1212{i:07d}" for i in range(101)])
    else:
        definition["SegmentSqlQuery"] = "SELECT * FROM profile"
    _get_definition(stub, definition)
    metadata = _metadata()
    stub.add_response(
        "create_segment_snapshot",
        {"SnapshotId": metadata["snapshotId"]},
        {
            "DomainName": DOMAIN,
            "SegmentDefinitionName": SEGMENT,
            "DataFormat": "CSV",
            "DestinationUri": metadata["destinationUri"],
            "RoleArn": ROLE,
            "EncryptionKey": KEY,
        },
    )
    _snapshot(stub, metadata, "IN_PROGRESS")
    publish = MagicMock(side_effect=lambda candidate: candidate)
    with (
        patch("segment_recipients.uuid.uuid4", return_value=uuid.UUID(hex="a" * 32)),
        pytest.raises(SegmentRecipientsPending),
    ):
        _run(aws, publish=publish)
    assert publish.call_args.args[0]["segmentName"] == SEGMENT
    assert set(publish.call_args.args[0]) == set(metadata)


def test_concurrent_export_reads_only_published_winner_then_reuses_it(aws):
    stub, s3_stub = aws[1], aws[3]
    definition = _definition()
    definition["SegmentGroups"]["Include"] = "ANY"
    _get_definition(stub, definition)
    loser, winner = _metadata("a"), _metadata("b")
    winner["snapshotId"] = uuid.UUID(int=10).hex
    stub.add_response(
        "create_segment_snapshot",
        {"SnapshotId": loser["snapshotId"]},
        {
            "DomainName": DOMAIN,
            "SegmentDefinitionName": SEGMENT,
            "DataFormat": "CSV",
            "DestinationUri": loser["destinationUri"],
            "RoleArn": ROLE,
            "EncryptionKey": KEY,
        },
    )
    _snapshot(stub, winner, "IN_PROGRESS")
    with (
        patch("segment_recipients.uuid.uuid4", return_value=uuid.UUID(hex="a" * 32)),
        pytest.raises(SegmentRecipientsPending),
    ):
        _run(aws, publish=lambda candidate: winner)
    _snapshot(stub, winner)
    _csv(s3_stub, winner, f"pRoFiLeId\n{_pid()}\n".encode())
    _batch(stub, [_pid()])
    assert _run(aws, winner) == [{"phone": PHONE, "FirstName": "Jane"}]


def test_snapshot_profile_hydration_uses_real_20_profile_batch_limit(aws):
    stub, s3_stub, metadata = aws[1], aws[3], _metadata()
    ids = [_pid(i) for i in range(1, 22)]
    _snapshot(stub, metadata)
    _csv(s3_stub, metadata, ("PROFILEID\n" + "\n".join(ids)).encode())
    _batch(stub, ids[:20])
    _batch(stub, ids[20:])
    assert len(_run(aws, metadata)) == 21


@pytest.mark.parametrize("failed", [False, True])
def test_snapshot_profile_errors_or_missing_profile_cannot_return_partial_success(
    aws, failed
):
    stub, s3_stub, metadata = aws[1], aws[3], _metadata()
    _snapshot(stub, metadata)
    _csv(s3_stub, metadata, f"ProfileId\n{_pid(1)}\n{_pid(2)}\n".encode())
    _batch(
        stub,
        [_pid(1), _pid(2)],
        profiles=[_profile(_pid(1))],
        errors=[
            {
                "Code": "InternalFailure",
                "Message": "synthetic private detail",
                "ProfileId": _pid(2),
            }
        ]
        if failed
        else None,
    )
    with pytest.raises(SegmentRecipientsError):
        _run(aws, metadata)


@pytest.mark.parametrize(
    "body",
    [
        b"PhoneNumber\n+12125551111\n",
        b'ProfileId\n"unterminated',
        b"ProfileId,FirstName\nidentifier\n",
        b"ProfileId\nidentifier,extra\n",
        b"ProfileId,FirstName\n,Jane\n",
        b"ProfileId\n\xff\n",
        b"ProfileId,profileid\na,b\n",
    ],
)
def test_malformed_snapshot_csv_fails_without_any_profile_success(aws, body):
    metadata = _metadata()
    _snapshot(aws[1], metadata)
    _csv(aws[3], metadata, body)
    with pytest.raises(SegmentRecipientsError):
        _run(aws, metadata)


@pytest.mark.parametrize(
    "destination", [f"s3://other/precall-sms/{'a' * 32}/", f"s3://{BUCKET}/unrelated/"]
)
def test_snapshot_response_cannot_redirect_s3_reads(aws, destination):
    metadata = _metadata()
    _snapshot(aws[1], metadata, DestinationUri=destination)
    with pytest.raises(SegmentRecipientsError, match="destination"):
        _run(aws, metadata)


def test_completed_snapshot_accepts_aws_trailing_slash_normalization(aws):
    metadata = _metadata()
    _snapshot(aws[1], metadata, DestinationUri=metadata["destinationUri"].rstrip("/"))
    # The requested slash-terminated prefix is still the exact S3 list prefix.
    _csv(aws[3], metadata, f"ProfileId\n{_pid()}\n".encode())
    _batch(aws[1], [_pid()])
    assert _run(aws, metadata) == [{"phone": PHONE, "FirstName": "Jane"}]


def test_valid_empty_snapshot_and_profile_without_phone_are_distinct_from_read_failure(
    aws,
):
    metadata = _metadata()
    _snapshot(aws[1], metadata)
    _csv(aws[3], metadata, b"ProfileId\n")
    assert _run(aws, metadata) == []
    _snapshot(aws[1], metadata)
    _csv(aws[3], metadata, f"ProfileId\n{_pid()}\n".encode())
    _batch(aws[1], [_pid()], profiles=[{"ProfileId": _pid(), "FirstName": "Jane"}])
    assert _run(aws, metadata) == []


def test_pending_response_may_omit_destination_before_export_finishes(aws):
    metadata = _metadata()
    aws[1].add_response(
        "get_segment_snapshot",
        {
            "SnapshotId": metadata["snapshotId"],
            "Status": "IN_PROGRESS",
            "DataFormat": "CSV",
        },
        {
            "DomainName": DOMAIN,
            "SegmentDefinitionName": SEGMENT,
            "SnapshotId": metadata["snapshotId"],
        },
    )
    with pytest.raises(SegmentRecipientsPending):
        _run(aws, metadata)


@pytest.mark.parametrize("nested_id", [None, _pid(2)])
def test_membership_nested_identity_is_optional_but_cannot_disagree(aws, nested_id):
    stub = aws[1]
    _get_definition(stub)
    _search(stub, [_pid()])
    profile = {"PhoneNumber": PHONE, "FirstName": "Jane"}
    if nested_id is not None:
        profile["ProfileId"] = nested_id
    stub.add_response(
        "get_segment_membership",
        {
            "Profiles": [
                {
                    "ProfileId": _pid(),
                    "QueryResult": "PRESENT",
                    "Profile": profile,
                }
            ]
        },
        {
            "DomainName": DOMAIN,
            "SegmentDefinitionName": SEGMENT,
            "ProfileIds": [_pid()],
        },
    )
    if nested_id is None:
        assert _run(aws) == [{"phone": PHONE, "FirstName": "Jane"}]
    else:
        with pytest.raises(SegmentRecipientsError, match="identity"):
            _run(aws)


def test_terminal_run_callback_exception_is_preserved(aws):
    class RunInactive(RuntimeError):
        pass

    original = RunInactive("run stopped")
    definition = _definition()
    definition["SegmentGroups"]["Include"] = "ANY"
    _get_definition(aws[1], definition)
    metadata = _metadata()
    aws[1].add_response(
        "create_segment_snapshot",
        {"SnapshotId": metadata["snapshotId"]},
        {
            "DomainName": DOMAIN,
            "SegmentDefinitionName": SEGMENT,
            "DataFormat": "CSV",
            "DestinationUri": metadata["destinationUri"],
            "RoleArn": ROLE,
            "EncryptionKey": KEY,
        },
    )
    with (
        patch("segment_recipients.uuid.uuid4", return_value=uuid.UUID(hex="a" * 32)),
        pytest.raises(RunInactive) as error,
    ):
        _run(aws, publish=MagicMock(side_effect=original))
    assert error.value is original


def test_snapshot_s3_pagination_reads_every_part_and_deduplicates_ids(aws):
    metadata = _metadata()
    _snapshot(aws[1], metadata)
    prefix = f"precall-sms/{'a' * 32}/"
    for index in (1, 2):
        params = {"Bucket": BUCKET, "Prefix": prefix}
        response = {
            "Contents": [{"Key": f"{prefix}part-{index}.csv"}],
            "IsTruncated": index == 1,
        }
        if index == 1:
            response["NextContinuationToken"] = "next-part"
        else:
            params["ContinuationToken"] = "next-part"
        aws[3].add_response("list_objects_v2", response, params)
        body = f"ProfileId\n{_pid(1)}\n{_pid(index)}\n".encode()
        aws[3].add_response(
            "get_object",
            {"Body": StreamingBody(io.BytesIO(body), len(body))},
            {
                "Bucket": BUCKET,
                "Key": f"{prefix}part-{index}.csv",
            },
        )
    _batch(aws[1], [_pid(1), _pid(2)])
    assert len(_run(aws, metadata)) == 2
