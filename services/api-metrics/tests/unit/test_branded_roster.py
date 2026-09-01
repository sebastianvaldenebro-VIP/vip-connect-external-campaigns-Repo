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


class TestPagination:
    """GetCurrentUserData paginates via NextToken — a single unpaginated call
    silently drops agents past the first page.
    """

    def test_follows_next_token_across_pages(self):
        from handlers import branded

        page1 = {
            "UserDataList": [_user_data(user_id="u-1", status_arn="arn:.../agent-status/s-avail")],
            "NextToken": "token-2",
        }
        page2 = {
            "UserDataList": [_user_data(user_id="u-2", status_arn="arn:.../agent-status/s-avail")],
        }
        statuses = [{"Id": "s-avail", "Name": "Available", "Type": "ROUTABLE"}]

        mock = _mock_connect([], statuses)
        mock.get_current_user_data.side_effect = [page1, page2]

        with patch("handlers.branded._connect", mock):
            resp = branded.get_agent_roster({"queryStringParameters": {"queueId": "q-1"}}, {})

        body = json.loads(resp["body"])
        assert {a["agentId"] for a in body["agents"]} == {"u-1", "u-2"}
        assert mock.get_current_user_data.call_count == 2

    def test_second_call_passes_the_next_token(self):
        from handlers import branded

        page1 = {"UserDataList": [], "NextToken": "token-2"}
        page2 = {"UserDataList": []}
        statuses: list = []

        mock = _mock_connect([], statuses)
        mock.get_current_user_data.side_effect = [page1, page2]

        with patch("handlers.branded._connect", mock):
            branded.get_agent_roster({"queryStringParameters": {"queueId": "q-1"}}, {})

        second_call_kwargs = mock.get_current_user_data.call_args_list[1].kwargs
        assert second_call_kwargs["NextToken"] == "token-2"

    def test_single_page_response_still_works(self):
        """No NextToken in the response — the existing single-page behavior
        (every other test in this file) must not regress.
        """
        from handlers import branded

        ud = [_user_data(status_arn="arn:.../agent-status/s-avail")]
        statuses = [{"Id": "s-avail", "Name": "Available", "Type": "ROUTABLE"}]

        with patch("handlers.branded._connect", _mock_connect(ud, statuses)):
            resp = branded.get_agent_roster({"queryStringParameters": {"queueId": "q-1"}}, {})

        body = json.loads(resp["body"])
        assert len(body["agents"]) == 1


class TestRoutingProfileIdsNotTruncated:
    """_routing_profile_ids() must return every profile the paginator yields,
    not just the first 100 — GetCurrentUserData's 100-entry filter limit is
    the caller's problem to batch around, not this function's to hide data.
    """

    def test_returns_all_ids_beyond_100(self):
        from handlers import branded

        page1_profiles = [{"Id": f"rp-{i}", "Name": f"RP {i}"} for i in range(100)]
        page2_profiles = [{"Id": f"rp-{i}", "Name": f"RP {i}"} for i in range(100, 150)]
        mock = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {"RoutingProfileSummaryList": page1_profiles},
            {"RoutingProfileSummaryList": page2_profiles},
        ]
        mock.get_paginator.return_value = paginator

        with patch("handlers.branded._connect", mock):
            ids = branded._routing_profile_ids()

        assert len(ids) == 150


