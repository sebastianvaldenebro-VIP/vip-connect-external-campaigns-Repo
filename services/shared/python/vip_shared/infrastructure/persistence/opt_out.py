"""Shared cross-channel Do-Not-Contact store for automated outreach.

Backed by VipConnectOptOutList (PK: ContactNumber) — a table dedicated to
automated, cross-channel opt-out (STOP/QUIT/UNSUBSCRIBE, and any future
Medwork-driven DNC sync). Deliberately separate from vip-connect-deny-list,
which stays scoped to its original meaning: voice-only, agent-manual
"Block Number" during an active call.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import boto3


class OptOutRepository:
    """Read/write access to the shared cross-channel opt-out list."""

    def __init__(self, table_name: str, dynamodb_resource=None) -> None:
        self._table = (dynamodb_resource or boto3.resource("dynamodb")).Table(
            table_name
        )

    def is_blocked(self, phone: str) -> bool:
        """Return True if `phone` (E.164) has opted out."""
        response = self._table.get_item(Key={"ContactNumber": phone})
        return "Item" in response

    def block(self, phone: str, *, reason: str, source: str) -> None:
        """Record `phone` as opted out. Overwrites if already present (idempotent)."""
        self._table.put_item(
            Item={
                "ContactNumber": phone,
                "reason": reason,
                "source": source,
                "addedAt": datetime.now(timezone.utc).isoformat(),
            }
        )


def build_from_env() -> OptOutRepository:
    return OptOutRepository(table_name=os.environ["OPT_OUT_TABLE"])
