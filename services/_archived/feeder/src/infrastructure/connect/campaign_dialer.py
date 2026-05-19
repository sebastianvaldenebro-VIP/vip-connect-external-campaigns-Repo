from __future__ import annotations

import time
from collections.abc import Sequence

from botocore.exceptions import ClientError

from ...domain.entities.dial_request import DialRequest
from ...domain.repositories.dialer import CampaignDialer, DialResult

BATCH_SIZE_CAP = 25
THROTTLE_RETRY_MAX = 3


class ConnectCampaignDialer(CampaignDialer):
    def __init__(self, campaigns_client) -> None:
        self._client = campaigns_client

    def put_dial_batch(self, campaign_id: str, requests: Sequence[DialRequest]) -> DialResult:
        if not requests:
            return DialResult(successful=[], failed=[])
        if len(requests) > BATCH_SIZE_CAP:
            raise ValueError(f"Batch size exceeds {BATCH_SIZE_CAP} (got {len(requests)})")

        payload = [r.to_api_payload() for r in requests]

        for attempt in range(THROTTLE_RETRY_MAX + 1):
            try:
                response = self._client.put_dial_request_batch(
                    id=campaign_id, dialRequests=payload
                )
                return DialResult(
                    successful=response.get("successfulRequests", []),
                    failed=response.get("failedRequests", []),
                )
            except ClientError as err:
                code = err.response.get("Error", {}).get("Code", "")
                if code in {"ThrottlingException", "RequestThrottled"} and attempt < THROTTLE_RETRY_MAX:
                    time.sleep((2**attempt) * 0.5)
                    continue
                raise

        return DialResult(successful=[], failed=[])
