"""Lambda entrypoint for api-plans.

Two event sources:
  1. API Gateway HTTP API → route via router.resolve()
  2. EventBridge Scheduler → tick payload: {"action": "tick", "planId", "runId", "bucketIndex"}
"""
from __future__ import annotations

from botocore.exceptions import ClientError

from vip_shared.application.http import error_response
from vip_shared.infrastructure.telemetry.structured_logger import StructuredLogger

import executor
from router import resolve
from store import ConcurrentWriteError

_logger = StructuredLogger(service="api-plans")


def lambda_handler(event: dict, context) -> dict:
    # EventBridge Rules tick / scheduled_run
    action = event.get("action")
    if action == "tick":
        plan_id = event.get("planId", "")
        run_id = event.get("runId", "")
        bucket_index = int(event.get("bucketIndex", 0))
        _logger.info("tick_received", plan_id=plan_id, run_id=run_id, bucket_index=bucket_index)
        try:
            result = executor.tick(plan_id, run_id, bucket_index)
            return result
        except ConcurrentWriteError:
            _logger.info("tick_concurrent_write", plan_id=plan_id, run_id=run_id, bucket_index=bucket_index)
            return {"ok": True, "reason": "concurrent_write"}
        except Exception as exc:
            _logger.error("tick_unhandled_error", error=str(exc), plan_id=plan_id, run_id=run_id, bucket_index=bucket_index)
            return {"ok": False, "error": str(exc)}

    if action == "scheduled_run":
        plan_id = event.get("planId", "")
        _logger.info("scheduled_run_received", plan_id=plan_id)
        try:
            result = executor.scheduled_run(plan_id)
            return result
        except Exception as exc:
            _logger.error("scheduled_run_error", error=str(exc), plan_id=plan_id)
            return {"ok": False, "error": str(exc)}

    if action == "chain_trigger":
        plan_id = event.get("planId", "")
        _logger.info("chain_trigger_received", plan_id=plan_id)
        try:
            executor.start_run_chained(plan_id)
            return {"ok": True}
        except Exception as exc:
            _logger.error("chain_trigger_error", error=str(exc), plan_id=plan_id)
            return {"ok": False, "error": str(exc)}

    if action == "prestart_check":
        _logger.info("prestart_check_received")
        try:
            result = executor.prestart_check()
            _logger.info("prestart_check_done", warmed=result.get("warmed", []))
            return {"ok": True, **result}
        except Exception as exc:
            _logger.error("prestart_check_error", error=str(exc))
            return {"ok": False, "error": str(exc)}

    # HTTP API event
    route_key = event.get("routeKey") or event.get("requestContext", {}).get("routeKey", "")
    path_params = event.get("pathParameters") or {}

    _logger.info("request_received", route_key=route_key, path_params=path_params)

    handler = resolve(route_key)
    if handler is None:
        return error_response(404, "ROUTE_NOT_FOUND", f"No handler for {route_key}")

    try:
        return handler(event, path_params)

    except ValueError as exc:
        _logger.warn("validation_error", error=str(exc))
        return error_response(400, "VALIDATION_ERROR", str(exc))

    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "ClientError")
        message = exc.response.get("Error", {}).get("Message", str(exc))
        _logger.error("aws_error", code=code, message=message, route_key=route_key)
        return error_response(_aws_status(code), code, message)

    except Exception as exc:
        _logger.error("unhandled_error", error=str(exc), route_key=route_key)
        return error_response(500, "INTERNAL_ERROR", "Unexpected error",
                              request_id=context.aws_request_id if context else None)


def _aws_status(code: str) -> int:
    return {
        "ResourceNotFoundException": 404,
        "ValidationException": 400,
        "AccessDeniedException": 403,
        "ConflictException": 409,
        "ThrottlingException": 429,
        "BadRequestException": 400,
    }.get(code, 500)
