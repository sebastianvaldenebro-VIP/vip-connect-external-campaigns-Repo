"""Branded campaign metrics collector.

Invoked every 1 minute by EventBridge. Scans VipActiveBrandedCampaigns,
calls SearchContacts + GetCurrentMetricData per active campaign queue, and
writes time-series snapshots to VipBrandedCampaignMetrics and VipAgentSnapshot.

PHI rule: no phone numbers are read or written here. All data is aggregate counts.
"""

import logging
import os
from datetime import datetime, timezone, timedelta

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_ACTIVE_TABLE = os.environ.get("ACTIVE_BRANDED_CAMPAIGNS_TABLE", "")
_METRICS_TABLE = os.environ.get("BRANDED_CAMPAIGN_METRICS_TABLE", "")
_SNAPSHOT_TABLE = os.environ.get("AGENT_SNAPSHOT_TABLE", "")
_CAMPAIGN_QUEUE_TABLE = os.environ.get("PROGRESSIVE_CAMPAIGN_QUEUE_TABLE", "VipProgressiveCampaignQueue")
_CONNECT_INSTANCE_ID = os.environ.get("CONNECT_INSTANCE_ID", "")

_connect = boto3.client("connect")
_ddb = boto3.resource("dynamodb")
_cw = boto3.client("cloudwatch")

_STALL_LOOKBACK_MINUTES = 10


def lambda_handler(event, context):
    active = _scan_active_campaigns()
    now = datetime.now(timezone.utc)

    # Emit both metrics unconditionally — cleaner alarm signal (no gap when count=0).
    _emit_business_hours_metric(len(active), now)
    _emit_stuck_campaigns_metric(active, now)

    if not active:
        logger.info("metrics_collector: no active branded campaigns")
        return {"collected": 0, "queues": 0}

    now_iso = now.isoformat()
    seen_queues: dict[str, dict] = {}
    collected = 0

    for item in active:
        campaign_id = item.get("campaignId", "")
        queue_arn = item.get("queueArn", "")
        queue_id = queue_arn.split("/")[-1] if queue_arn else ""
        plan_id = item.get("planId", "")
        run_id = item.get("runId", "")
        started_at = item.get("createdAt", now_iso)

        if not campaign_id or not queue_id:
            continue

        _resolve_outcomes(campaign_id)
        outcomes = _count_outcomes(campaign_id)
        if outcomes is None:
            logger.warning(
                "metrics_collector: skipping campaign=%s this cycle — outcomes query failed",
                campaign_id,
            )
            # A sustained failure here (lost IAM permission, sustained
            # throttling) would otherwise produce zero CloudWatch signal —
            # same blind spot BD-021 item 7 closed for _check_and_emit_stall's
            # own query, one level up (root-caused 2026-08-27, second
            # adversarial review round).
            try:
                _cw.put_metric_data(
                    Namespace="VipBrandedMonitor",
                    MetricData=[
                        {
                            "MetricName": "BrandedOutcomesQueryFailed",
                            "Value": 1,
                            "Unit": "Count",
                            "Dimensions": [
                                {"Name": "PlanId", "Value": plan_id},
                                {"Name": "CampaignId", "Value": campaign_id},
                            ],
                        }
                    ],
                )
            except Exception:
                pass  # metric emission must never abort the collector loop
            continue
        placed, answered, voicemail, busy, no_answer = outcomes

        if queue_id not in seen_queues:
            seen_queues[queue_id] = _queue_metrics(queue_id, queue_arn)
        qm = seen_queues[queue_id]

        _check_and_emit_stall(
            campaign_id=campaign_id,
            plan_id=plan_id,
            placed=placed,
            agents_available=qm.get("AGENTS_AVAILABLE", 0),
            now_utc=now,
        )

        ttl = int((now + timedelta(days=90)).timestamp())
        _ddb.Table(_METRICS_TABLE).put_item(Item={
            "brandedCampaignId": campaign_id,
            "snapshotAt":        now_iso,
            "planId":            plan_id,
            "runId":             run_id,
            "queueArn":          queue_arn,
            "windowStart":       started_at,
            "windowEnd":         now_iso,
            "contactsPlaced":    placed,
            "contactsAnswered":  answered,
            "contactsVoicemail": voicemail,
            "contactsBusy":      busy,
            "contactsNoAnswer":  no_answer,
            "answerRate":        str(round(answered / placed * 100, 1)) if placed else "0.0",
            "voicemailRate":     str(round(voicemail / placed * 100, 1)) if placed else "0.0",
            "agentsOnCall":      qm.get("AGENTS_ON_CONTACT", 0),
            "agentsAvailable":   qm.get("AGENTS_AVAILABLE", 0),
            "agentsStaffed":     qm.get("AGENTS_STAFFED", 0),
            "contactsInQueue":   qm.get("CONTACTS_IN_QUEUE", 0),
            "ttl":               ttl,
        })
        collected += 1
        logger.info(
            "metrics_collector: wrote snapshot for campaign=%s placed=%d answer_rate=%s%%",
            campaign_id, placed,
            str(round(answered / placed * 100, 1)) if placed else "0.0",
        )

    snap_ttl = int((now + timedelta(days=30)).timestamp())
    for queue_id, qm in seen_queues.items():
        queue_arn = qm.get("_queueArn", "")
        _ddb.Table(_SNAPSHOT_TABLE).put_item(Item={
            "queueId":        queue_id,
            "snapshotAt":     now_iso,
            "queueArn":       queue_arn,
            "agentsAvailable": qm.get("AGENTS_AVAILABLE", 0),
            "agentsStaffed":  qm.get("AGENTS_STAFFED", 0),
            "agentsOnline":   qm.get("AGENTS_ONLINE", 0),
            "contactsInQueue": qm.get("CONTACTS_IN_QUEUE", 0),
            "ttl":            snap_ttl,
        })

    logger.info("metrics_collector: done collected=%d queues=%d", collected, len(seen_queues))
    return {"collected": collected, "queues": len(seen_queues)}


