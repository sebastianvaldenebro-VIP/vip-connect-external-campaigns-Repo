from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable


class TrackingRepository(ABC):
    @abstractmethod
    def list_push_timestamps(self, lead_id: str, campaign_id: str) -> list[str]:
        ...

    @abstractmethod
    def is_in_flight(self, lead_id: str, campaign_id: str, now_iso: str) -> bool:
        ...

    @abstractmethod
    def record_pushes(self, items: Iterable[dict]) -> None:
        ...
