"""Export VipBrandedRunSummary and VipBrandedCampaignMetrics to S3 Parquet → Snowflake.

PHI rule: sourcePhoneLast4 (last 4 digits) is safe to export. Full phone numbers never
appear in these tables — they live exclusively in VipProgressiveCampaignQueue.

Architecture note: SearchContacts has a 2-10 min indexing lag. For in-progress
campaigns this means real-time snapshots are slightly understated. For the nightly
export, campaigns have been complete for hours — lag is irrelevant.

Known limitation: if two branded campaigns run simultaneously on the same queue,
their SearchContacts counts cannot be distinguished per campaign. In practice the
bucket chain design ensures sequential execution.
"""
import os
from datetime import datetime, timezone

import boto3
import awswrangler as wr
import pandas as pd

_RUN_SUMMARY_TABLE  = os.environ.get("BRANDED_RUN_SUMMARY_TABLE", "VipBrandedRunSummary")
_METRICS_TABLE      = os.environ.get("BRANDED_CAMPAIGN_METRICS_TABLE", "VipBrandedCampaignMetrics")
_DATA_EXPORT_BUCKET = os.environ["DATA_EXPORT_BUCKET"]
_GLUE_JOB_NAME      = os.environ["GLUE_JOB_NAME"]
_SF_SRC_CFG_URI     = os.environ["SF_SRC_CFG_URI"]
_SF_DATABASE        = os.environ.get("SF_DATABASE", "PRD_RAW_DB")
_SF_SCHEMA          = os.environ.get("SF_SCHEMA", "AMAZON_CONNECT")
_PIPELINE_VERSION   = "v1"

_ddb  = boto3.resource("dynamodb")
_glue = boto3.client("glue")


def export_branded_runs() -> dict:
    """Scan VipBrandedRunSummary, enrich with final disposition from last METRICS
    snapshot, write Parquet to S3, trigger Glue Snowflake loader."""
    now = datetime.now(timezone.utc)
    extracted_at = now.isoformat()
    dt_label = now.strftime("%Y-%m-%d")

    runs = _scan_table(_RUN_SUMMARY_TABLE)
    if not runs:
        return {"exported": 0, "table": "BRANDED_CAMPAIGN_RUNS"}

    # Build index of last METRICS snapshot per branded_campaign_id for final_* fields.
    # Graceful degradation: if VipBrandedCampaignMetrics doesn't exist yet (pre-Phase-1
    # deploy), last_metric stays empty and final_* fields default to 0.
    last_metric: dict[str, dict] = {}
    if _METRICS_TABLE:
        for m in _scan_table(_METRICS_TABLE):
            cid = m.get("brandedCampaignId", "")
            if not cid:
                continue
            existing = last_metric.get(cid)
            if not existing or m.get("snapshotAt", "") > existing.get("snapshotAt", ""):
                last_metric[cid] = m

    rows = []
    for item in runs:
        cid = item.get("brandedCampaignId", "")
        lm = last_metric.get(cid, {})
        started = item.get("startedAt", "")
        completed = item.get("completedAt", "")
        rows.append({
            "plan_id":                  item.get("planId", ""),
            "sk":                       item.get("sk", ""),
            "run_id":                   item.get("runId", ""),
            "campaign_id":              item.get("campaignId", ""),
            "branded_campaign_id":      cid,
            "plan_name":                item.get("planName", ""),
            "segment_arn":              item.get("segmentArn", ""),
            "segment_name":             item.get("segmentName", ""),
            "segment_definition_json":  item.get("segmentDefinitionJson", "{}"),
            "segment_size":             int(item.get("segmentSize", 0) or 0),
            "contact_flow_id":          item.get("contactFlowId", ""),
            "queue_arn":                item.get("queueArn", ""),
            "source_phone_last4":       item.get("sourcePhoneLast4", ""),
            "bucket_index":             int(item.get("bucketIndex", 0) or 0),
            "priority":                 int(item.get("priority", 0) or 0),
            "status":                   item.get("status", ""),
            "started_at":               started,
            "completed_at":             completed,
            "total_seeded":             int(item.get("totalSeeded", 0) or 0),
            "total_dialed":             int(item.get("totalDialed", 0) or 0),
            "exit_reason":              item.get("exitReason", ""),
            "duration_seconds":         int(item.get("durationSeconds", 0) or 0),
            # Final disposition — from last METRICS snapshot (accurate for completed campaigns)
            "final_contacts_placed":    int(lm.get("contactsPlaced", 0) or 0),
            "final_contacts_answered":  int(lm.get("contactsAnswered", 0) or 0),
            "final_contacts_voicemail": int(lm.get("contactsVoicemail", 0) or 0),
            "final_contacts_busy":      int(lm.get("contactsBusy", 0) or 0),
            "final_contacts_no_answer": int(lm.get("contactsNoAnswer", 0) or 0),
            "final_answer_rate":        float(lm.get("answerRate", "0") or "0"),
            "final_voicemail_rate":     float(lm.get("voicemailRate", "0") or "0"),
            # Audit columns
            "created_at":               started,
            "updated_at":               completed or started,
            "extracted_at":             extracted_at,
            "pipeline_version":         _PIPELINE_VERSION,
        })

    df = pd.DataFrame(rows)
    for col in ("started_at", "completed_at", "created_at", "updated_at"):
        df[col] = pd.to_datetime(df[col], utc=True, errors="coerce")
    df["extracted_at"] = pd.to_datetime(df["extracted_at"], utc=True)

    s3_path = f"s3://{_DATA_EXPORT_BUCKET}/connect/raw/branded_campaign_runs/{dt_label}/"
    wr.s3.to_parquet(df, path=s3_path, mode="overwrite", dataset=True)
    _trigger_glue(
        target_table="BRANDED_CAMPAIGN_RUNS",
        pk_columns="plan_id,sk",
        input_path=f"s3://{_DATA_EXPORT_BUCKET}/connect/raw/branded_campaign_runs",
        run_id=dt_label,
    )
    return {"exported": len(rows), "table": "BRANDED_CAMPAIGN_RUNS", "s3_path": s3_path}


