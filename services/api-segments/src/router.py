"""Route table for api-segments Lambda.

API Gateway HTTP API sends events with `routeKey` in the form `METHOD /path`.
This module maps those to handler functions. Each handler receives the full
event + path params + Caller + deps, returns an API Gateway response dict.
"""

from __future__ import annotations

from collections.abc import Callable

from handlers import diagnose as diagnose_handler
from handlers import estimate as estimate_handler
from handlers import leads as leads_handler
from handlers import reconcile as reconcile_handler
from handlers import segments as segment_handler
from handlers import snapshot as snapshot_handler
from handlers import sync_mode as sync_mode_handler
from handlers import verify as verify_handler
from handlers import verify_extras as verify_extras_handler

Handler = Callable[[dict, dict], dict]


ROUTES: dict[str, Handler] = {
    # Segment CRUD
    "GET /segments": segment_handler.list_segments,
    "POST /segments": segment_handler.create_segment,
    "GET /segments/{id}": segment_handler.get_segment,
    "PATCH /segments/{id}": sync_mode_handler.update_sync_mode,
    "DELETE /segments/{id}": segment_handler.delete_segment,
    # Estimates (async recompute)
    "POST /segments/{id}/estimate": estimate_handler.create_estimate,
    "GET /segments/{id}/estimate/{estimateId}": estimate_handler.get_estimate,
    # Snapshots (export)
    "POST /segments/{id}/snapshot": snapshot_handler.create_snapshot,
    "GET /segments/{id}/snapshot/{snapshotId}": snapshot_handler.get_snapshot,
    # Redis verification + rebuild (manual-sync segments only)
    "POST /segments/{id}/verify": verify_handler.verify_segment,
    "POST /segments/{id}/verify/extras": verify_extras_handler.start_extras_detection,
    "GET /segments/{id}/verify/extras/{snapshotId}": verify_extras_handler.get_extras_detection,
    "POST /segments/{id}/reconcile": reconcile_handler.reconcile_segment,
    "POST /segments/{id}/diagnose": diagnose_handler.diagnose_staleness,
    # Segment-create form helpers — Redis scan for distinct values + count preview
    "GET /leads/distinct-values": leads_handler.list_distinct_values,
    "POST /segments/preview-count": leads_handler.preview_count,
}


def resolve(route_key: str) -> Handler | None:
    return ROUTES.get(route_key)
