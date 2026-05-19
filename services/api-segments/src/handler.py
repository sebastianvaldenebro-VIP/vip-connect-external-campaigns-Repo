"""Lambda entrypoint for api-segments.

Routes requests to the right handler via `router.resolve`. Every handler
returns an API Gateway v2 HTTP API response (status + headers + body).
"""
from __future__ import annotations

from botocore.exceptions import ClientError

from vip_shared.application.http import (
    error_response,
    extract_caller,
)
from vip_shared.infrastructure.telemetry.structured_logger import StructuredLogger

from router import resolve

_logger = StructuredLogger(service="api-segments")


def lambda_handler(event: dict, context) -> dict:
    route_key = event.get("routeKey") or event.get("requestContext", {}).get("routeKey", "")
    path_params = event.get("pathParameters") or {}

    _logger.info("request_received", route_key=route_key, path_params=path_params)

    handler = resolve(route_key)
    if handler is None:
        return error_response(404, "ROUTE_NOT_FOUND", f"No handler for {route_key}")

    try:
        return handler(event, path_params)

    except ValueError as exc:
        # Validation errors
        _logger.warn("validation_error", error=str(exc))
        return error_response(400, "VALIDATION_ERROR", str(exc))

    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "ClientError")
        message = exc.response.get("Error", {}).get("Message", str(exc))
        status = _aws_error_to_status(code)
        _logger.error("aws_error", code=code, message=message, route_key=route_key)
        return error_response(status, code, message,
                              request_id=context.aws_request_id if context else None)

    except Exception as exc:
        _logger.error("unhandled_error", error=str(exc), route_key=route_key)
        return error_response(500, "INTERNAL_ERROR", "Unexpected error",
                              request_id=context.aws_request_id if context else None)


def _aws_error_to_status(aws_code: str) -> int:
    mapping = {
        "ResourceNotFoundException": 404,
        "ValidationException": 400,
        "AccessDeniedException": 403,
        "ConflictException": 409,
        "ThrottlingException": 429,
        "BadRequestException": 400,
    }
    return mapping.get(aws_code, 500)
