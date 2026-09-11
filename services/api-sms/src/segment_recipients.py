"""Resolve complete segment cohorts using supported Customer Profiles APIs.

Small canonical ID/phone lists use search plus authoritative membership checks.
Other definitions use a persisted asynchronous export into the existing encrypted
snapshot bucket. No partial recipient list is returned after a failed read.
"""

from __future__ import annotations

import csv
import io
import re
import time
import uuid
from collections.abc import Callable
from decimal import Decimal
from urllib.parse import urlparse

from vip_shared.infrastructure.persistence.snapshot_reader import SnapshotReader


class SegmentRecipientsPending(RuntimeError):
    """The cohort export is still running; retry with the persisted metadata."""


class SegmentRecipientsError(RuntimeError):
    """The cohort could not be read completely; do not send a partial list."""


class _SnapshotCallbackError(Exception):
    def __init__(self, original: Exception) -> None:
        self.original = original


_MAX_FAST_PATH_VALUES = 100
_MEMBERSHIP_BATCH_SIZE = 100
_PROFILE_BATCH_SIZE = 20  # BatchGetProfile's actual SDK/API maximum.


def load_segment_recipients(
    *,
    cp,
    s3,
    domain: str,
    segment_name: str,
    snapshot_bucket: str,
    snapshot_role_arn: str,
    encryption_key_arn: str,
    load_snapshot: Callable[[], dict | None],
    publish_snapshot: Callable[[dict], dict],
) -> list[dict]:
    """Return only phone/FirstName, or raise Pending/Error without partial data.

    ``publish_snapshot`` must conditionally publish only if metadata is absent,
    then return the persisted winner using a consistent read. Metadata contains
    snapshotId, destinationUri, segmentName and requestedAtEpoch; no profiles.
    Concurrent losing exports have unique prefixes and are never read here.
    """
    metadata = load_snapshot()  # Preserve caller's terminal-run exception.
    try:
        if metadata is None:
            definition = cp.get_segment_definition(
                DomainName=domain,
                SegmentDefinitionName=segment_name,
            )
            if definition.get("SegmentDefinitionName") != segment_name:
                raise SegmentRecipientsError("Unexpected segment definition response")
            candidates = _canonical_candidates(definition)
            if candidates is not None:
                return _load_finite_members(cp, domain, segment_name, candidates)

            if not snapshot_bucket or not snapshot_role_arn or not encryption_key_arn:
                raise SegmentRecipientsError(
                    "Encrypted segment snapshot is not configured"
                )
            destination = f"s3://{snapshot_bucket}/precall-sms/{uuid.uuid4().hex}/"
            response = cp.create_segment_snapshot(
                DomainName=domain,
                SegmentDefinitionName=segment_name,
                DataFormat="CSV",
                DestinationUri=destination,
                RoleArn=snapshot_role_arn,
                EncryptionKey=encryption_key_arn,
            )
            if not response.get("SnapshotId"):
                raise SegmentRecipientsError("Snapshot creation returned no identifier")
            candidate = {
                "snapshotId": response["SnapshotId"],
                "destinationUri": destination,
                "segmentName": segment_name,
                "requestedAtEpoch": int(time.time()),
            }
            try:
                metadata = publish_snapshot(candidate)
            except Exception as error:
                raise _SnapshotCallbackError(error) from None

        destination = _validate_metadata(metadata, snapshot_bucket, segment_name)
        response = cp.get_segment_snapshot(
            DomainName=domain,
            SegmentDefinitionName=segment_name,
            SnapshotId=metadata["snapshotId"],
        )
        if response.get("SnapshotId") != metadata["snapshotId"]:
            raise SegmentRecipientsError("Unexpected snapshot response")
        if (
            response.get("DestinationUri") is not None
            and not _same_destination(response["DestinationUri"], destination)
        ):
            raise SegmentRecipientsError(
                "Snapshot destination does not match its request"
            )
        if response.get("Status") == "IN_PROGRESS":
            raise SegmentRecipientsPending("Segment snapshot is still running")
        if response.get("Status") != "COMPLETED" or response.get("DataFormat") != "CSV":
            raise SegmentRecipientsError("Segment snapshot did not complete as CSV")
        if not _same_destination(response.get("DestinationUri"), destination):
            raise SegmentRecipientsError(
                "Completed snapshot has no matching destination"
            )

        ids = _StrictSnapshotReader(s3_client=s3).load_profile_ids(destination)
        return _load_snapshot_profiles(cp, domain, ids)
    except _SnapshotCallbackError as error:
        raise error.original from None
    except (SegmentRecipientsPending, SegmentRecipientsError):
        raise
    except Exception:
        # AWS exceptions and parser failures can embed profile/search data.
        raise SegmentRecipientsError(
            "Segment recipients could not be read completely"
        ) from None