def _scan_active_campaigns() -> list[dict]:
    if not _ACTIVE_TABLE:
        return []
    table = _ddb.Table(_ACTIVE_TABLE)
    items: list[dict] = []
    resp = table.scan(
        ProjectionExpression="campaignId, queueArn, planId, runId, createdAt",
    )
    items.extend(resp["Items"])
    while "LastEvaluatedKey" in resp:
        resp = table.scan(
            ExclusiveStartKey=resp["LastEvaluatedKey"],
            ProjectionExpression="campaignId, queueArn, planId, runId, createdAt",
        )
        items.extend(resp["Items"])
    return items


def _determine_outcome(contact: dict) -> str | None:
    """Derive outcome string from a DescribeContact response body.

    Returns one of 'answered' | 'voicemail' | 'busy' | 'no_answer',
    or None when the contact is still in progress (DisconnectTimestamp absent).

    Heuristic:
    - AgentInfo.ConnectedToAgentTimestamp set            → answered
    - DisconnectReason == TELECOM_PROBLEM / FLOW_ERROR  → busy (carrier reject)
    - ConnectedToSystemTimestamp set but no agent        → voicemail (AMD + flow played message)
    - Otherwise                                          → no_answer
    """
    if not contact.get("DisconnectTimestamp"):
        return None  # still in progress

    agent_info = contact.get("AgentInfo") or {}
    if agent_info.get("ConnectedToAgentTimestamp"):
        return "answered"

    disconnect_reason = contact.get("DisconnectReason", "")
    if disconnect_reason in ("TELECOM_PROBLEM", "CONTACT_FLOW_ERROR"):
        return "busy"

    if contact.get("ConnectedToSystemTimestamp"):
        # Phone answered by AMD contact flow but never bridged to agent → voicemail message played
        return "voicemail"

    return "no_answer"


