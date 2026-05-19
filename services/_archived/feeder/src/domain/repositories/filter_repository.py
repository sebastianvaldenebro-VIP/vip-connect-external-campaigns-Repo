from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable

from ..entities.campaign_config import CampaignConfig


class FilterRepository(ABC):
    @abstractmethod
    def list_enabled(self) -> Iterable[CampaignConfig]:
        ...
