from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AllowedHour:
    day_of_week: int  # 0=Monday, 6=Sunday
    start_hour: int
    end_hour: int


@dataclass(frozen=True, slots=True)
class ScheduleSpec:
    """Platform-agnostic schedule description used by ScheduleEvaluator."""

    enabled: bool
    timezone: str
    schedule_start_at: str | None = None  # ISO 8601 UTC
    schedule_end_at: str | None = None    # ISO 8601 UTC
    allowed_hours: tuple[AllowedHour, ...] = ()