def _canonical_candidates(definition: dict) -> list[tuple[str, str]] | None:
    if (
        definition.get("SegmentSqlQuery")
        or definition.get("SegmentSort")
        or definition.get("SegmentType") == "ENHANCED"
    ):
        return None
    groups = definition.get("SegmentGroups")
    if not isinstance(groups, dict) or set(groups) != {"Include", "Groups"}:
        return None
    if groups["Include"] != "ALL" or len(groups["Groups"]) != 1:
        return None
    group = groups["Groups"][0]
    if set(group) != {"Type", "Dimensions"}:
        return None
    dimensions = group["Dimensions"]
    if not dimensions or not (
        group["Type"] == "ANY" or (group["Type"] == "ALL" and len(dimensions) == 1)
    ):
        return None
    candidates: list[tuple[str, str]] = []
    for dimension in dimensions:
        if set(dimension) != {"ProfileAttributes"}:
            return None
        attributes = dimension["ProfileAttributes"]
        if set(attributes) == {"PhoneNumber"}:
            key, value_dimension = "_phone", attributes["PhoneNumber"]
        elif set(attributes) == {"Attributes"} and len(attributes["Attributes"]) == 1:
            field, value_dimension = next(iter(attributes["Attributes"].items()))
            if field not in {"customerid", "ID"}:
                return None
            key = "customerid"  # Both canonical ID attributes use this domain index.
        else:
            return None
        if set(value_dimension) != {"DimensionType", "Values"}:
            return None
        values = value_dimension["Values"]
        if value_dimension["DimensionType"] != "INCLUSIVE" or not isinstance(
            values, list
        ):
            return None
        if not values or any(
            not isinstance(value, str) or not value for value in values
        ):
            return None
        candidates.extend((key, value) for value in values)
    candidates = list(dict.fromkeys(candidates))
    return candidates if len(candidates) <= _MAX_FAST_PATH_VALUES else None


def _load_finite_members(
    cp, domain: str, segment_name: str, candidates: list
) -> list[dict]:
    ids: dict[str, None] = {}
    for key, value in candidates:
        kwargs = {
            "DomainName": domain,
            "KeyName": key,
            "Values": [value],
            "MaxResults": 100,
        }
        seen_tokens: set[str] = set()
        while True:
            response = cp.search_profiles(**kwargs)
            items = response.get("Items")
            if not isinstance(items, list):
                raise SegmentRecipientsError(
                    "Profile search returned an incomplete response"
                )
            for profile in items:
                profile_id = profile.get("ProfileId")
                if not profile_id:
                    raise SegmentRecipientsError(
                        "Profile search returned an unidentified profile"
                    )
                ids[profile_id] = None
            token = response.get("NextToken")
            if not token:
                break
            if token in seen_tokens:
                raise SegmentRecipientsError(
                    "Profile search pagination did not advance"
                )
            seen_tokens.add(token)
            kwargs["NextToken"] = token

    recipients: list[dict] = []
    profile_ids = list(ids)
    for offset in range(0, len(profile_ids), _MEMBERSHIP_BATCH_SIZE):
        batch = profile_ids[offset : offset + _MEMBERSHIP_BATCH_SIZE]
        response = cp.get_segment_membership(
            DomainName=domain,
            SegmentDefinitionName=segment_name,
            ProfileIds=batch,
        )
        if response.get("Failures") or not isinstance(response.get("Profiles"), list):
            raise SegmentRecipientsError(
                "Segment membership returned an incomplete response"
            )
        seen: set[str] = set()
        for item in response["Profiles"]:
            profile_id = item.get("ProfileId")
            if profile_id not in batch or profile_id in seen:
                raise SegmentRecipientsError(
                    "Segment membership returned unexpected profiles"
                )
            seen.add(profile_id)
            if item.get("QueryResult") == "ABSENT":
                continue
            if item.get("QueryResult") != "PRESENT" or not isinstance(
                item.get("Profile"), dict
            ):
                raise SegmentRecipientsError(
                    "Segment membership has no definitive result"
                )
            profile = item["Profile"]
            if (
                profile.get("ProfileId") is not None
                and profile["ProfileId"] != profile_id
            ):
                raise SegmentRecipientsError(
                    "Segment membership profile identity does not match"
                )
            recipient = _recipient(profile)
            if recipient is not None:
                recipients.append(recipient)
        if seen != set(batch):
            raise SegmentRecipientsError(
                "Segment membership omitted requested profiles"
            )
    return recipients


