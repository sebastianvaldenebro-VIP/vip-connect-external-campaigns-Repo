"""Tests for the api-profiles route table."""

from __future__ import annotations


def test_resolve_returns_handler_for_known_route():
    from handlers import profiles as profiles_handler
    from router import resolve

    handler = resolve("GET /profiles/search")

    assert handler is profiles_handler.search_profiles


def test_resolve_returns_none_for_unknown_route():
    from router import resolve

    assert resolve("DELETE /profiles/{profileId}") is None


def test_resolve_covers_all_registered_routes():
    from router import ROUTES, resolve

    for route_key, handler in ROUTES.items():
        assert resolve(route_key) is handler
