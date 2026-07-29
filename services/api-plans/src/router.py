"""Route table for api-plans Lambda."""

from __future__ import annotations

from collections.abc import Callable

from handlers import contacts as contacts_handler
from handlers import plans as plans_handler
from handlers import runs as runs_handler
from handlers import sms as sms_handler

Handler = Callable[[dict, dict], dict]

ROUTES: dict[str, Handler] = {
    # Location mapping (source of truth in DynamoDB)
    "GET /location-mapping": plans_handler.get_location_mapping,
    # Plan CRUD
    "GET /plans": plans_handler.list_plans,
    "POST /plans": plans_handler.create_plan,
    "GET /plans/{id}": plans_handler.get_plan,
    "PUT /plans/{id}": plans_handler.update_plan,
    "DELETE /plans/{id}": plans_handler.delete_plan,
    # Templates
    "GET /templates": plans_handler.list_templates,
    "POST /plans/from-template/{tid}": plans_handler.clone_from_template,
    # Runs
    "POST /plans/{id}/runs": runs_handler.trigger_run,
    "GET /plans/{id}/runs": runs_handler.list_runs,
    "GET /plans/{id}/runs/{runId}": runs_handler.get_run,
    "POST /plans/{id}/runs/{runId}/abort": runs_handler.abort_run,
    "POST /plans/{id}/runs/{runId}/force-finish": runs_handler.force_finish_run,
    "POST /plans/{id}/runs/{runId}/buckets/{bucketIndex}/force-start": runs_handler.force_start_bucket,
    "POST /plans/{id}/runs/{runId}/buckets/{bucketIndex}/force-stop": runs_handler.force_stop_bucket,
    "POST /plans/{id}/runs/{runId}/buckets/{bucketIndex}/campaigns/{campaignIndex}/force-start": runs_handler.force_start_campaign,
    "POST /plans/{id}/runs/{runId}/buckets/{bucketIndex}/campaigns/{campaignIndex}/force-stop": runs_handler.force_stop_campaign,
    "POST /plans/{id}/runs/{runId}/buckets/{bucketIndex}/campaigns/{campaignIndex}/skip": runs_handler.skip_campaign,
    "POST /plans/{id}/runs/{runId}/apply-snapshot": runs_handler.apply_plan_snapshot,
    "GET /plans/{id}/runs/{runId}/branded-progress": runs_handler.branded_progress,
    "GET /plans/{id}/runs/{runId}/branded-queue": runs_handler.branded_queue,
    "GET /plans/{id}/branded-history": runs_handler.branded_history,
    # SMS
    "GET /sms/numbers": sms_handler.list_origination_numbers,
    "GET /plans/{id}/sms-runs": sms_handler.get_sms_runs,
    # Contact artifacts
    "GET /contacts/{contactId}/artifacts": contacts_handler.get_artifacts,
}


def resolve(route_key: str) -> Handler | None:
    return ROUTES.get(route_key)
