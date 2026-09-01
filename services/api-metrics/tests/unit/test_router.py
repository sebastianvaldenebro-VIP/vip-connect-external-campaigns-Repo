"""Tests for the route table — resolve() must map API Gateway's routeKey
string to the correct handler function.
"""

from __future__ import annotations


def test_resolves_audit_entity_history_with_greedy_path_param():
    from handlers import audit as audit_handler
    from router import resolve

    handler = resolve("GET /audit/{entityId+}")
    assert handler is audit_handler.get_entity_history


def test_old_non_greedy_route_key_no_longer_resolves():
    """Guards against a regression where someone reverts the route table key
    without also reverting the CDK path (or vice versa) — the two must always
    agree on greedy vs. non-greedy, or requests silently 404.
    """
    from router import resolve

    assert resolve("GET /audit/{entityId}") is None


def test_resolves_audit_list():
    from handlers import audit as audit_handler
    from router import resolve

    handler = resolve("GET /audit")
    assert handler is audit_handler.list_audit_entries


def test_unknown_route_key_returns_none():
    from router import resolve

    assert resolve("GET /nonexistent") is None
