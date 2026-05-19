from __future__ import annotations

from collections.abc import Iterable


class MetricsPublisher:
    def __init__(self, cloudwatch_client, namespace: str) -> None:
        self._client = cloudwatch_client
        self._namespace = namespace
        self._buffer: list[dict] = []

    def add(self, name: str, value: float, campaign_id: str, unit: str = "Count") -> None:
        self._buffer.append(
            {
                "MetricName": name,
                "Value": float(value),
                "Unit": unit,
                "Dimensions": [{"Name": "CampaignId", "Value": campaign_id}],
            }
        )

    def flush(self) -> None:
        if not self._buffer:
            return
        for chunk in self._chunks(self._buffer, size=20):
            self._client.put_metric_data(Namespace=self._namespace, MetricData=chunk)
        self._buffer.clear()

    @staticmethod
    def _chunks(items: Iterable[dict], size: int) -> Iterable[list[dict]]:
        buf: list[dict] = []
        for item in items:
            buf.append(item)
            if len(buf) >= size:
                yield buf
                buf = []
        if buf:
            yield buf
