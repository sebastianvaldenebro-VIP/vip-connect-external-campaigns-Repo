"""Shared pytest fixtures for api-plans unit tests."""

from unittest.mock import patch

import pytest


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
