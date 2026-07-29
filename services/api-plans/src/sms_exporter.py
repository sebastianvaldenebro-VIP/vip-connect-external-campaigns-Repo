"""Export VipSmsCampaignRuns to S3 Parquet → Snowflake.

PHI rule: phone numbers are NOT in VipSmsCampaignRuns — they live exclusively
in VipSmsCampaignQueue (TTL-based, transient). This table contains only
campaign-level aggregates and metadata. Safe to export as-is.

Target: PRD_RAW_DB.AMAZON_CONNECT.SMS_CAMPAIGN_RUNS
PK for merge: plan_id, sk
Bookmark column: extracted_at (set at export time, stamps the DDB item too)
"""
import os
from datetime import datetime, timezone

import boto3
import awswrangler as wr
import pandas as pd

_RUNS_TABLE     = os.environ.get("SMS_CAMPAIGN_RUNS_TABLE", "VipSmsCampaignRuns")
_DATA_EXPORT_BUCKET = os.environ["DATA_EXPORT_BUCKET"]
_GLUE_JOB_NAME  = os.environ["GLUE_JOB_NAME"]
_SF_SRC_CFG_URI = os.environ["SF_SRC_CFG_URI"]
_SF_DATABASE    = os.environ.get("SF_DATABASE", "PRD_RAW_DB")
_SF_SCHEMA      = os.environ.get("SF_SCHEMA", "AMAZON_CONNECT")
_SF_KEY_SECRET  = os.environ.get("SF_KEY_SECRET", "specialops-prod-snowflake-etl-ingestor")
_PIPELINE_VERSION = "v1"

_ddb  = boto3.resource("dynamodb")
_glue = boto3.client("glue")


def export_sms_runs() -> dict:
    """Scan VipSmsCampaignRuns, write Parquet to S3, trigger Glue Snowflake loader."""
    now = datetime.now(timezone.utc)
    extracted_at = now.isoformat()
    dt_label = now.strftime("%Y-%m-%d")

    items = _scan_table(_RUNS_TABLE)
    if not items:
        return {"exported": 0, "table": "SMS_CAMPAIGN_RUNS"}

    rows = []
    for item in items:
        rows.append({
            "plan_id":                  item.get("planId", ""),
            "sk":                       item.get("sk", ""),
            "sms_campaign_id":          item.get("smsCampaignId", ""),
            "plan_name":                item.get("planName", ""),
            "segment_name":             item.get("segmentName", ""),
            "segment_arn":              item.get("segmentArn", ""),
            "message_template":         item.get("messageTemplate", ""),
            "origination_number_arn":   item.get("originationNumberArn", ""),
            "origination_number":       item.get("originationNumber", ""),
            "total_enqueued":           int(item.get("totalEnqueued", 0) or 0),
            "total_sent":               int(item.get("totalSent", 0) or 0),
            "total_failed":             int(item.get("totalFailed", 0) or 0),
            "total_opted_out":          int(item.get("totalOptedOut", 0) or 0),
            "status":                   item.get("status", ""),
            "started_at":               item.get("startedAt", ""),
            "completed_at":             item.get("completedAt", ""),
            "exit_reason":              item.get("exitReason", ""),
            "pipeline_version":         item.get("pipelineVersion", _PIPELINE_VERSION),
            # Audit columns
            "created_at":               item.get("createdAt", ""),
            "updated_at":               item.get("updatedAt", ""),
            "extracted_at":             extracted_at,
        })

    df = pd.DataFrame(rows)
    for col in ("started_at", "completed_at", "created_at", "updated_at"):
        df[col] = pd.to_datetime(df[col], utc=True, errors="coerce")
    df["extracted_at"] = pd.to_datetime(df["extracted_at"], utc=True)

    s3_path = f"s3://{_DATA_EXPORT_BUCKET}/connect/raw/sms_campaign_runs/{dt_label}/"
    wr.s3.to_parquet(df, path=s3_path, mode="overwrite", dataset=True)
    _trigger_glue(
        target_table="SMS_CAMPAIGN_RUNS",
        pk_columns="plan_id,sk",
        input_path=f"s3://{_DATA_EXPORT_BUCKET}/connect/raw/sms_campaign_runs",
        run_id=dt_label,
    )
    return {"exported": len(rows), "table": "SMS_CAMPAIGN_RUNS", "s3_path": s3_path}


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
            "--sfDatabase":    _SF_DATABASE,
            "--sfSchema":      _SF_SCHEMA,
            "--input_path":    input_path,
            "--target_table":  target_table,
            "--run_id":        run_id,
            "--load_type":     "merge",
            "--pk_columns":    pk_columns,
            "--src_cfg":       _SF_SRC_CFG_URI,
            "--pipeline":      "vip-connect",
            "--sf_key_secret": _SF_KEY_SECRET,
        },
    )
