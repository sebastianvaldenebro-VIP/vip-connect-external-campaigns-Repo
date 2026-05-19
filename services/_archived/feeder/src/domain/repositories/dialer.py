from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass

from ..entities.dial_request import DialRequest


@dataclass(frozen=True, slots=True)
class DialResult:
    successful: list[dict]
    failed: list[dict]

    @property
    def success_count(self) -> int:
        return len(self.successful)

    @property
    def failure_count(self) -> int:
        return len(self.failed)


class CampaignDialer(ABC):
    @abstractmethod
    def put_dial_batch(self, campaign_id: str, requests: Sequence[DialRequest]) -> DialResult:
        ...
