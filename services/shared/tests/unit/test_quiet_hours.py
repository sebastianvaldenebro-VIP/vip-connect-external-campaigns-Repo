"""Tests for per-lead TCPA quiet-hours resolution.

Distinct from executor.py's _within_working_hours, which is a Colombia-time
call-center staffing gate. This module answers a different question: is it
legal to contact *this* patient right now, in *their* local time?
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from vip_shared.domain.services.quiet_hours import (
    is_within_quiet_hours,
    resolve_timezone,
)


def test_resolves_eastern_area_code():
    assert resolve_timezone("+12125551234") == "America/New_York"  # 212 = NYC


def test_resolves_pacific_area_code():
    assert resolve_timezone("+14155551234") == "America/Los_Angeles"  # 415 = SF


def test_unparseable_number_falls_back_to_default():
    assert resolve_timezone("not-a-number") == "America/New_York"


@pytest.mark.parametrize(
    "utc_hour,expected",
    [
        # 2026-06-15 is a MONDAY. In June, Eastern is EDT (UTC-4).
        (12, True),   # 08:00 Eastern Mon — window opens
        (13, True),   # 09:00 Eastern Mon
        (17, True),   # 13:00 Eastern Mon
        (23, True),   # 19:00 Eastern Mon
        (11, False),  # 07:00 Eastern Mon — too early
    ],
)
def test_eastern_number_gated_on_eastern_local_time(utc_hour, expected):
    now = datetime(2026, 6, 15, utc_hour, 0, tzinfo=timezone.utc)
    assert is_within_quiet_hours("+12125551234", now=now) is expected


@pytest.mark.parametrize(
    "utc,expected",
    [
        # These two straddle the 21:00 close on a Monday, and are the tests that
        # would have FAILED under the earlier 20:00 proposal — they pin the
        # decision to use the full statutory window.
        (datetime(2026, 6, 16, 0, 0, tzinfo=timezone.utc), True),   # 20:00 ET Mon
        (datetime(2026, 6, 16, 1, 0, tzinfo=timezone.utc), False),  # 21:00 ET Mon — closed
    ],
)
def test_window_closes_at_21_00_local_not_20_00(utc, expected):
    assert is_within_quiet_hours("+12125551234", now=utc) is expected


def test_saturday_is_allowed():
    """Mon-Sat, so Saturday is a contact day. 2026-06-13 is a Saturday."""
    now = datetime(2026, 6, 13, 17, 0, tzinfo=timezone.utc)  # 13:00 ET Sat
    assert is_within_quiet_hours("+12125551234", now=now) is True


def test_sunday_is_never_allowed():
    """No contact on Sunday, at any hour. 2026-06-14 is a Sunday."""
    for utc_hour in (13, 17, 23):
        now = datetime(2026, 6, 14, utc_hour, 0, tzinfo=timezone.utc)
        assert is_within_quiet_hours("+12125551234", now=now) is False, utc_hour


def test_day_of_week_is_evaluated_in_recipient_local_time_not_utc():
    """THE subtle one, and the reason day-of-week cannot be read off UTC.

    2026-06-15 03:00 UTC is a MONDAY in UTC. For a Pacific number (PDT, UTC-7)
    it is 2026-06-14 20:00 — SUNDAY 20:00, which is *inside* the 08:00-21:00
    hour window. The hour axis says yes; only the recipient-local day axis can
    refuse it. A UTC-based day check would text this lead on a Sunday evening.
    """
    utc_monday = datetime(2026, 6, 15, 3, 0, tzinfo=timezone.utc)
    assert is_within_quiet_hours("+14155551234", now=utc_monday) is False


def test_same_instant_differs_by_recipient_timezone():
    """The whole point: one instant, two recipients, two answers.
    13:00 UTC is 09:00 Eastern (allowed) but 06:00 Pacific (blocked)."""
    now = datetime(2026, 6, 15, 13, 0, tzinfo=timezone.utc)
    assert is_within_quiet_hours("+12125551234", now=now) is True
    assert is_within_quiet_hours("+14155551234", now=now) is False


def test_multi_zone_number_fails_closed(monkeypatch):
    """A number mapping to several zones must be inside the window in EVERY
    candidate zone before we send — fail closed, not open.

    Ambiguity is injected rather than hunted for in a real area code: which
    NANP prefixes phonenumbers reports as multi-zone is library-data dependent
    and would make this test drift with a dependency bump. The instant chosen
    (13:00 UTC Monday) is 09:00 Eastern — inside the window — and 06:00 Pacific
    — outside it. Two candidate zones, two answers, so the fail-closed rule is
    the only thing that can produce False.
    """
    import vip_shared.domain.services.quiet_hours as qh

    monkeypatch.setattr(
        qh,
        "_candidate_zones",
        lambda _phone: ["America/New_York", "America/Los_Angeles"],
    )
    now = datetime(2026, 6, 15, 13, 0, tzinfo=timezone.utc)  # 09:00 ET / 06:00 PT
    assert qh.is_within_quiet_hours("+15555551234", now=now) is False
