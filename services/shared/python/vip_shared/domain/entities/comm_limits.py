from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CommLimits:
    """Per-recipient communication frequency limits."""

    per_day: int = 0
    per_week: int = 0
    per_month: int = 0
