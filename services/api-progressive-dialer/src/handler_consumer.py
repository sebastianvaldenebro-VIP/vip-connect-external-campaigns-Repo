# services/api-progressive-dialer/src/handler_consumer.py
"""Kinesis Agent Event Stream consumer — dispatches contacts to branded progressive dialer."""
from __future__ import annotations
import base64, json, logging, os

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event: dict, _context) -> dict:
    records = event.get("Records", [])
    processed = 0
    for record in records:
        raw = base64.b64decode(record["kinesis"]["data"]).decode("utf-8")
        agent_event = json.loads(raw)
        logger.info("Received event type=%s", agent_event.get("EventType"))
        processed += 1
    return {"processed": processed}
