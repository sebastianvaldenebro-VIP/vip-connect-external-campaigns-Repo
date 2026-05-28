from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from vip_shared.domain.entities.schedule_spec import ScheduleSpec


class ScheduleEvaluator:
    """Decides whether a scheduled activity is active at a given UTC instant."""

    def is_within_window(self, spec: ScheduleSpec, now_utc: datetime) -> bool:
        if not spec.enabled:
            return False

        if not self._within_schedule(spec, now_utc):
            return False

        if spec.allowed_hours:
            local = now_utc.astimezone(ZoneInfo(spec.timezone))
            if not self._matches_allowed_hour(spec, local):
                return False

        return True

    @staticmethod
    def _within_schedule(spec: ScheduleSpec, now_utc: datetime) -> bool:
        if spec.schedule_start_at:
            start = datetime.fromisoformat(
                spec.schedule_start_at.replace("Z", "+00:00")
            )
            if now_utc < start:
                return False
        if spec.schedule_end_at:
            end = datetime.fromisoformat(spec.schedule_end_at.replace("Z", "+00:00"))
            if now_utc >= end:
                return False
        return True

    @staticmethod
    def _matches_allowed_hour(spec: ScheduleSpec, local: datetime) -> bool:
        day_of_week = local.weekday()
        hour = local.hour
        return any(
            h.day_of_week == day_of_week and h.start_hour <= hour < h.end_hour
            for h in spec.allowed_hours
        )
