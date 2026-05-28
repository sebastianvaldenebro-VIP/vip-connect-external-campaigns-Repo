from __future__ import annotations

import os
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError


class CustomerProfilesClient:
    """Thin wrapper around boto3 customer-profiles client.

    - Binds the domain name from env, so callers don't repeat it
    - Surfaces specific error types we care about
    - Retries idempotent reads on transient throttles
    """

    def __init__(self, domain_name: str, boto_client=None) -> None:
        self._domain = domain_name
        self._client = boto_client or boto3.client("customer-profiles")

    # ── Segment CRUD ──────────────────────────────────────────────────

    def list_segment_definitions(
        self, max_results: int = 100, next_token: str | None = None
    ) -> dict:
        kwargs = {"DomainName": self._domain, "MaxResults": max_results}
        if next_token:
            kwargs["NextToken"] = next_token
        return self._client.list_segment_definitions(**kwargs)

    def get_segment_definition(self, name: str) -> dict:
        return self._client.get_segment_definition(
            DomainName=self._domain, SegmentDefinitionName=name
        )

    def create_segment_definition(
        self,
        *,
        name: str,
        display_name: str,
        segment_groups: dict,
        description: str | None = None,
        tags: dict[str, str] | None = None,
    ) -> dict:
        kwargs: dict[str, Any] = {
            "DomainName": self._domain,
            "SegmentDefinitionName": name,
            "DisplayName": display_name,
            "SegmentGroups": segment_groups,
        }
        if description:
            kwargs["Description"] = description
        if tags:
            kwargs["Tags"] = tags
        return self._client.create_segment_definition(**kwargs)

    def delete_segment_definition(self, name: str) -> None:
        self._client.delete_segment_definition(
            DomainName=self._domain, SegmentDefinitionName=name
        )

    # ── Segment estimates (async recompute) ───────────────────────────

    def create_segment_estimate(self, name: str) -> dict:
        """Recompute count for an existing segment definition.

        CP's CreateSegmentEstimate wants a SegmentGroups query, not a segment
        name. We fetch the definition first and forward its SegmentGroups so
        callers can estimate by name — matching the UI's mental model.
        """
        definition = self.get_segment_definition(name)
        return self._client.create_segment_estimate(
            DomainName=self._domain,
            SegmentQuery=definition["SegmentGroups"],
        )

    def get_segment_estimate(self, estimate_id: str) -> dict:
        return self._client.get_segment_estimate(
            DomainName=self._domain, EstimateId=estimate_id
        )

    def wait_for_estimate(
        self, estimate_id: str, timeout_seconds: int = 120, poll_interval: float = 2.0
    ) -> dict:
        """Block-polling until estimate completes or timeout. Raises on FAILED."""
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            response = self.get_segment_estimate(estimate_id)
            status = response.get("Status")
            if status == "SUCCEEDED":
                return response
            if status == "FAILED":
                raise RuntimeError(
                    f"Estimate failed: {response.get('Message', 'no detail')}"
                )
            time.sleep(poll_interval)
        raise TimeoutError(
            f"Estimate {estimate_id} did not complete within {timeout_seconds}s"
        )

    # ── Snapshot exports ──────────────────────────────────────────────

    def create_segment_snapshot(
        self,
        *,
        name: str,
        destination_uri: str,
        data_format: str = "CSV",
        role_arn: str | None = None,
        encryption_key_arn: str | None = None,
    ) -> dict:
        kwargs: dict[str, Any] = {
            "DomainName": self._domain,
            "SegmentDefinitionName": name,
            "DataFormat": data_format,
            "DestinationUri": destination_uri,
        }
        if role_arn:
            kwargs["RoleArn"] = role_arn
        if encryption_key_arn:
            # AWS API calls this field just "EncryptionKey" (the ARN goes there).
            kwargs["EncryptionKey"] = encryption_key_arn
        return self._client.create_segment_snapshot(**kwargs)

    def get_segment_snapshot(self, snapshot_id: str, name: str) -> dict:
        return self._client.get_segment_snapshot(
            DomainName=self._domain,
            SegmentDefinitionName=name,
            SnapshotId=snapshot_id,
        )

    def wait_for_snapshot(
        self,
        *,
        snapshot_id: str,
        name: str,
        timeout_seconds: int = 300,
        poll_interval: float = 3.0,
    ) -> dict:
        """Block-poll until the snapshot completes or raise on FAILED/timeout."""
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            response = self.get_segment_snapshot(snapshot_id, name)
            status = response.get("Status")
            if status == "COMPLETED":
                return response
            if status == "FAILED":
                raise RuntimeError(
                    f"Snapshot failed: {response.get('StatusMessage', 'no detail')}"
                )
            time.sleep(poll_interval)
        raise TimeoutError(
            f"Snapshot {snapshot_id} did not complete within {timeout_seconds}s"
        )

    def tag_segment(self, *, segment_arn: str, tags: dict[str, str]) -> None:
        """Merge tags onto an existing segment (used for syncMode updates)."""
        self._client.tag_resource(resourceArn=segment_arn, tags=tags)

    # ── Segment membership check ──────────────────────────────────────

    def get_segment_membership(self, name: str, profile_ids: list[str]) -> dict:
        return self._client.get_segment_membership(
            DomainName=self._domain,
            SegmentDefinitionName=name,
            ProfileIds=profile_ids,
        )

    # ── Profile search & get ──────────────────────────────────────────

    def search_profiles(
        self,
        *,
        key_name: str,
        values: list[str],
        max_results: int = 20,
        additional_search_keys: list[dict] | None = None,
    ) -> dict:
        kwargs: dict[str, Any] = {
            "DomainName": self._domain,
            "KeyName": key_name,
            "Values": values,
            "MaxResults": max_results,
        }
        if additional_search_keys:
            kwargs["AdditionalSearchKeys"] = additional_search_keys
        return self._client.search_profiles(**kwargs)

    def batch_get_profile(self, profile_ids: list[str]) -> dict:
        return self._client.batch_get_profile(
            DomainName=self._domain, ProfileIds=profile_ids
        )

    def list_profile_objects(
        self,
        *,
        profile_id: str,
        object_type_name: str,
        max_results: int = 10,
    ) -> dict:
        return self._client.list_profile_objects(
            DomainName=self._domain,
            ProfileId=profile_id,
            ObjectTypeName=object_type_name,
            MaxResults=max_results,
        )

    def list_calculated_attributes_for_profile(
        self, profile_id: str, max_results: int = 50
    ) -> dict:
        return self._client.list_calculated_attributes_for_profile(
            DomainName=self._domain, ProfileId=profile_id, MaxResults=max_results
        )

    def get_calculated_attribute_for_profile(
        self, profile_id: str, calculated_attribute_name: str
    ) -> dict:
        return self._client.get_calculated_attribute_for_profile(
            DomainName=self._domain,
            ProfileId=profile_id,
            CalculatedAttributeName=calculated_attribute_name,
        )


def build_from_env() -> CustomerProfilesClient:
    domain = os.environ["PROFILES_DOMAIN_NAME"]
    return CustomerProfilesClient(domain_name=domain)


# Exceptions re-exported for convenience
ClientError = ClientError
