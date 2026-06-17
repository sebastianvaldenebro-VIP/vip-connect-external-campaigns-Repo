"""Wrapper for StartOutboundVoiceContact.

Hardcodes the AMD contact flow (3d24320b-c1e3-40f3-90a2-b6867ef70c85) and
TrafficType=GENERAL (AMD detection happens inside the contact flow, not via
EnableAnswerMachineDetection=true, so no CAMPAIGN quota increase is needed).

Throttle limit: 2 RPS / 5 burst shared per account+region. Lambda 2 reserved
concurrency is set to 2 in the CDK stack to stay within this limit.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


@dataclass
class DialResult:
    success: bool
    contact_id: str | None = None
    error_code: str | None = None


class ConnectCaller:
    def __init__(
        self,
        *,
        instance_id: str,
        contact_flow_id: str,
        boto_client=None,
    ) -> None:
        self._instance_id = instance_id
        self._contact_flow_id = contact_flow_id
        self._client = boto_client or boto3.client("connect")

    def dial(
        self,
        *,
        destination_phone: str,
        queue_id: str,
        source_phone: str | None = None,
        attributes: dict | None = None,
        client_token: str | None = None,
    ) -> DialResult:
        """Initiate an outbound call. Returns DialResult; never raises.

        client_token: idempotency key (use contactSk) — Connect deduplicates calls with
        the same token within ~7 minutes, preventing double-dials on SQS redelivery.
        HIPAA: destination_phone is PHI and is NOT logged here.
        """
        kwargs: dict = {
            "InstanceId": self._instance_id,
            "ContactFlowId": self._contact_flow_id,
            "DestinationPhoneNumber": destination_phone,
            "QueueId": queue_id,
            "TrafficType": "GENERAL",
        }
        if source_phone:
            kwargs["SourcePhoneNumber"] = source_phone
        if attributes:
            kwargs["Attributes"] = attributes
        if client_token:
            kwargs["ClientToken"] = client_token

        try:
            resp = self._client.start_outbound_voice_contact(**kwargs)
            logger.info("StartOutboundVoiceContact success contact_id=%s", resp["ContactId"])
            return DialResult(success=True, contact_id=resp["ContactId"])
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            logger.warning("StartOutboundVoiceContact failed error_code=%s", code)
            return DialResult(success=False, error_code=code)
