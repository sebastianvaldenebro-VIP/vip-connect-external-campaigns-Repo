from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .filter_rule import FilterRule


@dataclass(frozen=True, slots=True)
class AllowedHour:
    day_of_week: int
    start_hour: int
    end_hour: int


@dataclass(frozen=True, slots=True)
class CommLimits:
    per_day: int = 0
    per_week: int = 0
    per_month: int = 0


@dataclass(frozen=True, slots=True)
class EntryLimits:
    max_entries: int = 0
    min_interval_hours: int = 0


@dataclass(frozen=True, slots=True)
class CampaignConfig:
    campaign_id: str
    name: str
    connect_campaign_id: str
    enabled: bool
    filters: tuple[FilterRule, ...]
    timezone: str
    allowed_hours: tuple[AllowedHour, ...]
    schedule_start_at: str | None
    schedule_end_at: str | None
    comm_limits: CommLimits
    entry_limits: EntryLimits
    dial_expiration_minutes: int
    raw: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_ddb_item(item: dict[str, Any]) -> CampaignConfig:
        filter_map = item.get("filters", {}) or {}
        filter_rules = tuple(FilterRule.from_dict(k, v) for k, v in filter_map.items())

        hours_raw = item.get("allowed_hours", []) or []
        hours = tuple(
            AllowedHour(
                day_of_week=int(h["day_of_week"]),
                start_hour=int(h["start_hour"]),
                end_hour=int(h["end_hour"]),
            )
            for h in hours_raw
        )

        comm_raw = item.get("comm_limits", {}) or {}
        comm = CommLimits(
            per_day=int(comm_raw.get("per_day", 0)),
            per_week=int(comm_raw.get("per_week", 0)),
            per_month=int(comm_raw.get("per_month", 0)),
        )

        entry_raw = item.get("entry_limits", {}) or {}
        entry = EntryLimits(
            max_entries=int(entry_raw.get("max_entries", 0)),
            min_interval_hours=int(entry_raw.get("min_interval_hours", 0)),
        )

        schedule = item.get("schedule", {}) or {}

        return CampaignConfig(
            campaign_id=str(item["campaign_id"]),
            name=str(item.get("name", "")),
            connect_campaign_id=str(item["connect_campaign_id"]),
            enabled=bool(item.get("enabled", False)),
            filters=filter_rules,
            timezone=str(item.get("timezone", "America/New_York")),
            allowed_hours=hours,
            schedule_start_at=schedule.get("start_at"),
            schedule_end_at=schedule.get("end_at"),
            comm_limits=comm,
            entry_limits=entry,
            dial_expiration_minutes=int(item.get("dial_expiration_minutes", 30)),
            raw=item,
        )
