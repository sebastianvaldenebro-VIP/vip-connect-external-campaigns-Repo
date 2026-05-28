from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3

AUDIT_RETENTION_DAYS = 365 * 6  # 6 years HIPAA requirement


class AuditRecorder:
    """Writes immutable audit rows to the AdminAuditLog DynamoDB table.

    Every mutation in an API Lambda should call `record(...)` exactly once
    within the same request. The Lambda role must have `dynamodb:PutItem`
    on the AdminAuditLog table (and only that action — no update or delete).
    """

    def __init__(self, table_name: str, dynamodb_resource=None) -> None:
        self._table = (dynamodb_resource or boto3.resource("dynamodb")).Table(
            table_name
        )

    def record(
        self,
        *,
        entity_type: str,
        entity_id: str,
        action: str,
        actor_sub: str,
        actor_email: str,
        before: Any = None,
        after: Any = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        now = datetime.now(tz=timezone.utc)
        ttl = int((now + timedelta(days=AUDIT_RETENTION_DAYS)).timestamp())

        item: dict[str, Any] = {
            "entity_id": f"{entity_type}/{entity_id}",
            "timestamp": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "entity_type": entity_type,
            "resource_id": entity_id,
            "action": action,
            "actor_sub": actor_sub,
            "actor_email": actor_email,
            "ttl": ttl,
        }
        if before is not None:
            item["before"] = json.dumps(before, default=str)
        if after is not None:
            item["after"] = json.dumps(after, default=str)
        if ip_address:
            item["ip_address"] = ip_address
        if user_agent:
            item["user_agent"] = user_agent
        if extra:
            item["extra"] = json.dumps(extra, default=str)

        self._table.put_item(Item=item)


def build_from_env() -> AuditRecorder:
    table_name = os.environ["AUDIT_TABLE"]
    return AuditRecorder(table_name=table_name)
