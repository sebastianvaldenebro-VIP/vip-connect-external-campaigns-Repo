"""CloudWatch metric handlers for campaigns and queues."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from vip_shared.application.http import json_response
from vip_shared.infrastructure.persistence.cloudwatch_client import (
    DEFAULT_CAMPAIGN_METRICS,
    CloudWatchMetricsClient,
    build as build_cw,
)
from vip_shared.infrastructure.persistence.connect_client import build_from_env as build_connect
from vip_shared.infrastructure.persistence.outbound_campaigns_client import build as build_oc


def get_campaign_metrics(event: dict, path_params: dict) -> dict:
    """GET /metrics/campaigns/{id}?lookbackHours=24&period=60

    Returns totals + time-series per metric for a specific campaign.
    """
    campaign_id = path_params["id"]
    qs = event.get("queryStringParameters") or {}
    lookback_hours = int(qs.get("lookbackHours", 24))
    period_minutes = int(qs.get("period", 60))

    cw = build_cw()

    totals = cw.get_campaign_totals(campaign_id, lookback_hours=lookback_hours)

    # Time series for the most important metric
    series = {
        metric: cw.get_campaign_metric_series(
            campaign_id,
            metric,
            period_minutes=period_minutes,
            lookback_hours=lookback_hours,
        )
        for metric in ("Delivery", "ContactsAnswered", "ContactsPlaced")
    }

    return json_response(
        200,
        {
            "campaignId": campaign_id,
            "lookbackHours": lookback_hours,
            "totals": totals,
            "series": series,
        },
    )


def get_campaigns_summary(event: dict, _path_params: dict) -> dict:
    """GET /metrics/campaigns?lookbackHours=24

    Aggregate per-campaign totals for the dashboard.
    """
    qs = event.get("queryStringParameters") or {}
    lookback_hours = int(qs.get("lookbackHours", 24))

    oc = build_oc()
    cw = build_cw()

    list_resp = oc.list_campaigns(max_results=25)
    summaries: list[dict] = []
    for campaign in list_resp.get("campaignSummaryList", []):
        cid = campaign.get("id")
        if not cid:
            continue
        totals = cw.get_campaign_totals(cid, lookback_hours=lookback_hours)
        summaries.append(
            {
                "id": cid,
                "name": campaign.get("name"),
                "status": campaign.get("status"),
                "totals": totals,
            }
        )

    return json_response(
        200,
        {
            "lookbackHours": lookback_hours,
            "campaigns": summaries,
        },
    )


def get_queue_realtime(event: dict, path_params: dict) -> dict:
    """GET /metrics/queues/{queueId} — real-time agent availability."""
    queue_id = path_params["queueId"]
    client = build_connect()
    results = client.get_current_metric_data(
        queue_id=queue_id,
        metrics=["AGENTS_AVAILABLE", "AGENTS_ONLINE", "AGENTS_STAFFED", "CONTACTS_IN_QUEUE"],
    )
    collections = results[0].get("Collections", []) if results else []
    metrics_map = {c["Metric"]["Name"]: c["Value"] for c in collections}
    return json_response(200, {"queueId": queue_id, "metrics": metrics_map})


def get_current_realtime(event: dict, _path_params: dict) -> dict:
    """GET /metrics/current?queueId=... — convenience wrapper."""
    qs = event.get("queryStringParameters") or {}
    queue_id = qs.get("queueId")
    if not queue_id:
        raise ValueError("Missing required query param: queueId")
    return get_queue_realtime({}, {"queueId": queue_id})


def get_dispositions(event: dict, _path_params: dict) -> dict:
    """GET /metrics/dispositions?campaignId=...&lookbackHours=24

    Groups contact records by DisconnectReason.
    """
    qs = event.get("queryStringParameters") or {}
    campaign_id = qs.get("campaignId")
    lookback_hours = int(qs.get("lookbackHours", 24))
    if not campaign_id:
        raise ValueError("Missing required query param: campaignId")

    client = build_connect()
    now = datetime.now(tz=timezone.utc)
    start = now - timedelta(hours=lookback_hours)

    contacts = client.search_contacts(
        start_time=start,
        end_time=now,
        initiation_methods=["OUTBOUND"],
        campaign_id=campaign_id,
    )

    buckets: dict[str, int] = {}
    for c in contacts:
        reason = c.get("DisconnectReason") or "UNKNOWN"
        buckets[reason] = buckets.get(reason, 0) + 1

    total = sum(buckets.values())
    return json_response(
        200,
        {
            "campaignId": campaign_id,
            "lookbackHours": lookback_hours,
            "totalContacts": total,
            "breakdown": [
                {
                    "disconnectReason": reason,
                    "count": count,
                    "percent": round((count / total) * 100, 2) if total else 0,
                }
                for reason, count in sorted(buckets.items(), key=lambda x: -x[1])
            ],
        },
    )