def _validate_metadata(metadata: dict, bucket: str, segment_name: str) -> str:
    if not isinstance(metadata, dict) or metadata.get("segmentName") != segment_name:
        raise SegmentRecipientsError("Snapshot metadata does not match this segment")
    if not metadata.get("snapshotId") or not isinstance(
        metadata.get("requestedAtEpoch"), (int, float, Decimal)
    ):
        raise SegmentRecipientsError("Snapshot metadata is incomplete")
    destination = metadata.get("destinationUri", "")
    parsed = urlparse(destination)
    if (
        parsed.scheme != "s3"
        or parsed.netloc != bucket
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"/precall-sms/[0-9a-f]{32}/", parsed.path)
    ):
        raise SegmentRecipientsError("Snapshot metadata has an invalid destination")
    return destination


def _same_destination(actual: object, requested: str) -> bool:
    # CP strips the request's trailing slash in completed snapshot metadata.
    # Ignore only that normalization; keep the persisted prefix for S3 reads.
    return isinstance(actual, str) and actual.rstrip("/") == requested.rstrip("/")


class _StrictSnapshotReader(SnapshotReader):
    def load_profile_ids(self, destination: str) -> list[str]:
        parsed = urlparse(destination)
        ids: dict[str, None] = {}
        csv_files = 0
        for key in self._iter_object_keys(parsed.netloc, parsed.path.lstrip("/")):
            if not key.lower().endswith(".csv"):
                continue
            if not key.startswith(parsed.path.lstrip("/")):
                raise SegmentRecipientsError(
                    "Snapshot listing escaped its requested prefix"
                )
            csv_files += 1
            body = self._s3.get_object(Bucket=parsed.netloc, Key=key)["Body"]
            try:
                text = body.read().decode("utf-8-sig")
            finally:
                body.close()
            reader = csv.DictReader(io.StringIO(text), strict=True)
            headers = reader.fieldnames or []
            normalized = [header.lower() for header in headers]
            if "profileid" not in normalized or len(set(normalized)) != len(headers):
                raise SegmentRecipientsError(
                    "Snapshot CSV has missing or ambiguous profile identifiers"
                )
            profile_field = headers[normalized.index("profileid")]
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise SegmentRecipientsError("Snapshot CSV has an incomplete row")
                profile_id = row.get(profile_field, "").strip()
                if not profile_id:
                    raise SegmentRecipientsError(
                        "Snapshot CSV contains an unidentified profile"
                    )
                ids[profile_id] = None
        if not csv_files:
            raise SegmentRecipientsError("Completed snapshot has no CSV objects")
        return list(ids)


def _load_snapshot_profiles(cp, domain: str, ids: list[str]) -> list[dict]:
    recipients: list[dict] = []
    for offset in range(0, len(ids), _PROFILE_BATCH_SIZE):
        batch = ids[offset : offset + _PROFILE_BATCH_SIZE]
        response = cp.batch_get_profile(DomainName=domain, ProfileIds=batch)
        if response.get("Errors") or not isinstance(response.get("Profiles"), list):
            raise SegmentRecipientsError(
                "Snapshot profiles could not be read completely"
            )
        seen: set[str] = set()
        for profile in response["Profiles"]:
            profile_id = profile.get("ProfileId")
            if profile_id not in batch or profile_id in seen:
                raise SegmentRecipientsError(
                    "Snapshot profiles returned unexpected identifiers"
                )
            seen.add(profile_id)
            recipient = _recipient(profile)
            if recipient is not None:
                recipients.append(recipient)
        if seen != set(batch):
            raise SegmentRecipientsError(
                "Snapshot profiles omitted requested identifiers"
            )
    return recipients


def _recipient(profile: dict) -> dict | None:
    phone = profile.get("PhoneNumber") or profile.get("MobilePhoneNumber") or ""
    if not isinstance(phone, str):
        raise SegmentRecipientsError("Profile has an invalid phone field")
    if not phone:
        return None  # A complete profile without a phone is not SMS-addressable.
    return {"phone": phone, "FirstName": profile.get("FirstName") or ""}
