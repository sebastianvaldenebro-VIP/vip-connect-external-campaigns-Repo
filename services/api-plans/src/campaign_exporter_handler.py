"""Lambda handler for daily data exports.

Supported actions (set in EventBridge Input):
  {"action": "campaign_export"}  — CAMPAIGN_MAPPING (Connect V2 campaigns → Snowflake)
  {"action": "branded_export"}   — BRANDED_CAMPAIGN_RUNS + BRANDED_CAMPAIGN_METRICS → Snowflake

Separated from api-plans so the heavy awswrangler/Pandas layer does not
add cold-start overhead to the latency-sensitive HTTP API function.
"""

from __future__ import annotations

from vip_shared.infrastructure.telemetry.structured_logger import StructuredLogger
import exporter

_logger = StructuredLogger(service="campaign-exporter")


def lambda_handler(event: dict, context) -> dict:
    action = event.get("action", "campaign_export")

    if action == "branded_export":
        import branded_exporter
        _logger.info("branded_export_started")
        try:
            runs_result = branded_exporter.export_branded_runs()
            metrics_result = branded_exporter.export_branded_metrics()
            result = {"runs": runs_result, "metrics": metrics_result}
            _logger.info("branded_export_done", **{
                "runs_exported": runs_result.get("exported", 0),
                "metrics_exported": metrics_result.get("exported", 0),
            })
            return {"ok": True, **result}
        except Exception as exc:
            # Re-raise (don't swallow into {"ok": False}) so this Lambda's AWS/Lambda
            # Errors metric fires — the standard alarm every other function in this
            # repo relies on. Swallowing here left 14+ days of 100% export failures
            # with zero signal, since nothing else watches this handler's return value.
            _logger.error("branded_export_error", error=str(exc))
            raise

    if action == "sms_export":
        import sms_exporter
        _logger.info("sms_export_started")
        try:
            result = sms_exporter.export_sms_runs()
            _logger.info("sms_export_done", **{
                "exported": result.get("exported", 0),
                "table": result.get("table", "SMS_CAMPAIGN_RUNS"),
            })
            return {"ok": True, **result}
        except Exception as exc:
            _logger.error("sms_export_error", error=str(exc))
            raise

    # Default: campaign_export
    _logger.info("campaign_export_started")
    try:
        result = exporter.export_campaign_mappings()
        _logger.info("campaign_export_done", **result)
        return {"ok": True, **result}
    except Exception as exc:
        _logger.error("campaign_export_error", error=str(exc))
        raise
