from __future__ import annotations

import os
from typing import Any

import boto3


class ConnectClient:
    """Wrapper around boto3 connect (core) for queue/flow/phone lookups."""

    def __init__(self, instance_id: str, boto_client=None) -> None:
        self._instance_id = instance_id
        self._client = boto_client or boto3.client("connect")

    def list_queues(self, queue_types: list[str] | None = None) -> list[dict]:
        """List queues, auto-paginated, filtered to STANDARD by default."""
        kwargs: dict[str, Any] = {"InstanceId": self._instance_id}
        if queue_types is None:
            queue_types = ["STANDARD"]
        kwargs["QueueTypes"] = queue_types

        items: list[dict] = []
        next_token: str | None = None
        while True:
            call_kwargs = dict(kwargs)
            if next_token:
                call_kwargs["NextToken"] = next_token
            response = self._client.list_queues(**call_kwargs)
            items.extend(response.get("QueueSummaryList", []))
            next_token = response.get("NextToken")
            if not next_token:
                break
        return items

    def list_contact_flows(
        self, contact_flow_types: list[str] | None = None
    ) -> list[dict]:
        """List contact flows, auto-paginated."""
        kwargs: dict[str, Any] = {"InstanceId": self._instance_id}
        if contact_flow_types:
            kwargs["ContactFlowTypes"] = contact_flow_types

        items: list[dict] = []
        next_token: str | None = None
        while True:
            call_kwargs = dict(kwargs)
            if next_token:
                call_kwargs["NextToken"] = next_token
            response = self._client.list_contact_flows(**call_kwargs)
            items.extend(response.get("ContactFlowSummaryList", []))
            next_token = response.get("NextToken")
            if not next_token:
                break
        return items

    def list_phone_numbers(self, target_arn: str) -> list[dict]:
        """List phone numbers claimed on the instance, auto-paginated."""
        kwargs: dict[str, Any] = {"TargetArn": target_arn}

        items: list[dict] = []
        next_token: str | None = None
        while True:
            call_kwargs = dict(kwargs)
            if next_token:
                call_kwargs["NextToken"] = next_token
            response = self._client.list_phone_numbers_v2(**call_kwargs)
            items.extend(response.get("ListPhoneNumbersSummaryList", []))
            next_token = response.get("NextToken")
            if not next_token:
                break
        return items

    def get_current_metric_data(self, queue_id: str, metrics: list[str]) -> list[dict]:
        """Real-time metrics for a queue (agents available, contacts in queue, etc.)."""
        current_metrics = [{"Name": m, "Unit": "COUNT"} for m in metrics]
        response = self._client.get_current_metric_data(
            InstanceId=self._instance_id,
            Filters={"Queues": [queue_id]},
            CurrentMetrics=current_metrics,
        )
        return response.get("MetricResults", [])

    def get_current_user_data(
        self,
        filters: dict,
        max_results: int = 100,
    ) -> list[dict]:
        """Per-agent real-time status data. Not PHI — agent IDs are employee identifiers."""
        response = self._client.get_current_user_data(
            InstanceId=self._instance_id,
            Filters=filters,
            MaxResults=max_results,
        )
        return response.get("UserDataList", [])

    def search_contacts(
        self,
        *,
        start_time,
        end_time,
        initiation_methods: list[str] | None = None,
        campaign_id: str | None = None,
        max_results: int = 100,
    ) -> list[dict]:
        """Query contact records (for dispositions report)."""
        criteria: dict[str, Any] = {}
        if initiation_methods:
            criteria["InitiationMethods"] = initiation_methods
        if campaign_id:
            criteria["Campaign"] = {"CampaignId": campaign_id}

        response = self._client.search_contacts(
            InstanceId=self._instance_id,
            TimeRange={
                "Type": "INITIATION_TIMESTAMP",
                "StartTime": start_time,
                "EndTime": end_time,
            },
            SearchCriteria=criteria,
            MaxResults=max_results,
        )
        return response.get("Contacts", [])


def build_from_env() -> ConnectClient:
    instance_id = os.environ["CONNECT_INSTANCE_ID"]
    return ConnectClient(instance_id=instance_id)
