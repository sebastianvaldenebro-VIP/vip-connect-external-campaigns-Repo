from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Lead:
    """A lead sourced from Redis wait_list."""

    id: str
    phone: str
    first_name: str
    last_name: str
    groups: str
    location: str
    campaign: str
    available: bool
    raw: dict[str, Any]

    @staticmethod
    def from_redis_item(item: dict[str, Any]) -> Lead | None:
        lead_id = str(item.get("id", "")).strip()
        if not lead_id or len(lead_id) < 8:
            return None

        phone = str(item.get("phone", "")).strip()
        if not phone:
            return None

        return Lead(
            id=lead_id,
            phone=phone,
            first_name=str(item.get("first_name", "")).strip(),
            last_name=str(item.get("last_name", "")).strip(),
            groups=str(item.get("groups", "")).strip(),
            location=str(item.get("location", "")).strip(),
            campaign=str(item.get("campaign", "")).strip(),
            available=bool(item.get("available", False)),
            raw=item,
        )

    def normalized_phone_e164(self, default_country: str = "+1") -> str:
        if self.phone.startswith("+"):
            return self.phone
        digits = "".join(c for c in self.phone if c.isdigit())
        return f"{default_country}{digits}" if digits else ""

    def field(self, name: str) -> Any:
        return self.raw.get(name)
