"""Shared pytest fixtures for api-plans unit tests."""

import importlib.util
import os
import socket
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.endpoint import Endpoint


def pytest_configure(config):
    """Keep collection and tests offline, regardless of the caller's AWS setup.

    Some legacy lifecycle tests intentionally ignore auxiliary telemetry,
    cancellation cleanup or chained-plan lookups. Without a transport guard,
    those unmocked calls fail fast only when AWS configuration is absent; a
    configured developer machine can otherwise contact real services or hang.
    Stubber resolves requests before Endpoint.make_request, so explicit SDK
    stubs and normal unittest mocks continue to exercise their own responses.
    """
    guard = pytest.MonkeyPatch()
    config.add_cleanup(guard.undo)
    for key, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_CONFIG_FILE": "/dev/null",
        "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
        "AWS_DEFAULT_REGION": os.environ.get("AWS_DEFAULT_REGION") or "us-east-1",
    }.items():
        guard.setenv(key, value)
    guard.delenv("AWS_PROFILE", raising=False)

    def deny_aws_request(self, operation_model, request_dict):
        raise RuntimeError(f"Offline Plans tests: unmocked AWS {operation_model.name} request blocked")

    def deny_socket(*args, **kwargs):
        raise RuntimeError("Offline Plans tests: network connection blocked")

    guard.setattr(Endpoint, "make_request", deny_aws_request)
    guard.setattr(socket.socket, "connect", deny_socket)
    guard.setattr(socket.socket, "connect_ex", deny_socket)
    guard.setattr(socket, "create_connection", deny_socket)
    guard.setattr(socket, "getaddrinfo", deny_socket)

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

# Profile-mode validation uses the real catalog version, even when the Lambda
# layer's parent packages are stubbed by the existing executor tests.
_PRECALL_CATALOG_MODULE_NAME = "vip_shared.domain.services.precall_sms"
if _PRECALL_CATALOG_MODULE_NAME not in sys.modules:
    _precall_catalog_path = os.path.join(
        os.path.dirname(__file__),
        "../../../shared/python/vip_shared/domain/services/precall_sms.py",
    )
    _spec = importlib.util.spec_from_file_location(
        _PRECALL_CATALOG_MODULE_NAME, _precall_catalog_path
    )
    _precall_catalog_module = importlib.util.module_from_spec(_spec)
    sys.modules[_PRECALL_CATALOG_MODULE_NAME] = _precall_catalog_module
    _spec.loader.exec_module(_precall_catalog_module)

# Keep campaign SMS validation real even when legacy tests stub layer packages.
_SMS_CAMPAIGN_MODULE_NAME = "vip_shared.domain.services.sms_campaign"
if _SMS_CAMPAIGN_MODULE_NAME not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        _SMS_CAMPAIGN_MODULE_NAME,
        os.path.join(os.path.dirname(__file__),
                     "../../../shared/python/vip_shared/domain/services/sms_campaign.py"),
    )
    _sms_campaign_module = importlib.util.module_from_spec(_spec)
    sys.modules[_SMS_CAMPAIGN_MODULE_NAME] = _sms_campaign_module
    _spec.loader.exec_module(_sms_campaign_module)

_SMS_ORIGINATION_MODULE_NAME = "vip_shared.domain.services.sms_origination"
if _SMS_ORIGINATION_MODULE_NAME not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        _SMS_ORIGINATION_MODULE_NAME,
        os.path.join(os.path.dirname(__file__),
                     "../../../shared/python/vip_shared/domain/services/sms_origination.py"),
    )
    _sms_origination_module = importlib.util.module_from_spec(_spec)
    sys.modules[_SMS_ORIGINATION_MODULE_NAME] = _sms_origination_module
    _spec.loader.exec_module(_sms_origination_module)

# Same real-module-loading trick, now for builders.py's TCPA openHours import
# (fix_later cleanup: builders.py used to define _open_hours()/_QUIET_HOURS_*
# locally; it now imports connect_open_hours from this shared module). Needs
# the REAL function — a generic MagicMock stub would make
# build_campaign_params/build_create_campaign_params's communicationTimeConfig
# assertions compare against Mock() attribute-access noise instead of the
# actual openHours dict the tests pin down byte-for-byte.
#
# This loads connect_open_hours.py, NOT quiet_hours.py: quiet_hours.py
# unconditionally imports `phonenumbers` at module scope (for the unrelated
# per-recipient SMS quiet-hours gate), which builders.py in this Lambda no
# longer imports and must not depend on transitively (see the phonenumbers/
# api-plans-cold-start incident this split was made to fix).
_QUIET_HOURS_MODULE_NAME = "vip_shared.domain.services.connect_open_hours"
if _QUIET_HOURS_MODULE_NAME not in sys.modules:
    _quiet_hours_path = os.path.join(
        os.path.dirname(__file__),
        "../../../shared/python/vip_shared/domain/services/connect_open_hours.py",
    )
    _spec = importlib.util.spec_from_file_location(
        _QUIET_HOURS_MODULE_NAME, _quiet_hours_path
    )
    _quiet_hours_module = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_quiet_hours_module)
    sys.modules[_QUIET_HOURS_MODULE_NAME] = _quiet_hours_module

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
