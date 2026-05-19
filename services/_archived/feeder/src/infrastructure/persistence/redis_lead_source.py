from __future__ import annotations

import json
from collections.abc import Iterator

from ...domain.entities.lead import Lead
from ...domain.repositories.lead_source import LeadSource

REDIS_SCAN_CHUNK = 5000


class RedisLeadSource(LeadSource):
    def __init__(self, redis_client, team: str) -> None:
        self._redis = redis_client
        self._list_key = f"wait_list:{team}:list"

    def iter_leads(self) -> Iterator[Lead]:
        total = self._redis.llen(self._list_key)
        for start in range(0, total, REDIS_SCAN_CHUNK):
            end = start + REDIS_SCAN_CHUNK - 1
            items = self._redis.lrange(self._list_key, start, end)
            for raw in items:
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                lead = Lead.from_redis_item(parsed)
                if lead is not None:
                    yield lead
