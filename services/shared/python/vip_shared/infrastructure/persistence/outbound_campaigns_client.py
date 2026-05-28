from __future__ import annotations

import boto3


class OutboundCampaignsClient:
    """Thin wrapper around boto3 connectcampaignsv2 client.

    All Outbound Campaigns operations in this project use the V2 API.
    V1 `connectcampaigns` is not wrapped because we archived the feeder.
    """

    def __init__(self, boto_client=None) -> None:
        self._client = boto_client or boto3.client("connectcampaignsv2")

    # ── Lifecycle ─────────────────────────────────────────────────────

    def list_campaigns(
        self, max_results: int = 25, next_token: str | None = None
    ) -> dict:
        kwargs: dict = {"maxResults": max_results}
        if next_token:
            kwargs["nextToken"] = next_token
        return self._client.list_campaigns(**kwargs)

    def describe_campaign(self, campaign_id: str) -> dict:
        return self._client.describe_campaign(id=campaign_id)

    def get_campaign_state(self, campaign_id: str) -> dict:
        return self._client.get_campaign_state(id=campaign_id)

    def create_campaign(self, **kwargs) -> dict:
        """Pass-through — caller provides the full schema.

        Expected keys: name, connectInstanceId, connectCampaignFlowArn,
        channelSubtypeConfig, source, schedule, communicationTimeConfig (optional),
        communicationLimitsOverride (optional), tags (optional).
        """
        return self._client.create_campaign(**kwargs)

    def delete_campaign(self, campaign_id: str) -> None:
        self._client.delete_campaign(id=campaign_id)

    def start_campaign(self, campaign_id: str) -> dict:
        return self._client.start_campaign(id=campaign_id)

    def stop_campaign(self, campaign_id: str) -> dict:
        return self._client.stop_campaign(id=campaign_id)

    def pause_campaign(self, campaign_id: str) -> dict:
        return self._client.pause_campaign(id=campaign_id)

    def resume_campaign(self, campaign_id: str) -> dict:
        return self._client.resume_campaign(id=campaign_id)

    # ── Updates (narrow to what the UI supports) ──────────────────────

    def update_campaign_name(self, campaign_id: str, name: str) -> dict:
        return self._client.update_campaign_name(id=campaign_id, name=name)

    def update_campaign_source(self, campaign_id: str, source: dict) -> dict:
        return self._client.update_campaign_source(id=campaign_id, source=source)

    def update_campaign_schedule(self, campaign_id: str, schedule: dict) -> dict:
        return self._client.update_campaign_schedule(id=campaign_id, schedule=schedule)


def build() -> OutboundCampaignsClient:
    return OutboundCampaignsClient()
