"""Lambda B — deny list write.

Invoked from *DenyListTransferFlow when an agent activates the
"Block Number" Quick Connect during an active call.

Writes the caller's number to DynamoDB so future calls from that
number are blocked by check_handler.

Connect flow usage (Queue Transfer Flow):
  Invoke Lambda → write_handler
  $.External.statusCode == "200" → MessageParticipant "Number blocked" → EndFlow
  else                            → MessageParticipant "Error" → EndFlow
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone

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
    agent_arn = (contact_data.get("AgentInfo") or {}).get("AgentARN", "unknown")
    contact_id = contact_data.get("ContactId", "unknown")

    if not caller:
        logger.warning(
            "deny_list_write: no CustomerEndpoint.Address in event contactId=%s",
            contact_id,
        )
        return {"statusCode": "400", "body": "Missing caller number"}

    try:
        _table().put_item(
            Item={
                "ContactNumber": caller,
                "addedAt": datetime.now(timezone.utc).isoformat(),
                "addedBy": agent_arn,
                "contactId": contact_id,
            }
        )
        logger.info(
            "deny_list_write: blocked number_hash=%s by=%s contactId=%s",
            _hash_phone(caller),
            agent_arn,
            contact_id,
        )
        return {"statusCode": "200", "body": "Number blocked successfully"}
    except ClientError as exc:
        logger.error("deny_list_write: DynamoDB error number_hash=%s: %s", _hash_phone(caller), exc)
        return {"statusCode": "500", "body": "Internal error"}
