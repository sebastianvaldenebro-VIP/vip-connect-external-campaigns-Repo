from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True, slots=True)
class DialRequest:
    client_token: str
    phone_number: str
    expiration_iso: str
    attributes: dict[str, str]
    lead_id: str

    @staticmethod
    def build(
        lead_id: str,
        phone_e164: str,
        expiration_minutes: int,
        attributes: dict[str, str],
        now: datetime | None = None,
    ) -> DialRequest:
        clock = now or datetime.now(tz=timezone.utc)
        expires = clock + timedelta(minutes=expiration_minutes)
        return DialRequest(
            client_token=str(uuid.uuid4()),
            phone_number=phone_e164,
            expiration_iso=expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
            attributes=attributes,
            lead_id=lead_id,
        )

    def to_api_payload(self) -> dict:
        return {
            "clientToken": self.client_token,
            "phoneNumber": self.phone_number,
            "expirationTime": self.expiration_iso,
            "attributes": self.attributes,
        }
