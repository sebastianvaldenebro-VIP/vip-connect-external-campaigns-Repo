"""Tests for router.py — the route table mapping routeKey -> handler function.

router.py does `from handlers import contacts/plans/runs/sms as ..._handler`
at module scope. We only care about the ROUTES mapping and resolve() here —
not the real handler bodies — so those four submodules are stubbed as
MagicMocks inside a *scoped* patch.dict(sys.modules, ...) block (mirroring
test_handlers_plans.py's / test_contacts_handler.py's own convention: those
files need to import handlers.plans / handlers.contacts fresh, with their own
specific store/scheduler_manager mocks, so this file's stub must NOT leak
past its own import).

IMPORTANT: `patch.dict(sys.modules, ...)` clears and restores the *entire*
sys.modules snapshot on __exit__ (see unittest.mock._patch_dict), not just
the keys you passed in — so nothing outside `_stub_modules` may be touched
inside this block, or every OTHER module imported during the block (however
unrelated) gets silently evicted from sys.modules too. Concretely: since the
stubs below short-circuit before router.py ever reaches the real
handlers/plans.py etc., `store`/`scheduler_manager`/`builders`/`executor`/
`vip_shared` are never imported inside this block at all, so there's nothing
of theirs to lose — `vip_shared` is still stubbed, but permanently via
setdefault (see below), never through this context manager.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

# Permanent (never reverted) — safe because other test files only ever
# .setdefault() this too, or overwrite it and rely on their own
# importlib.reload() to re-bind names afterward. Must cover the full set
# executor.py needs (infrastructure.persistence.audit, infrastructure.
# telemetry.structured_logger) — conftest.py's autouse fixture patches
# `executor._emit_branded_metric` before EVERY test in the whole suite, which
# forces a real `import executor` the first time it's ever needed.
sys.modules.setdefault("vip_shared", MagicMock())
sys.modules.setdefault("vip_shared.application", MagicMock())
sys.modules.setdefault("vip_shared.application.http", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure.persistence", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure.persistence.audit", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure.telemetry", MagicMock())
sys.modules.setdefault(
    "vip_shared.infrastructure.telemetry.structured_logger", MagicMock()
)

_stub_handlers = {
    "handlers.contacts": MagicMock(),
    "handlers.plans": MagicMock(),
    "handlers.runs": MagicMock(),
    "handlers.sms": MagicMock(),
}

# `router` may already be cached as a blind MagicMock by test_handler.py
# (which never cares what "router" resolves to — it always patches
# handler.resolve directly). Drop that stub before importing so this file
# gets a genuine, freshly-executed router module built against the handlers.*
# stubs above, regardless of collection order.
sys.modules.pop("router", None)

with patch.dict(sys.modules, _stub_handlers):
    import router  # noqa: E402
    import handlers.contacts as contacts_handler  # noqa: E402
    import handlers.plans as plans_handler  # noqa: E402
    import handlers.runs as runs_handler  # noqa: E402
    import handlers.sms as sms_handler  # noqa: E402


def test_resolve_returns_handler_for_known_route():
    handler = router.resolve("GET /plans")
    assert handler is plans_handler.list_plans


def test_resolve_returns_none_for_unknown_route():
    assert router.resolve("DELETE /nonexistent") is None


def test_resolve_covers_every_registered_route():
    for route_key, handler in router.ROUTES.items():
        assert router.resolve(route_key) is handler


def test_routes_reference_expected_handler_modules():
    assert router.resolve("GET /contacts/{contactId}/artifacts") is (
        contacts_handler.get_artifacts
    )
    assert router.resolve("POST /plans/{id}/runs") is runs_handler.trigger_run
    assert router.resolve("GET /sms/numbers") is sms_handler.list_origination_numbers
    assert router.resolve("PUT /plans/{id}") is plans_handler.update_plan
