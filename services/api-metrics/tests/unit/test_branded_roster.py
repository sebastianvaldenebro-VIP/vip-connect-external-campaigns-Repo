"""Tests for get_agent_roster — StatusType resolution, ACW freshness, Offline handling.

GetCurrentUserData's AgentStatusReference only carries StatusStartTimestamp,
StatusArn, StatusName — never StatusType. Reading status.get("StatusType", "CUSTOM")
silently defaults every agent to CUSTOM, collapsing "Available" and "Offline"
into the same "Unavailable" bucket the frontend renders as "Away" with an
"Extended break" alert.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    # handlers.branded reads CONNECT_INSTANCE_ID once at import time (module-level
    # constant, as it would in a real Lambda cold start). monkeypatch.setenv alone
    # only affects os.environ, not the already-bound module constant, so patch the
    # constant directly to keep this independent of import order across test files.
    from handlers import branded

    monkeypatch.setenv("CONNECT_INSTANCE_ID", "instance-1")
    monkeypatch.setattr(branded, "_CONNECT_INSTANCE_ID", "instance-1")
    branded._rp_name_cache.clear()
    branded._user_name_cache.clear()
    branded._status_type_cache.clear()
    yield
    branded._rp_name_cache.clear()
    branded._user_name_cache.clear()
    branded._status_type_cache.clear()


def _user_data(
    status_arn="arn:aws:connect:us-east-1:1:instance/i/agent-status/s-available",
    status_name="Available",
    status_start="2026-08-11T10:00:00+00:00",
    contacts=None,
    user_id="u-1",
    rp_id="rp-1",
):
    return {
        "User": {"Id": user_id},
        "RoutingProfile": {"Id": rp_id},
        "Status": {
            "StatusArn": status_arn,
            "StatusName": status_name,
            "StatusStartTimestamp": status_start,
        },
        "Contacts": contacts or [],
    }


def _mock_connect(user_data_list, agent_statuses):
    mock = MagicMock()
    mock.get_current_user_data.return_value = {"UserDataList": user_data_list}
    paginator = MagicMock()
    paginator.paginate.return_value = [{"RoutingProfileSummaryList": [{"Id": "rp-1", "Name": "RP 1"}]}]
    mock.get_paginator.return_value = paginator
    mock.list_agent_statuses.return_value = {"AgentStatusSummaryList": agent_statuses}
    mock.describe_user.return_value = {"User": {"IdentityInfo": {"FirstName": "A", "LastName": "B"}, "Username": "ab"}}
    return mock


class TestStatusTypeResolution:
    """StatusType must come from ListAgentStatuses (keyed by StatusArn), not from
    a field GetCurrentUserData never returns.
    """

    def test_routable_status_arn_yields_available(self):
        from handlers import branded

        ud = [_user_data(status_arn="arn:.../agent-status/s-avail", status_name="Available")]
        statuses = [{"Id": "s-avail", "Name": "Available", "Type": "ROUTABLE"}]

        with patch("handlers.branded._connect", _mock_connect(ud, statuses)):
            resp = branded.get_agent_roster({"queryStringParameters": {"queueId": "q-1"}}, {})

        body = json.loads(resp["body"])
        assert body["agents"][0]["effectiveStatus"] == "Available"

    def test_offline_status_arn_yields_offline_not_unavailable(self):
        """An agent who logged out must not be reported as 'Unavailable' — that
        bucket feeds the frontend's 'Extended break' alert.
        """
        from handlers import branded

        ud = [_user_data(status_arn="arn:.../agent-status/s-off", status_name="Offline")]
        statuses = [{"Id": "s-off", "Name": "Offline", "Type": "OFFLINE"}]

        with patch("handlers.branded._connect", _mock_connect(ud, statuses)):
            resp = branded.get_agent_roster({"queryStringParameters": {"queueId": "q-1"}}, {})

        body = json.loads(resp["body"])
        assert body["agents"][0]["effectiveStatus"] == "Offline"

    def test_custom_status_arn_yields_unavailable(self):
        from handlers import branded

        ud = [_user_data(status_arn="arn:.../agent-status/s-break", status_name="On a Break")]
        statuses = [{"Id": "s-break", "Name": "On a Break", "Type": "CUSTOM"}]

        with patch("handlers.branded._connect", _mock_connect(ud, statuses)):
            resp = branded.get_agent_roster({"queryStringParameters": {"queueId": "q-1"}}, {})

        body = json.loads(resp["body"])
        assert body["agents"][0]["effectiveStatus"] == "Unavailable"

    def test_unknown_status_arn_defaults_to_unavailable(self):
        """If ListAgentStatuses lookup misses (e.g. status deleted after caching),
        fail safe to Unavailable rather than crashing.
        """
        from handlers import branded

        ud = [_user_data(status_arn="arn:.../agent-status/s-missing", status_name="Mystery")]
        statuses = []  # nothing registered

        with patch("handlers.branded._connect", _mock_connect(ud, statuses)):
            resp = branded.get_agent_roster({"queryStringParameters": {"queueId": "q-1"}}, {})

        body = json.loads(resp["body"])
        assert body["agents"][0]["effectiveStatus"] == "Unavailable"


class TestAcwFreshness:
    """A contact in state ENDED must only count as active ACW while recent —
    stale ENDED contacts left in the roster produce multi-day fake ACW timers.
    """

    def test_recent_ended_contact_counts_as_acw(self):
        from handlers import branded

        ud = [
            _user_data(
                status_arn="arn:.../agent-status/s-avail",
                status_name="Available",
                contacts=[{"AgentContactState": "ENDED", "StateStartTimestamp": "2026-08-11T09:58:00+00:00"}],
            )
        ]
        statuses = [{"Id": "s-avail", "Name": "Available", "Type": "ROUTABLE"}]

        with patch("handlers.branded._connect", _mock_connect(ud, statuses)), \
             patch("handlers.branded._now", return_value=__import__("datetime").datetime(2026, 8, 11, 10, 0, 0, tzinfo=__import__("datetime").timezone.utc)):
            resp = branded.get_agent_roster({"queryStringParameters": {"queueId": "q-1"}}, {})

        body = json.loads(resp["body"])
        assert body["agents"][0]["effectiveStatus"] == "ACW"

    def test_stale_ended_contact_does_not_count_as_acw(self):
        """A contact that ENDED hours ago must not keep the agent pinned in ACW —
        the underlying Connect status (here Available) should win instead.
        """
        from handlers import branded
        import datetime as dt

        ud = [
            _user_data(
                status_arn="arn:.../agent-status/s-avail",
                status_name="Available",
                contacts=[{"AgentContactState": "ENDED", "StateStartTimestamp": "2026-08-06T14:59:01+00:00"}],
            )
        ]
        statuses = [{"Id": "s-avail", "Name": "Available", "Type": "ROUTABLE"}]

        with patch("handlers.branded._connect", _mock_connect(ud, statuses)), \
             patch("handlers.branded._now", return_value=dt.datetime(2026, 8, 11, 10, 0, 0, tzinfo=dt.timezone.utc)):
            resp = branded.get_agent_roster({"queryStringParameters": {"queueId": "q-1"}}, {})

        body = json.loads(resp["body"])
        assert body["agents"][0]["effectiveStatus"] != "ACW"
        assert body["agents"][0]["effectiveStatus"] == "Available"


class TestIntentionalAbsenceFlag:
    """Out of the Office / Vacation are planned absences, not breaks — the
    roster must flag them distinctly so the frontend doesn't fire the
    'Extended break' alert against a multi-day intentional status.
    """

    def test_out_of_the_office_is_flagged_intentional(self):
        from handlers import branded

        ud = [_user_data(status_arn="arn:.../agent-status/s-ooo", status_name="Out of the Office")]
        statuses = [{"Id": "s-ooo", "Name": "Out of the Office", "Type": "CUSTOM"}]

        with patch("handlers.branded._connect", _mock_connect(ud, statuses)):
            resp = branded.get_agent_roster({"queryStringParameters": {"queueId": "q-1"}}, {})

        body = json.loads(resp["body"])
        assert body["agents"][0]["isIntentionalAbsence"] is True

    def test_on_a_break_is_not_flagged_intentional(self):
        from handlers import branded

        ud = [_user_data(status_arn="arn:.../agent-status/s-brk", status_name="On a Break")]
        statuses = [{"Id": "s-brk", "Name": "On a Break", "Type": "CUSTOM"}]

        with patch("handlers.branded._connect", _mock_connect(ud, statuses)):
            resp = branded.get_agent_roster({"queryStringParameters": {"queueId": "q-1"}}, {})

        body = json.loads(resp["body"])
        assert body["agents"][0]["isIntentionalAbsence"] is False
