"""Route table for api-profiles Lambda."""
from __future__ import annotations

from collections.abc import Callable

from handlers import profiles as profiles_handler

Handler = Callable[[dict, dict], dict]


ROUTES: dict[str, Handler] = {
    "GET /profiles/search": profiles_handler.search_profiles,
    "POST /profiles/batch": profiles_handler.batch_get_profiles,
    "GET /profiles/{profileId}": profiles_handler.get_profile,
    "GET /profiles/{profileId}/objects": profiles_handler.list_objects,
    "GET /profiles/{profileId}/calculated-attributes": profiles_handler.list_calculated_attrs,
    "GET /profiles/{profileId}/calculated-attributes/{attrName}": profiles_handler.get_calculated_attr,
}


def resolve(route_key: str) -> Handler | None:
    return ROUTES.get(route_key)
