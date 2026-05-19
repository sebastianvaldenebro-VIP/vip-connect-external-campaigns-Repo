from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from ..entities.campaign_config import CampaignConfig


class ScheduleEvaluator:
    """Decides whether a campaign is active at a given UTC instant."""

    def is_within_window(self, campaign: CampaignConfig, now_utc: datetime) -> bool:
        if not campaign.enabled:
            return False

        if not self._within_schedule(campaign, now_utc):
            return False

        if campaign.allowed_hours:
            local = now_utc.astimezone(ZoneInfo(campaign.timezone))
            if not self._matches_allowed_hour(campaign, local):
                return False

        return True

    @staticmethod
    def _within_schedule(campaign: CampaignConfig, now_utc: datetime) -> bool:
        if campaign.schedule_start_at:
            start = datetime.fromisoformat(campaign.schedule_start_at.replace("Z", "+00:00"))
            if now_utc < start:
                return False
        if campaign.schedule_end_at:
            end = datetime.fromisoformat(campaign.schedule_end_at.replace("Z", "+00:00"))
            if now_utc >= end:
                return False
        return True

    @staticmethod
    def _matches_allowed_hour(campaign: CampaignConfig, local: datetime) -> bool:
        day_of_week = local.weekday()
        hour = local.hour
        return any(
            h.day_of_week == day_of_week and h.start_hour <= hour < h.end_hour
            for h in campaign.allowed_hours
        )
