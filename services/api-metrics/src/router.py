"""Route table for api-metrics Lambda."""
from __future__ import annotations

from collections.abc import Callable

from handlers import audit as audit_handler
from handlers import metrics as metrics_handler

Handler = Callable[[dict, dict], dict]


ROUTES: dict[str, Handler] = {
    # Metrics
    "GET /metrics/campaigns/{id}": metrics_handler.get_campaign_metrics,
    "GET /metrics/campaigns": metrics_handler.get_campaigns_summary,
    "GET /metrics/queues/{queueId}": metrics_handler.get_queue_realtime,
    "GET /metrics/current": metrics_handler.get_current_realtime,
    "GET /metrics/dispositions": metrics_handler.get_dispositions,

    # Audit log (read-only)
    "GET /audit": audit_handler.list_audit_entries,
    "GET /audit/{entityId}": audit_handler.get_entity_history,
}


def resolve(route_key: str) -> Handler | None:
    return ROUTES.get(route_key)
