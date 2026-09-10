"""Tests for OptOutRepository — shared cross-channel opt-out store."""

from __future__ import annotations

from unittest.mock import MagicMock

from vip_shared.infrastructure.persistence.opt_out import (
    OptOutRepository,
    build_from_env,
)


def test_is_blocked_returns_true_when_item_exists():
    mock_table = MagicMock()
    mock_table.get_item.return_value = {"Item": {"ContactNumber": "+15125551234"}}
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    repo = OptOutRepository(
        table_name="VipConnectOptOutList", dynamodb_resource=mock_resource
    )

    assert repo.is_blocked("+15125551234") is True
    mock_table.get_item.assert_called_once_with(Key={"ContactNumber": "+15125551234"})


def test_is_blocked_returns_false_when_item_missing():
    mock_table = MagicMock()
    mock_table.get_item.return_value = {}
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    repo = OptOutRepository(
        table_name="VipConnectOptOutList", dynamodb_resource=mock_resource
    )

    assert repo.is_blocked("+15125551234") is False


def test_block_writes_reason_and_source():
    mock_table = MagicMock()
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    repo = OptOutRepository(
        table_name="VipConnectOptOutList", dynamodb_resource=mock_resource
    )

    repo.block("+15125551234", reason="Patient replied STOP", source="sms_optout")

    mock_table.put_item.assert_called_once()
    item = mock_table.put_item.call_args.kwargs["Item"]
    assert item["ContactNumber"] == "+15125551234"
    assert item["reason"] == "Patient replied STOP"
    assert item["source"] == "sms_optout"
    assert "addedAt" in item


def test_build_from_env_reads_table_name(monkeypatch):
    monkeypatch.setenv("OPT_OUT_TABLE", "VipConnectOptOutList")

    repo = build_from_env()

    assert isinstance(repo, OptOutRepository)
