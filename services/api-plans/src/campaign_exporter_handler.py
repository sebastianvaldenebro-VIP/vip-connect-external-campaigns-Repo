"""Lambda handler for the daily campaign mapping export.

Triggered by EventBridge daily rule: {"action": "campaign_export"}

Separated from api-plans so the heavy awswrangler/Pandas layer does not
add cold-start overhead to the latency-sensitive HTTP API function.
"""
from __future__ import annotations

from vip_shared.infrastructure.telemetry.structured_logger import StructuredLogger
import exporter

_logger = StructuredLogger(service="campaign-exporter")


def lambda_handler(event: dict, context) -> dict:
    _logger.info("campaign_export_started")
    try:
        result = exporter.export_campaign_mappings()
        _logger.info("campaign_export_done", **result)
        return {"ok": True, **result}
    except Exception as exc:
        _logger.error("campaign_export_error", error=str(exc))
        return {"ok": False, "error": str(exc)}