class TestRoutingProfileBatching:
    """GetCurrentUserData's RoutingProfiles filter accepts max 100 entries —
    an instance with more than 100 routing profiles must be queried in
    multiple batches, not silently truncated to the first 100.
    """

    def test_more_than_100_profiles_are_batched_not_truncated(self):
        from handlers import branded

        page1_profiles = [{"Id": f"rp-{i}", "Name": f"RP {i}"} for i in range(100)]
        page2_profiles = [{"Id": f"rp-{i}", "Name": f"RP {i}"} for i in range(100, 150)]

        mock = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {"RoutingProfileSummaryList": page1_profiles},
            {"RoutingProfileSummaryList": page2_profiles},
        ]
        mock.get_paginator.return_value = paginator
        mock.list_agent_statuses.return_value = {
            "AgentStatusSummaryList": [{"Id": "s-avail", "Name": "Available", "Type": "ROUTABLE"}],
        }
        mock.describe_user.return_value = {
            "User": {"IdentityInfo": {"FirstName": "A", "LastName": "B"}, "Username": "ab"},
        }

        batch1_response = {
            "UserDataList": [_user_data(user_id="u-1", rp_id="rp-0", status_arn="arn:.../agent-status/s-avail")],
        }
        batch2_response = {
            "UserDataList": [_user_data(user_id="u-2", rp_id="rp-149", status_arn="arn:.../agent-status/s-avail")],
        }
        mock.get_current_user_data.side_effect = [batch1_response, batch2_response]

        with patch("handlers.branded._connect", mock):
            resp = branded.get_agent_roster({"queryStringParameters": {}}, {})

        body = json.loads(resp["body"])
        assert {a["agentId"] for a in body["agents"]} == {"u-1", "u-2"}
        assert mock.get_current_user_data.call_count == 2
        first_filters = mock.get_current_user_data.call_args_list[0].kwargs["Filters"]
        second_filters = mock.get_current_user_data.call_args_list[1].kwargs["Filters"]
        assert len(first_filters["RoutingProfiles"]) == 100
        assert len(second_filters["RoutingProfiles"]) == 50

    def test_100_or_fewer_profiles_use_a_single_batch(self):
        from handlers import branded

        profiles = [{"Id": f"rp-{i}", "Name": f"RP {i}"} for i in range(50)]
        mock = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [{"RoutingProfileSummaryList": profiles}]
        mock.get_paginator.return_value = paginator
        mock.list_agent_statuses.return_value = {
            "AgentStatusSummaryList": [{"Id": "s-avail", "Name": "Available", "Type": "ROUTABLE"}],
        }
        mock.get_current_user_data.return_value = {"UserDataList": []}

        with patch("handlers.branded._connect", mock):
            branded.get_agent_roster({"queryStringParameters": {}}, {})

        assert mock.get_current_user_data.call_count == 1

    def test_each_batch_still_paginates_via_next_token(self):
        """A single routing-profile batch that itself has >100 agents must
        still follow NextToken within that batch (regression guard: the
        earlier agent-level pagination fix must keep working now that it's
        nested inside per-batch iteration)."""
        from handlers import branded

        profiles = [{"Id": "rp-1", "Name": "RP 1"}]
        mock = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [{"RoutingProfileSummaryList": profiles}]
        mock.get_paginator.return_value = paginator
        mock.list_agent_statuses.return_value = {
            "AgentStatusSummaryList": [{"Id": "s-avail", "Name": "Available", "Type": "ROUTABLE"}],
        }

        page1 = {
            "UserDataList": [_user_data(user_id="u-1", rp_id="rp-1", status_arn="arn:.../agent-status/s-avail")],
            "NextToken": "tok-2",
        }
        page2 = {"UserDataList": [_user_data(user_id="u-2", rp_id="rp-1", status_arn="arn:.../agent-status/s-avail")]}
        mock.get_current_user_data.side_effect = [page1, page2]

        with patch("handlers.branded._connect", mock):
            resp = branded.get_agent_roster({"queryStringParameters": {}}, {})

        body = json.loads(resp["body"])
        assert {a["agentId"] for a in body["agents"]} == {"u-1", "u-2"}


class TestEmptyRosterResponseShape:
    """The empty-roster early-return must carry the same 4 keys the TS type
    declares non-optional (routingProfiles, lastUpdated) — a prior version
    omitted them, which type-disagreed with the frontend contract."""

    def test_empty_roster_still_returns_routing_profiles_and_last_updated(self):
        from handlers import branded

        mock = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [{"RoutingProfileSummaryList": []}]
        mock.get_paginator.return_value = paginator

        with patch("handlers.branded._connect", mock):
            resp = branded.get_agent_roster({"queryStringParameters": {}}, {})

        body = json.loads(resp["body"])
        assert body["agents"] == []
        assert body["routingProfiles"] == []
        assert "lastUpdated" in body and body["lastUpdated"]
