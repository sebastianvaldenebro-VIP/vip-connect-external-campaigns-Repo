"""Tests for the api-segments route table."""

from __future__ import annotations


def test_resolve_returns_handler_for_known_route():
    from handlers import segments as segment_handler
    from router import resolve

    handler = resolve("GET /segments")
    assert handler is segment_handler.list_segments


def test_resolve_returns_none_for_unknown_route():
    from router import resolve

    assert resolve("DELETE /nonexistent") is None


def test_resolve_covers_all_registered_routes():
    from router import ROUTES, resolve

    for route_key, handler in ROUTES.items():
        assert resolve(route_key) is handler
