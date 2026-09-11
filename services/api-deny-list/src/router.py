"""Route table for api-deny-list Lambda."""

from __future__ import annotations

from collections.abc import Callable

from handlers import deny_list as deny_list_handler

Handler = Callable[[dict, dict], dict]


ROUTES: dict[str, Handler] = {
    "GET /deny-list": deny_list_handler.list_blocked_numbers,
    "POST /deny-list": deny_list_handler.add_blocked_number,
}


def resolve(route_key: str) -> Handler | None:
    return ROUTES.get(route_key)
