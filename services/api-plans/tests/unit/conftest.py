"""Shared pytest fixtures for api-plans unit tests."""

import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# handlers/plans.py's _validate_sms_campaign (Task 3, precall SMS personalization)
# needs vip_shared.domain.services.sms_template's REAL ALLOWED_FIELDS/
# extract_placeholders/max_rendered_length — a generic MagicMock stub (like the
# blind-stub loop below) would make every template look like it has an unknown
# placeholder, since Mock's `__sub__`/`__bool__` don't implement real set
# semantics.
#
# Load the real module from its file directly (bypassing the normal package
# import machinery, which would otherwise need `vip_shared`/`vip_shared.domain`
# to be real packages too — they're not; see the blind-stub loop below) and
# register it under its exact dotted name. Python's import system resolves a
# fully-qualified name straight out of sys.modules before ever consulting a
# parent package's __path__, so this works regardless of what `vip_shared`
# itself is stubbed to, and regardless of collection order — this must run
# before any test file imports handlers.plans, hence living here rather than in
# one test file.
_SMS_TEMPLATE_MODULE_NAME = "vip_shared.domain.services.sms_template"
if _SMS_TEMPLATE_MODULE_NAME not in sys.modules:
    _sms_template_path = os.path.join(
        os.path.dirname(__file__),
        "../../../shared/python/vip_shared/domain/services/sms_template.py",
    )
    _spec = importlib.util.spec_from_file_location(
        _SMS_TEMPLATE_MODULE_NAME, _sms_template_path
    )
    _sms_template_module = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_sms_template_module)
    sys.modules[_SMS_TEMPLATE_MODULE_NAME] = _sms_template_module

# Permanent (never reverted) blind stub for vip_shared's submodule tree.
#
# Real vip_shared isn't installed/importable in this test env — it lives in a
# Lambda layer. `executor.py` does `from vip_shared.infrastructure.persistence.audit
# import build_from_env` at module scope, and the autouse fixture below patches
# `executor._emit_branded_metric` before EVERY test in the suite, which forces a
# real `import executor` (and therefore this vip_shared chain) the first time any
# test file runs — including a single new test file executed in isolation, with
# no other test file around to have stubbed this first.
#
# Only .setdefault() here, never a bare assignment: individual test files (e.g.
# test_contacts_handler.py) install their own more specific vip_shared.application.http
# mock (with real json_response/error_response side effects) and rely on it
# surviving for the rest of the session via importlib.reload() — this must never
# clobber that.
for _mod_name in (
    "vip_shared",
    "vip_shared.application",
    "vip_shared.application.http",
    "vip_shared.infrastructure",
    "vip_shared.infrastructure.persistence",
    "vip_shared.infrastructure.persistence.audit",
    "vip_shared.infrastructure.telemetry",
    "vip_shared.infrastructure.telemetry.structured_logger",
):
    sys.modules.setdefault(_mod_name, MagicMock())


@pytest.fixture(autouse=True)
def _neutralize_branded_metric():
    """Neutralise branded CloudWatch telemetry suite-wide.

    ``executor._emit_branded_metric`` is fire-and-forget telemetry that builds a
    real CloudWatch client when unmocked. Depending on ambient AWS config it
    either fails fast (``NoRegionError``, swallowed) or blocks on a real socket
    connect — the latter made the suite flaky and, without a per-test timeout,
    able to hang/exhaust memory. No test asserts on it, so patch it out for every
    test; a dedicated metric test can re-patch with its own assertion if needed.
    """
    with patch("executor._emit_branded_metric"):
        yield