def _resolve_outcomes(campaign_id: str) -> None:
    """Call DescribeContact for each DIALED item without an outcome and persist the result.

    PHI rule: we read only contactId (non-PHI) — phone numbers are never touched here.
    Errors per contact are swallowed to avoid blocking the rest of the batch.
    """
    logger.info("_resolve_outcomes: start campaign=%s instance_id_set=%s", campaign_id, bool(_CONNECT_INSTANCE_ID))
    if not _CONNECT_INSTANCE_ID:
        return

    table = _ddb.Table(_CAMPAIGN_QUEUE_TABLE)
    try:
        resp = table.query(
            KeyConditionExpression="campaignId = :cid",
            FilterExpression="#s = :dialed AND attribute_not_exists(#o)",
            ExpressionAttributeNames={"#s": "status", "#o": "outcome"},
            ExpressionAttributeValues={":cid": campaign_id, ":dialed": "DIALED"},
            ProjectionExpression="sk, contactId",
        )
        items = resp.get("Items", [])
        logger.info("_resolve_outcomes: campaign=%s pending_outcome=%d", campaign_id, len(items))
    except Exception as exc:
        logger.error("_resolve_outcomes: query failed campaign=%s error=%s", campaign_id, type(exc).__name__, exc_info=True)
        return

    for item in items:
        contact_id = item.get("contactId")
        if not contact_id:
            continue
        try:
            detail = _connect.describe_contact(InstanceId=_CONNECT_INSTANCE_ID, ContactId=contact_id)
            outcome = _determine_outcome(detail.get("Contact", {}))
            if outcome is None:
                continue  # still in progress — check next minute
            table.update_item(
                Key={"campaignId": campaign_id, "sk": item["sk"]},
                UpdateExpression="SET #o = :o",
                ConditionExpression="attribute_not_exists(#o)",
                ExpressionAttributeNames={"#o": "outcome"},
                ExpressionAttributeValues={":o": outcome},
            )
        except Exception as exc:
            logger.warning(
                "_resolve_outcomes: contact=%s error=%s", contact_id, type(exc).__name__
            )


def _count_outcomes(campaign_id: str) -> tuple[int, int, int, int, int] | None:
    """Count DIALED items by outcome.

    Returns: (placed, answered, voicemail, busy, no_answer), or None if the
    query itself failed. None is NOT the same as a real (0,0,0,0,0) — a
    fabricated zero on a transient DynamoDB error would both persist a
    contaminated VipBrandedCampaignMetrics snapshot and falsely trigger
    _check_and_emit_stall's "zero progress" comparison (root-caused
    2026-08-27, adversarial code review). Callers must skip this cycle
    entirely on None, not treat it as genuine zero progress.
    placed = total DIALED regardless of outcome (includes in-progress calls)
    """
    try:
        resp = _ddb.Table(_CAMPAIGN_QUEUE_TABLE).query(
            KeyConditionExpression="campaignId = :cid",
            FilterExpression="#s = :d",
            ExpressionAttributeNames={"#s": "status", "#o": "outcome"},
            ExpressionAttributeValues={":cid": campaign_id, ":d": "DIALED"},
            ProjectionExpression="#o",
        )
        items = resp.get("Items", [])
        placed = len(items)
        answered = sum(1 for i in items if i.get("outcome") == "answered")
        voicemail = sum(1 for i in items if i.get("outcome") == "voicemail")
        busy = sum(1 for i in items if i.get("outcome") == "busy")
        no_answer = sum(1 for i in items if i.get("outcome") == "no_answer")
        return placed, answered, voicemail, busy, no_answer
    except Exception as exc:
        logger.error("_count_outcomes: campaign=%s error=%s", campaign_id, type(exc).__name__)
        return None



