from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from ..entities.campaign_config import CampaignConfig


class CommLimitsEvaluator:
    """Enforces per-recipient communication limits using historical dial counts."""

    @staticmethod
    def exceeded(
        campaign: CampaignConfig,
        past_push_timestamps_iso: Iterable[str],
        now_utc: datetime | None = None,
    ) -> bool:
        if campaign.comm_limits.per_day == campaign.comm_limits.per_week == campaign.comm_limits.per_month == 0:
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

        if campaign.comm_limits.per_day and day_count >= campaign.comm_limits.per_day:
            return True
        if campaign.comm_limits.per_week and week_count >= campaign.comm_limits.per_week:
            return True
        return bool(
            campaign.comm_limits.per_month and month_count >= campaign.comm_limits.per_month
        )
