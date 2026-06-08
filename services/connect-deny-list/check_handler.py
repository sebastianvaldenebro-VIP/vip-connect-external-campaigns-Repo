"""Lambda A — deny list check.

Invoked from *MainInboundVoice at the start of every inbound call.
Returns {"blocked": "true"} if the caller's number is in DynamoDB,
{"blocked": "false"} otherwise.

Connect flow usage:
  Invoke Lambda → check_handler
  $.External.blocked == "true"  → play message + Disconnect
  else                           → continue normal flow
"""

from __future__ import annotations

import hashlib
import logging
import os

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

TABLE_NAME: str = os.environ.get("DENY_LIST_TABLE", "vip-connect-deny-list")

_dynamodb = None


def _hash_phone(phone: str) -> str:
    return hashlib.sha256(phone.encode()).hexdigest()[:12]


def _table():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb").Table(TABLE_NAME)
    return _dynamodb


def lambda_handler(event: dict, context) -> dict:
    contact_data = event.get("Details", {}).get("ContactData", {})
    caller = (contact_data.get("CustomerEndpoint") or {}).get("Address", "")

    if not caller:
        logger.warning("deny_list_check: no CustomerEndpoint.Address in event")
        return {"blocked": "false"}

    try:
        item = _table().get_item(Key={"ContactNumber": caller}).get("Item")
        if item:
            logger.info(
                "deny_list_check: blocked number_hash=%s reason=%s",
                _hash_phone(caller),
                item.get("reason", ""),
            )
            return {"blocked": "true", "reason": item.get("reason", "")}
    except ClientError as exc:
        logger.error("deny_list_check: DynamoDB error number_hash=%s: %s", _hash_phone(caller), exc)

    return {"blocked": "false"}
