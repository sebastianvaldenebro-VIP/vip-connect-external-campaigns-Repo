"""Regression guard: connect_open_hours.py must have ZERO transitive dependency
on `phonenumbers`.

Background (the incident this test exists to prevent from recurring): a
cleanup commit once merged this Connect Campaigns V2 openHours builder into
quiet_hours.py, which unconditionally `import phonenumbers` at module scope
for its own (unrelated) per-recipient SMS quiet-hours gate. api-plans and
api-campaigns's builders.py both import `connect_open_hours` at module scope,
and their Lambda entrypoints import builders.py eagerly at cold start — but
their Lambda layers are built from plain `requirements.txt`, which does NOT
include `phonenumbers` (only api-sms's layer does, via
`requirements-sms.txt`; see infra/lib/utils/shared-layer.ts). Shipping that
merge would have crashed every single api-plans/api-campaigns invocation at
import time with `ModuleNotFoundError: No module named 'phonenumbers'` — a
total outage of both services.

`phonenumbers` happens to be pip-installed in this dev machine's ambient
site-packages, so a normal pytest run cannot catch this class of bug — the
import silently succeeds here even when it would fail in the real deployed
layer. To catch it for real, this test blocks `phonenumbers` from being
importable *within this test process* via `sys.modules['phonenumbers'] =
None` — Python's import system raises ImportError immediately for any
`import phonenumbers` while that sentinel is set, faithfully simulating "not
installed" without needing a separate venv.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Iterator

_MODULE_NAME = "vip_shared.domain.services.connect_open_hours"


@contextmanager
def _phonenumbers_blocked() -> Iterator[None]:
    """Block `import phonenumbers` and force a fresh import of the target module.

    Removing any cached module object for `_MODULE_NAME` before the `with`
    body runs is essential: if a prior test already imported it, Python's
    import system would just return the cached object without re-executing
    its import statements, which would silently hide a phonenumbers
    regression re-introduced into the module.
    """
    saved_target = sys.modules.pop(_MODULE_NAME, None)
    saved_phonenumbers = sys.modules.get("phonenumbers")
    sys.modules["phonenumbers"] = None  # type: ignore[assignment]
    try:
        yield
    finally:
        del sys.modules["phonenumbers"]
        if saved_phonenumbers is not None:
            sys.modules["phonenumbers"] = saved_phonenumbers
        sys.modules.pop(_MODULE_NAME, None)
        if saved_target is not None:
            sys.modules[_MODULE_NAME] = saved_target


def test_connect_open_hours_module_imports_without_phonenumbers():
    """The core regression guard: import must succeed with phonenumbers unimportable."""
    with _phonenumbers_blocked():
        import vip_shared.domain.services.connect_open_hours as connect_open_hours_module

        result = connect_open_hours_module.connect_open_hours()

    assert "openHours" in result
    assert "dailyHours" in result["openHours"]
    days = result["openHours"]["dailyHours"]
    assert set(days) == {
        "MONDAY",
        "TUESDAY",
        "WEDNESDAY",
        "THURSDAY",
        "FRIDAY",
        "SATURDAY",
    }
    assert "SUNDAY" not in days
    for windows in days.values():
        assert windows == [{"startTime": "T08:00", "endTime": "T21:00"}]


def test_connect_open_hours_constants_survive_with_phonenumbers_blocked():
    with _phonenumbers_blocked():
        import vip_shared.domain.services.connect_open_hours as connect_open_hours_module

        assert connect_open_hours_module.CONNECT_QUIET_HOURS_START == "T08:00"
        assert connect_open_hours_module.CONNECT_QUIET_HOURS_END == "T21:00"
        assert connect_open_hours_module.CONNECT_CONTACT_DAYS == (
            "MONDAY",
            "TUESDAY",
            "WEDNESDAY",
            "THURSDAY",
            "FRIDAY",
            "SATURDAY",
        )
