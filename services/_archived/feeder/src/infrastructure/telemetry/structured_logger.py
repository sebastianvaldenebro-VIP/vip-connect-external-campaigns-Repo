from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
from typing import Any

PHI_FIELDS = frozenset({"phone", "first_name", "last_name", "fullname", "phone_number"})


def hash_identifier(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


class StructuredLogger:
    """JSON logger that scrubs PHI fields automatically."""

    def __init__(self, service: str, level: str = "INFO") -> None:
        self._service = service
        self._logger = logging.getLogger(service)
        self._logger.setLevel(level.upper())
        if not self._logger.handlers:
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(handler)
            self._logger.propagate = False

    def info(self, event: str, **kwargs: Any) -> None:
        self._emit(logging.INFO, event, kwargs)

    def warn(self, event: str, **kwargs: Any) -> None:
        self._emit(logging.WARNING, event, kwargs)

    def error(self, event: str, **kwargs: Any) -> None:
        self._emit(logging.ERROR, event, kwargs)

    def _emit(self, level: int, event: str, fields: dict[str, Any]) -> None:
        record = {
            "service": self._service,
            "event": event,
            **self._scrub(fields),
        }
        self._logger.log(level, json.dumps(record, default=str))

    @classmethod
    def _scrub(cls, fields: dict[str, Any]) -> dict[str, Any]:
        scrubbed: dict[str, Any] = {}
        for key, value in fields.items():
            if key in PHI_FIELDS:
                scrubbed[f"{key}_hash"] = hash_identifier(str(value)) if value else None
            elif key == "lead_id" and value:
                scrubbed["lead_id_hash"] = hash_identifier(str(value))
                scrubbed["lead_id_prefix"] = str(value)[:8]
            else:
                scrubbed[key] = value
        return scrubbed


def build(service: str | None = None) -> StructuredLogger:
    return StructuredLogger(
        service=service or os.environ.get("POWERTOOLS_SERVICE_NAME", "feeder"),
        level=os.environ.get("LOG_LEVEL", "INFO"),
    )