def export_branded_metrics() -> dict:
    """Scan VipBrandedCampaignMetrics, write Parquet, trigger Glue load."""
    now = datetime.now(timezone.utc)
    extracted_at = now.isoformat()
    dt_label = now.strftime("%Y-%m-%d")

    items = _scan_table(_METRICS_TABLE)
    if not items:
        return {"exported": 0, "table": "BRANDED_CAMPAIGN_METRICS"}

    rows = []
    for item in items:
        snap = item.get("snapshotAt", "")
        rows.append({
            "branded_campaign_id": item.get("brandedCampaignId", ""),
            "snapshot_at":         snap,
            "plan_id":             item.get("planId", ""),
            "run_id":              item.get("runId", ""),
            "queue_arn":           item.get("queueArn", ""),
            "window_start":        item.get("windowStart", ""),
            "window_end":          item.get("windowEnd", ""),
            "contacts_placed":     int(item.get("contactsPlaced", 0) or 0),
            "contacts_answered":   int(item.get("contactsAnswered", 0) or 0),
            "contacts_voicemail":  int(item.get("contactsVoicemail", 0) or 0),
            "contacts_busy":       int(item.get("contactsBusy", 0) or 0),
            "contacts_no_answer":  int(item.get("contactsNoAnswer", 0) or 0),
            "contacts_failed":     int(item.get("contactsFailed", 0) or 0),
            "answer_rate":         float(item.get("answerRate", "0") or "0"),
            "voicemail_rate":      float(item.get("voicemailRate", "0") or "0"),
            "agents_on_call":      int(item.get("agentsOnCall", 0) or 0),
            "agents_available":    int(item.get("agentsAvailable", 0) or 0),
            "agents_staffed":      int(item.get("agentsStaffed", 0) or 0),
            "contacts_in_queue":   int(item.get("contactsInQueue", 0) or 0),
            # Audit columns
            "created_at":          snap,
            "extracted_at":        extracted_at,
            "pipeline_version":    _PIPELINE_VERSION,
        })

    df = pd.DataFrame(rows)
    for col in ("snapshot_at", "window_start", "window_end", "created_at"):
        df[col] = pd.to_datetime(df[col], utc=True, errors="coerce")
    df["extracted_at"] = pd.to_datetime(df["extracted_at"], utc=True)

    s3_path = f"s3://{_DATA_EXPORT_BUCKET}/connect/raw/branded_campaign_metrics/{dt_label}/"
    wr.s3.to_parquet(df, path=s3_path, mode="overwrite", dataset=True)
    _trigger_glue(
        target_table="BRANDED_CAMPAIGN_METRICS",
        pk_columns="branded_campaign_id,snapshot_at",
        input_path=f"s3://{_DATA_EXPORT_BUCKET}/connect/raw/branded_campaign_metrics",
        run_id=dt_label,
    )
    return {"exported": len(rows), "table": "BRANDED_CAMPAIGN_METRICS", "s3_path": s3_path}


def _scan_table(table_name: str) -> list[dict]:
    if not table_name:
        return []
    table = _ddb.Table(table_name)
    items: list[dict] = []
    resp = table.scan()
    items.extend(resp["Items"])
    while "LastEvaluatedKey" in resp:
        resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        items.extend(resp["Items"])
    return items


def _trigger_glue(target_table: str, pk_columns: str, input_path: str, run_id: str) -> None:
    _glue.start_job_run(
        JobName=_GLUE_JOB_NAME,
        Arguments={
            "--sfDatabase":   _SF_DATABASE,
            "--sfSchema":     _SF_SCHEMA,
            "--input_path":   input_path,
            "--target_table": target_table,
            "--run_id":       run_id,
            "--load_type":    "merge",
            "--pk_columns":   pk_columns,
            "--src_cfg":        _SF_SRC_CFG_URI,
            "--pipeline":       "vip-connect",
        },
    )
