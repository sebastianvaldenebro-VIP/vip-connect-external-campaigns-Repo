"""Campaign mapping exporter.

Primary source: Connect V2 ListCampaigns — includes all campaigns regardless
of whether they were created via the admin webapp.

Historical runs (campaigns already deleted from Connect) are also included via
DynamoDB scan so that Snowflake can join CTRs for the full history.

Lead count: CloudWatch AWS/Connect/Campaigns Delivery metric, 30-day rolling
window, fetched in a single batched GetMetricData call.

S3 path layout (flat date partition, compatible with specialOps Glue loader):
  s3://{DATA_EXPORT_BUCKET}/connect/raw/campaign_mapping/{YYYY-MM-DD}/

After writing, triggers specialOps-prod-snowflake-loader-glue with load_type=merge
so that Snowflake table NEXTECH.CONNECT.CAMPAIGN_MAPPING is kept current.
One row per connect_campaign_id; updates in place when campaign state changes.

Columns:
  connect_campaign_id  — Connect V2 campaign UUID (join key for CTRs)
  campaign_name        — business label
  segment_name         — Customer Profiles segment name (webapp runs only)
  segment_arn          — Customer Profiles segment ARN
  lead_count           — CloudWatch Delivery metric sum (30 days)
  campaign_status      — terminal status from DynamoDB run record
  connect_state        — live state from Connect API (Running/Stopped/…)
  exit_reason          — why the campaign stopped (DynamoDB)
  started_at           — UTC timestamp
  completed_at         — UTC timestamp
  bucket_index         — bucket position in plan (0-based)
  bucket_id            — bucket UUID
  bucket_name          — bucket display name
  plan_id              — plan UUID
  plan_name            — plan display name
  run_id               — run identifier
  run_date             — YYYY-MM-DD in COT
  triggered_by         — manual | loop | chained | scheduled | external
  extracted_at         — UTC timestamp of this export job
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Final

import boto3

logger = logging.getLogger(__name__)

DATA_EXPORT_BUCKET: Final = os.environ.get("DATA_EXPORT_BUCKET", "")
TABLE_NAME: Final = os.environ.get("PLANS_TABLE_NAME", "VipAdminPlans")
GLUE_JOB_NAME: Final = os.environ.get("GLUE_JOB_NAME", "")
SF_SRC_CFG_URI: Final = os.environ.get("SF_SRC_CFG_URI", "")

_SF_DATABASE: Final = os.environ.get("SF_DATABASE", "PRD_DATA_PRODUCT_DB")
_SF_SCHEMA: Final = os.environ.get("SF_SCHEMA", "INTEGRATIONS_AWS")
_SF_TABLE = "CAMPAIGN_MAPPING"
_SF_PK = "connect_campaign_id"
_SF_PIPELINE = "vip-connect"
_S3_PREFIX = "connect/raw/campaign_mapping"

_COT = timezone(timedelta(hours=-5))
_CW_LOOKBACK_DAYS = 30
_DESCRIBE_THROTTLE_SLEEP = 0.6  # seconds between DescribeCampaign calls (~1.5 TPS)


def export_campaign_mappings() -> dict:
    """Entry point called by handler.py for the campaign_export action."""
    if not DATA_EXPORT_BUCKET:
        raise RuntimeError("DATA_EXPORT_BUCKET env var is not set")

    extracted_at = datetime.now(timezone.utc).isoformat()

    # Step 1: Build DynamoDB index — historical context keyed by connect_campaign_id
    db_index = _build_db_index()

    # Step 2: All campaigns currently in Connect (including non-webapp)
    connect_campaigns = _list_connect_campaigns()

    # Step 3: Merge Connect + DynamoDB into unified campaign set
    all_ids = set(connect_campaigns) | set(db_index)
    if not all_ids:
        logger.info("exporter: no campaigns found")
        return {"exported": 0}

    # Step 4: Enrich campaigns not in DynamoDB with DescribeCampaign (segment ARN)
    _enrich_external_campaigns(connect_campaigns, db_index)

    # Step 5: Build rows
    rows = []
    for cid in all_ids:
        cc = connect_campaigns.get(cid, {})
        ctx = db_index.get(cid, {})
        rows.append({
            "connect_campaign_id": cid,
            "connect_campaign_name": cc.get("name") or ctx.get("segment_name", ""),
            "campaign_name": ctx.get("campaign_name") or cc.get("name", ""),
            "segment_name": ctx.get("segment_name", ""),
            "segment_arn": cc.get("segment_arn") or ctx.get("segment_arn", ""),
            "campaign_status": ctx.get("campaign_status", ""),
            "connect_state": cc.get("state", ""),
            "exit_reason": ctx.get("exit_reason", ""),
            "started_at": ctx.get("started_at"),
            "completed_at": ctx.get("completed_at"),
            "bucket_index": ctx.get("bucket_index"),
            "bucket_id": ctx.get("bucket_id", ""),
            "bucket_name": ctx.get("bucket_name", ""),
            "plan_id": ctx.get("plan_id", ""),
            "plan_name": ctx.get("plan_name", ""),
            "run_id": ctx.get("run_id", ""),
            "run_date": ctx.get("run_date", ""),
            "triggered_by": ctx.get("triggered_by", "external" if not ctx else ""),
            "extracted_at": extracted_at,
        })

    import awswrangler as wr
    import pandas as pd

    df = pd.DataFrame(rows)
    df["bucket_index"] = pd.to_numeric(df["bucket_index"], errors="coerce").astype("Int64")
    for col in ("started_at", "completed_at", "extracted_at"):
        df[col] = pd.to_datetime(df[col], errors="coerce", utc=True)
    df["run_date"] = pd.to_datetime(df["run_date"], errors="coerce").dt.date

    dt_label = datetime.now(_COT).strftime("%Y-%m-%d")
    s3_path = f"s3://{DATA_EXPORT_BUCKET}/{_S3_PREFIX}/{dt_label}/"

    wr.s3.to_parquet(
        df=df,
        path=s3_path,
        dataset=True,
        mode="overwrite",
        sanitize_columns=False,
    )

    logger.info("exporter: wrote %d rows to %s", len(rows), s3_path)

    glue_run_id = _trigger_snowflake_load(dt_label)

    return {"exported": len(rows), "s3_path": s3_path, "dt": dt_label, "glue_run_id": glue_run_id}


def _trigger_snowflake_load(dt_label: str) -> str | None:
    """Fire-and-forget: start the specialOps Glue Snowflake loader."""
    if not GLUE_JOB_NAME or not SF_SRC_CFG_URI:
        logger.warning("exporter: GLUE_JOB_NAME or SF_SRC_CFG_URI not set — skipping Glue trigger")
        return None
    try:
        glue = boto3.client("glue")
        resp = glue.start_job_run(
            JobName=GLUE_JOB_NAME,
            Arguments={
                "--sfDatabase": _SF_DATABASE,
                "--sfSchema": _SF_SCHEMA,
                "--input_path": f"s3://{DATA_EXPORT_BUCKET}/{_S3_PREFIX}",
                "--target_table": _SF_TABLE,
                "--run_id": dt_label,
                "--load_type": "merge",
                "--pk_columns": _SF_PK,
                "--src_cfg": SF_SRC_CFG_URI,
                "--pipeline": _SF_PIPELINE,
            },
        )
        run_id = resp.get("JobRunId", "")
        logger.info("exporter: triggered Glue job %s run %s", GLUE_JOB_NAME, run_id)
        return run_id
    except Exception as exc:
        logger.error("exporter: failed to trigger Glue job: %s", exc)
        return None


# ── Connect V2 helpers ────────────────────────────────────────────────────────


def _list_connect_campaigns() -> dict[str, dict]:
    """Returns {campaign_id: {name, segment_arn, state}} for all Connect campaigns."""
    client = boto3.client("connectcampaignsv2")
    campaigns: dict[str, dict] = {}

    kwargs: dict = {"maxResults": 50}
    while True:
        resp = client.list_campaigns(**kwargs)
        for c in resp.get("campaignSummaryList", []):
            campaigns[c["id"]] = {"name": c.get("name", ""), "segment_arn": "", "state": ""}
        token = resp.get("nextToken")
        if not token:
            break
        kwargs["nextToken"] = token

    if not campaigns:
        return campaigns

    # V2 has no batch state call — use get_campaign_state individually.
    # Throttle lightly; active campaign count is small in practice.
    for cid in campaigns:
        try:
            resp = client.get_campaign_state(id=cid)
            campaigns[cid]["state"] = resp.get("state", "")
            time.sleep(0.2)
        except Exception as exc:
            logger.warning("exporter: get_campaign_state %s failed: %s", cid, exc)

    return campaigns


def _enrich_external_campaigns(
    connect_campaigns: dict[str, dict],
    db_index: dict[str, dict],
) -> None:
    """DescribeCampaign for campaigns not in DynamoDB to get segment_arn.

    Campaigns created via the webapp already have segment_arn in the DB index.
    This only runs for campaigns created outside the webapp, keeping API calls minimal.
    """
    client = boto3.client("connectcampaignsv2")
    external_ids = [cid for cid in connect_campaigns if cid not in db_index]

    for cid in external_ids:
        try:
            resp = client.describe_campaign(id=cid)
            source = resp.get("campaign", {}).get("source", {})
            connect_campaigns[cid]["segment_arn"] = source.get("customerProfilesSegmentArn", "")
            time.sleep(_DESCRIBE_THROTTLE_SLEEP)
        except Exception as exc:
            logger.warning("exporter: describe_campaign %s failed: %s", cid, exc)


# ── CloudWatch helpers ────────────────────────────────────────────────────────


def _get_lead_counts_cw(campaign_ids: list[str]) -> dict[str, int | None]:
    """Batch-fetch CloudWatch Delivery metric sum per campaign (30-day window).

    Uses GetMetricData with up to 500 queries per API call.
    Delivery = contacts successfully delivered to agents (best proxy for dialed count).
    """
    if not campaign_ids:
        return {}

    cw = boto3.client("cloudwatch")
    now = datetime.now(tz=timezone.utc)
    start = now - timedelta(days=_CW_LOOKBACK_DAYS)
    period = _CW_LOOKBACK_DAYS * 86400  # one single bucket = whole window

    id_map: dict[str, str] = {}  # metric query id → campaign_id
    metric_queries = []
    for i, cid in enumerate(campaign_ids):
        mid = f"m{i}"
        id_map[mid] = cid
        metric_queries.append({
            "Id": mid,
            "MetricStat": {
                "Metric": {
                    "Namespace": "AWS/Connect/Campaigns",
                    "MetricName": "Delivery",
                    "Dimensions": [{"Name": "CampaignId", "Value": cid}],
                },
                "Period": period,
                "Stat": "Sum",
            },
            "ReturnData": True,
        })

    counts: dict[str, int | None] = {cid: None for cid in campaign_ids}

    paginator = cw.get_paginator("get_metric_data")
    for batch_start in range(0, len(metric_queries), 500):
        batch = metric_queries[batch_start : batch_start + 500]
        try:
            for page in paginator.paginate(MetricDataQueries=batch, StartTime=start, EndTime=now):
                for result in page.get("MetricDataResults", []):
                    cid = id_map.get(result["Id"])
                    if cid and result.get("Values"):
                        counts[cid] = int(sum(result["Values"]))
        except Exception as exc:
            logger.warning("exporter: CloudWatch GetMetricData failed: %s", exc)

    return counts


# ── DynamoDB helpers ──────────────────────────────────────────────────────────


def _build_db_index() -> dict[str, dict]:
    """Scan all RUN# items in DynamoDB and index by connect_campaign_id.

    When multiple run records contain the same connect_campaign_id (re-use across
    runs is rare but possible), the most-recent startedAt wins.
    """
    table = boto3.resource("dynamodb").Table(TABLE_NAME)
    from boto3.dynamodb.conditions import Attr

    index: dict[str, dict] = {}
    kwargs: dict = {"FilterExpression": Attr("sk").begins_with("RUN#")}

    while True:
        result = table.scan(**kwargs)
        for item in result.get("Items", []):
            _index_run(item, index)
        last = result.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last

    return index


def _index_run(item: dict, index: dict[str, dict]) -> None:
    plan_id = item.get("planId", "")
    run_id = item.get("runId", "")
    triggered_by = item.get("triggeredBy", "")
    snapshot = item.get("planSnapshot") or {}
    plan_name = snapshot.get("name") or item.get("name", "")
    started_at_iso = item.get("startedAt", "")
    run_date = _cot_date(started_at_iso) if started_at_iso else ""

    for bi, bs in enumerate(item.get("bucketStates") or []):
        for cs in bs.get("campaignStates") or []:
            cid = cs.get("connectCampaignId")
            if not cid:
                continue
            existing = index.get(cid)
            if existing and existing.get("started_at", "") > _norm_ts(cs.get("startedAt")) or "":
                continue  # keep the more-recent run record
            index[cid] = {
                "campaign_name": cs.get("name", ""),
                "segment_name": cs.get("segmentName", ""),
                "segment_arn": cs.get("segmentArn", ""),
                "campaign_status": cs.get("status", ""),
                "exit_reason": cs.get("exitReason", ""),
                "started_at": _norm_ts(cs.get("startedAt")),
                "completed_at": _norm_ts(cs.get("completedAt")),
                "bucket_index": bi,
                "bucket_id": bs.get("bucketId", ""),
                "bucket_name": bs.get("name", ""),
                "plan_id": plan_id,
                "plan_name": plan_name,
                "run_id": run_id,
                "run_date": run_date,
                "triggered_by": triggered_by,
            }


# ── Timestamp helpers ─────────────────────────────────────────────────────────


def _norm_ts(val) -> str | None:
    if val is None:
        return None
    try:
        from decimal import Decimal
        if isinstance(val, (int, float, Decimal)):
            return datetime.fromtimestamp(float(val) / 1000, tz=timezone.utc).isoformat()
        s = str(val).strip()
        if not s:
            return None
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except Exception:
        return str(val) if val else None


def _cot_date(val) -> str:
    try:
        from decimal import Decimal
        if isinstance(val, (int, float, Decimal)):
            dt = datetime.fromtimestamp(float(val) / 1000, tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(val).strip())
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_COT).strftime("%Y-%m-%d")
    except Exception:
        return ""