def _queue_metrics(queue_id: str, queue_arn: str) -> dict:
    """Return real-time queue metrics from GetCurrentMetricData."""
    result: dict = {"_queueArn": queue_arn}
    if not queue_id or not _CONNECT_INSTANCE_ID:
        return result
    try:
        resp = _connect.get_current_metric_data(
            InstanceId=_CONNECT_INSTANCE_ID,
            Filters={"Queues": [queue_id], "Channels": ["VOICE"]},
            Groupings=["QUEUE"],
            CurrentMetrics=[
                {"Name": "AGENTS_AVAILABLE",  "Unit": "COUNT"},
                {"Name": "AGENTS_STAFFED",    "Unit": "COUNT"},
                {"Name": "AGENTS_ONLINE",     "Unit": "COUNT"},
                {"Name": "AGENTS_ON_CONTACT", "Unit": "COUNT"},
                {"Name": "CONTACTS_IN_QUEUE", "Unit": "COUNT"},
            ],
        )
        for rc in resp.get("MetricResults", []):
            for cv in rc.get("Collections", []):
                result[cv["Metric"]["Name"]] = int(cv.get("Value", 0))
    except Exception as exc:
        logger.error("_queue_metrics: queue=%s error=%s", queue_id, type(exc).__name__)
    return result


def _emit_business_hours_metric(count: int, now_utc: datetime) -> None:
    """Publish ActiveBrandedCampaigns to CloudWatch during business hours only.

    Business hours: 7am-7pm COT = 12:00-23:59 UTC.
    Outside hours: skip emit — CloudWatch treats missing datapoints as notBreaching,
    so the alarm stays silent at night without any explicit suppression.
    """
    # 7am COT = 12:00 UTC, 7pm COT = 00:00 UTC (next day), so hours 12-23 = in-hours
    if not (12 <= now_utc.hour <= 23):
        return
    try:
        _cw.put_metric_data(
            Namespace="VipBrandedMonitor",
            MetricData=[{
                "MetricName": "ActiveBrandedCampaigns",
                "Timestamp": now_utc,
                "Value": float(count),
                "Unit": "Count",
            }],
        )
    except Exception as exc:
        logger.error("metrics_collector: failed to emit ActiveBrandedCampaigns metric: %s", exc)


