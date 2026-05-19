from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import boto3


DEFAULT_CAMPAIGN_METRICS = (
    "Delivery",
    "ContactsPlaced",
    "ContactsAnswered",
    "ContactsAbandoned",
    "AMDDetected",
    "ContactsBusyDisposition",
    "ContactsNoAnswerDisposition",
    "ContactsFailedDisposition",
)


class CloudWatchMetricsClient:
    """Wrapper around CloudWatch GetMetricStatistics for Outbound Campaigns metrics."""

    def __init__(self, boto_client=None) -> None:
        self._client = boto_client or boto3.client("cloudwatch")

    def get_campaign_metric_series(
        self,
        campaign_id: str,
        metric_name: str,
        *,
        period_minutes: int = 60,
        lookback_hours: int = 24,
        now: datetime | None = None,
    ) -> list[dict]:
        """Return datapoints (timestamp + sum) for a campaign metric."""
        clock = now or datetime.now(tz=timezone.utc)
        end = clock
        start = clock - timedelta(hours=lookback_hours)

        response = self._client.get_metric_statistics(
            Namespace="AWS/Connect/Campaigns",
            MetricName=metric_name,
            Dimensions=[{"Name": "CampaignId", "Value": campaign_id}],
            StartTime=start,
            EndTime=end,
            Period=period_minutes * 60,
            Statistics=["Sum"],
        )
        return sorted(
            (
                {"timestamp": dp["Timestamp"].isoformat(), "value": float(dp["Sum"])}
                for dp in response.get("Datapoints", [])
            ),
            key=lambda p: p["timestamp"],
        )

    def get_campaign_totals(
        self,
        campaign_id: str,
        *,
        metrics: tuple[str, ...] = DEFAULT_CAMPAIGN_METRICS,
        lookback_hours: int = 24,
        now: datetime | None = None,
    ) -> dict[str, float]:
        """One call per metric; return totals over the window."""
        totals: dict[str, float] = {}
        for metric in metrics:
            series = self.get_campaign_metric_series(
                campaign_id,
                metric,
                period_minutes=lookback_hours * 60,
                lookback_hours=lookback_hours,
                now=now,
            )
            totals[metric] = sum(p["value"] for p in series)
        return totals


def build() -> CloudWatchMetricsClient:
    return CloudWatchMetricsClient()
