"""Tests for the GET /location-mapping handler and unknown-location detection in executor."""

from __future__ import annotations

import json as _json
import sys
import os
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

# Stub vip_shared before any handler import (it lives in a Lambda layer).
_VIP_SHARED_STUB: dict = {}
for _mod in ("vip_shared", "vip_shared.application", "vip_shared.application.http",
             "vip_shared.infrastructure", "vip_shared.infrastructure.persistence",
             "vip_shared.infrastructure.persistence.audit"):
    _VIP_SHARED_STUB[_mod] = MagicMock()

# json_response must return a real dict so handler assertions work.
def _json_response(status: int, body: object) -> dict:
    return {"statusCode": status, "body": _json.dumps(body)}

_VIP_SHARED_STUB["vip_shared.application.http"].json_response = _json_response

# ── Shared DynamoDB stub ──────────────────────────────────────────────────────

_STUB_BY_CODE: dict = {
    "NY": ["New York", "NY - Brighton Beach"],
    "NJ": ["New Jersey", "NJ - Clifton"],
    "TX": ["Texas", "TX - Addison", "TX - Cinco Ranch"],
}
_STUB_GROUPS = [
    {"state": "New York", "slug": "NewYork", "code": "NY", "locations": _STUB_BY_CODE["NY"]},
    {"state": "New Jersey", "slug": "NewJersey", "code": "NJ", "locations": _STUB_BY_CODE["NJ"]},
    {"state": "Texas", "slug": "Texas", "code": "TX", "locations": _STUB_BY_CODE["TX"]},
]
_STUB_ALL = frozenset(loc for locs in _STUB_BY_CODE.values() for loc in locs)
_STUB_MAPPING = (_STUB_BY_CODE, _STUB_GROUPS, _STUB_ALL)


# ── GET /location-mapping handler ─────────────────────────────────────────────


class TestGetLocationMappingHandler:
    @pytest.fixture(autouse=True)
    def _patch_all(self):
        with patch.dict(sys.modules, _VIP_SHARED_STUB):
            # Remove cached handler import so it picks up the stub
            sys.modules.pop("handlers.plans", None)
            sys.modules.pop("handlers", None)
            with patch("builders._load_location_mapping", return_value=_STUB_MAPPING):
                yield
            sys.modules.pop("handlers.plans", None)
            sys.modules.pop("handlers", None)

    def _get_handler(self):
        from handlers.plans import get_location_mapping  # type: ignore[import]
        return get_location_mapping

    def test_returns_200(self):
        handler = self._get_handler()
        resp = handler({}, {})
        assert resp["statusCode"] == 200

    def test_response_has_groups_key(self):
        import json as _json2
        handler = self._get_handler()
        resp = handler({}, {})
        body = _json2.loads(resp["body"])
        assert "groups" in body

    def test_groups_have_expected_structure(self):
        import json as _json2
        handler = self._get_handler()
        resp = handler({}, {})
        body = _json2.loads(resp["body"])
        for g in body["groups"]:
            assert "state" in g
            assert "slug" in g
            assert "code" in g
            assert "locations" in g
            assert "stateSortOrder" not in g

    def test_tx_group_present_in_response(self):
        import json as _json2
        handler = self._get_handler()
        resp = handler({}, {})
        body = _json2.loads(resp["body"])
        codes = [g["code"] for g in body["groups"]]
        assert "TX" in codes
        tx = next(g for g in body["groups"] if g["code"] == "TX")
        assert "TX - Cinco Ranch" in tx["locations"]


# ── Unknown location detection logic ─────────────────────────────────────────
# The detection logic inside _create_segment reads each record's location field
# and compares it against all_known_locations(). We test the core logic directly
# rather than through _create_segment (which has deep Lambda-layer dependencies).


def _run_detection(records: list[dict], known: frozenset[str]) -> set[str]:
    """Replicate the unknown-location detection loop from executor._create_segment."""
    unknown_locs: set[str] = set()
    for record in records:
        loc_val = str(record.get("location", "")).strip()
        if loc_val and loc_val not in known:
            unknown_locs.add(loc_val)
    return unknown_locs


class TestUnknownLocationDetectionLogic:
    """Unit-test the set-difference logic used in executor._create_segment."""

    def test_known_location_not_flagged(self):
        known = frozenset(["New York", "NJ - Clifton"])
        records = [{"location": "New York"}, {"location": "NJ - Clifton"}]
        assert _run_detection(records, known) == set()

    def test_unknown_location_flagged(self):
        known = frozenset(["New York"])
        records = [{"location": "TX - Unknown Clinic"}]
        assert _run_detection(records, known) == {"TX - Unknown Clinic"}

    def test_empty_location_field_not_flagged(self):
        known = frozenset(["New York"])
        records = [{"location": ""}, {}, {"customerid": "c1"}]
        assert _run_detection(records, known) == set()

    def test_multiple_unknowns_collected(self):
        known = frozenset(["New York"])
        records = [
            {"location": "TX - Clinic A"},
            {"location": "TX - Clinic B"},
            {"location": "New York"},
        ]
        result = _run_detection(records, known)
        assert result == {"TX - Clinic A", "TX - Clinic B"}

    def test_deduplication(self):
        known = frozenset(["New York"])
        records = [{"location": "TX - Unknown"}, {"location": "TX - Unknown"}]
        assert _run_detection(records, known) == {"TX - Unknown"}
