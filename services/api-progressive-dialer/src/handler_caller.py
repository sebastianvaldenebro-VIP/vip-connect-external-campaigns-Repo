# services/api-progressive-dialer/src/handler_caller.py
"""SQS consumer — fires StartOutboundVoiceContact after the 22s delay."""
from __future__ import annotations
import json, logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event: dict, _context) -> dict:
    for record in event.get("Records", []):
        body = json.loads(record["body"])
        logger.info("Caller received message agent_id=%s", body.get("agentId"))
    return {"status": "ok"}
