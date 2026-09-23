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
        """Return True if `phone` (E.164) has opted out.

        Uses ConsistentRead=True (VIP-02): this table is checked at both
        enqueue time (sms_sender_handler.py) and again immediately before
        send (sms_processor_handler.py, progressive-dialer's handler_caller.py)
        specifically to catch a STOP recorded in the gap between those two
        checks. An eventually-consistent read here can serve stale data from
        a replica that hasn't yet applied a very recent `block()` write,
        defeating that exact last-mile check — this is a compliance-critical
        Do-Not-Contact gate, not a place to trade correctness for lower RCU cost.
        """
        response = self._table.get_item(
            Key={"ContactNumber": phone}, ConsistentRead=True
        )
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
