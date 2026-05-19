from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from ..entities.lead import Lead


class LeadSource(ABC):
    @abstractmethod
    def iter_leads(self) -> Iterator[Lead]:
        ...