def _check_and_emit_stall(
    campaign_id: str,
    plan_id: str,
    placed: int,
    agents_available: int,
    now_utc: datetime,
) -> None:
    """Emit BrandedCampaignStalled if this campaign made zero dialing progress in
    the last _STALL_LOOKBACK_MINUTES while agents are available right now.

    Neither StuckRun (4h threshold, whole-run level) nor NoActiveCampaign (checks
    campaign *status*, not throughput) catches a campaign that stays "running"
    indefinitely with near-zero dispatch despite free agent capacity — root-caused
    2026-08-27 with Plan 1.2/2.2 before the sweep-timer fix (BD-014).
    """
    if agents_available <= 0:
        return  # no available capacity right now — genuinely busy/understaffed, not stalled
    cutoff_iso = (now_utc - timedelta(minutes=_STALL_LOOKBACK_MINUTES)).isoformat()
    try:
        resp = _ddb.Table(_METRICS_TABLE).query(
            KeyConditionExpression=(
                Key("brandedCampaignId").eq(campaign_id)
                & Key("snapshotAt").lte(cutoff_iso)
            ),
            ScanIndexForward=False,
            Limit=1,
        )
    except Exception as exc:
        logger.warning(
            "metrics_collector: stall check query failed for %s: %s",
            campaign_id,
            type(exc).__name__,
        )
        try:
            _cw.put_metric_data(
                Namespace="VipBrandedMonitor",
                MetricData=[
                    {
                        "MetricName": "BrandedStallCheckError",
                        "Value": 1,
                        "Unit": "Count",
                        "Dimensions": [
                            {"Name": "PlanId", "Value": plan_id},
                            {"Name": "CampaignId", "Value": campaign_id},
                        ],
                    }
                ],
            )
        except Exception:
            pass  # the stall check itself failing must never raise
        return

    items = resp.get("Items", [])
    if not items:
        return  # campaign younger than the lookback window — not enough history yet

    # brandedCampaignId is deterministic per (planId, runId, bucket_index,
    # campaign_index) and survives a stop/force-restart within the same run.
    # Without this bound, the first post-restart cycle would compare against
    # an hours-old pre-restart snapshot and could emit a false stall right
    # after a legitimate restart (root-caused 2026-08-27, adversarial code
    # review).
    prior_snapshot_at = items[0].get("snapshotAt", "")
    try:
        prior_age_minutes = (
            now_utc - datetime.fromisoformat(prior_snapshot_at)
        ).total_seconds() / 60
    except (ValueError, TypeError):
        prior_age_minutes = float("inf")
    # The query itself already requires the found snapshot to be
    # >=_STALL_LOOKBACK_MINUTES old (Key("snapshotAt").lte(now-lookback)), so
    # the old `* 2` bound (20 min) left a false-positive window for restart
    # gaps between _STALL_LOOKBACK_MINUTES and 2x that (10-20 min) — only a
    # couple of minutes of slack for collector-cycle jitter is needed here,
    # not a full extra lookback window (root-caused 2026-08-27, second
    # adversarial review round).
    if prior_age_minutes > _STALL_LOOKBACK_MINUTES + 2:
        return  # gap too large — not enough continuous history to compare

    prior_placed = int(items[0].get("contactsPlaced", 0))
    if placed > prior_placed:
        return  # made progress since then — not stalled

    logger.warning(
        "metrics_collector: campaign %s stalled — placed=%d unchanged since %s "
        "(agents_available=%d)",
        campaign_id,
        placed,
        items[0].get("snapshotAt"),
        agents_available,
    )
    try:
        _cw.put_metric_data(
            Namespace="VipBrandedMonitor",
            MetricData=[
                {
                    "MetricName": "BrandedCampaignStalled",
                    "Value": 1,
                    "Unit": "Count",
                    "Dimensions": [
                        {"Name": "PlanId", "Value": plan_id},
                        {"Name": "CampaignId", "Value": campaign_id},
                    ],
                },
                {
                    "MetricName": "BrandedCampaignStalled",
                    "Value": 1,
                    "Unit": "Count",
                    "Dimensions": [],
                },
            ],
        )
    except Exception as exc:
        logger.error(
            "metrics_collector: stall metric emit failed for %s: %s",
            campaign_id,
            type(exc).__name__,
        )


def _emit_stuck_campaigns_metric(active: list[dict], now_utc: datetime) -> None:
    """Publish StuckBrandedCampaigns count to CloudWatch (always, not restricted to business hours).

    A campaign is 'stuck' when it has been in VipActiveBrandedCampaigns for >26 hours
    without being deleted. TTL is set to createdAt+24h; DynamoDB TTL processing can lag
    up to 48h, so 26h is the earliest reliable signal that the delete path was skipped.
    Root cause: _force_finish_internal or other early-exit paths missed _stop_branded_campaign.
    """
    cutoff = (now_utc - timedelta(hours=26)).isoformat()
    stuck_count = sum(1 for a in active if a.get("createdAt", "") < cutoff)
    if stuck_count:
        logger.warning(
            "metrics_collector: %d stuck campaign(s) detected (>26h in active table)",
            stuck_count,
        )
    try:
        _cw.put_metric_data(
            Namespace="VipBrandedMonitor",
            MetricData=[{
                "MetricName": "StuckBrandedCampaigns",
                "Timestamp": now_utc,
                "Value": float(stuck_count),
                "Unit": "Count",
            }],
        )
    except Exception as exc:
        logger.error("metrics_collector: failed to emit StuckBrandedCampaigns metric: %s", exc)
