"""Per-recipient TCPA quiet-hours gate for the bulk SMS pipeline.

WHY THIS EXISTS SEPARATELY FROM executor.py's _within_working_hours:

  executor.py's _within_working_hours / _is_working_day / _now_cot_hhmm answer
  "is the Bogota call-center staffed right now?" — a fixed UTC-5, no-DST
  question about *our* operating hours. That is correct for what it does and is
  not being replaced.

  This module answers a different question: "is it legal to text *this patient*
  right now, in *their* local time?" TCPA quiet hours are a property of the
  recipient's location, not ours. Conflating the two is how a plan that fires at
  08:00 COT texts a Pacific-timezone lead at 06:00 local.

  Pipeline A (deliveryType 'campaign'/'journey') gets the equivalent for free
  from Connect Campaigns V2's own localTimeZoneDetection=AREA_CODE + openHours.
  Pipeline B (deliveryType 'sms') never touches Connect Campaigns, so it needs
  this.

Fails closed: an unresolvable or multi-zone number is blocked unless it is
inside the window in every candidate zone.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import phonenumbers
from phonenumbers.timezone import time_zones_for_number

_DEFAULT_TZ = os.environ.get("QUIET_HOURS_DEFAULT_TZ", "America/New_York")
_START_HHMM = os.environ.get("QUIET_HOURS_START", "08:00")
_END_HHMM = os.environ.get("QUIET_HOURS_END", "21:00")

# Permitted contact days, as Python weekday() integers (Monday=0 .. Sunday=6).
# Default is Monday-Saturday: TCPA itself does not exempt Sunday, so excluding it
# is a VIP business choice, not a statutory requirement. Kept as an env var so it
# can be widened to include Sunday without a code change.
_DAYS_ENV = os.environ.get("QUIET_HOURS_DAYS", "0,1,2,3,4,5")
_ALLOWED_WEEKDAYS = frozenset(int(d) for d in _DAYS_ENV.split(",") if d.strip())

# phonenumbers returns this sentinel when it cannot map a number.
_UNKNOWN_TZ = "Etc/Unknown"


def _minutes(hhmm: str) -> int:
    hours, mins = (int(part) for part in hhmm.split(":"))
    return hours * 60 + mins


def _candidate_zones(phone: str) -> list[str]:
    try:
        parsed = phonenumbers.parse(phone, "US")
        zones = [z for z in time_zones_for_number(parsed) if z != _UNKNOWN_TZ]
    except Exception:
        return [_DEFAULT_TZ]
    return zones or [_DEFAULT_TZ]


def resolve_timezone(phone: str) -> str:
    """Return the single best IANA timezone for `phone`, or the default.

    Never raises — an unparseable number yields the default so the caller's
    window check still runs rather than the whole batch failing.
    """
    return _candidate_zones(phone)[0]


def is_within_quiet_hours(phone: str, *, now: datetime | None = None) -> bool:
    """True if `phone` may be contacted right now in its own local time.

    Two independent axes, both evaluated in the RECIPIENT's timezone:
      1. hour-of-day inside [_START_HHMM, _END_HHMM)
      2. day-of-week in _ALLOWED_WEEKDAYS (Mon-Sat by default, no Sunday)

    Evaluating the day in the recipient's zone rather than UTC is load-bearing:
    2026-06-15 03:00 UTC is Monday in UTC but Sunday 20:00 Pacific -- inside
    the hour window, so only the day axis can refuse it.

    Fails closed on ambiguity: a number mapping to several timezones must pass
    BOTH checks in ALL of them.
    """
    instant = now or datetime.now(timezone.utc)
    start, end = _minutes(_START_HHMM), _minutes(_END_HHMM)
    for zone in _candidate_zones(phone):
        try:
            local = instant.astimezone(ZoneInfo(zone))
        except Exception:
            return False
        if local.weekday() not in _ALLOWED_WEEKDAYS:
            return False
        if not (start <= local.hour * 60 + local.minute < end):
            return False
    return True
