from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from vip_shared.domain.entities.comm_limits import CommLimits


class CommLimitsEvaluator:
    """Enforces per-recipient communication limits using historical dial counts."""

    @staticmethod
    def exceeded(
        limits: CommLimits,
        past_push_timestamps_iso: Iterable[str],
        now_utc: datetime | None = None,
    ) -> bool:
        if limits.per_day == limits.per_week == limits.per_month == 0:
            return False

        now = now_utc or datetime.now(tz=timezone.utc)
        one_day = now - timedelta(days=1)
        one_week = now - timedelta(days=7)
        one_month = now - timedelta(days=30)

        day_count = week_count = month_count = 0
        for ts_iso in past_push_timestamps_iso:
            try:
                ts = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
            except ValueError:
                continue
            if ts >= one_month:
                month_count += 1
            if ts >= one_week:
                week_count += 1
            if ts >= one_day:
                day_count += 1

        if limits.per_day and day_count >= limits.per_day:
            return True
        if limits.per_week and week_count >= limits.per_week:
            return True
        return bool(limits.per_month and month_count >= limits.per_month)
