"""Route table for api-campaigns Lambda."""

from __future__ import annotations

from collections.abc import Callable

from handlers import campaigns as campaigns_handler
from handlers import resources as resources_handler

Handler = Callable[[dict, dict], dict]


ROUTES: dict[str, Handler] = {
    # Campaign CRUD
    "GET /campaigns": campaigns_handler.list_campaigns,
    "POST /campaigns": campaigns_handler.create_campaign,
    "GET /campaigns/{id}": campaigns_handler.get_campaign,
    "DELETE /campaigns/{id}": campaigns_handler.delete_campaign,
    "PATCH /campaigns/{id}": campaigns_handler.update_campaign,
    # Lifecycle
    "POST /campaigns/{id}/start": campaigns_handler.start_campaign,
    "POST /campaigns/{id}/stop": campaigns_handler.stop_campaign,
    "POST /campaigns/{id}/pause": campaigns_handler.pause_campaign,
    "POST /campaigns/{id}/resume": campaigns_handler.resume_campaign,
    # Resource lookups (for form dropdowns)
    "GET /campaigns/resources/queues": resources_handler.list_queues,
    "GET /campaigns/resources/contact-flows": resources_handler.list_contact_flows,
    "GET /campaigns/resources/phone-numbers": resources_handler.list_phone_numbers,
}


def resolve(route_key: str) -> Handler | None:
    return ROUTES.get(route_key)
