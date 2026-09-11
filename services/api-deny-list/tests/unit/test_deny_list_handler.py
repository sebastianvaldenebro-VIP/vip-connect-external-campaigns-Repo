"""Tests for the deny-list (blocked numbers) handlers."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


def _event(body=None, qs=None):
    e = {
        "requestContext": {
            "authorizer": {"jwt": {"claims": {"sub": "u-1", "email": "agent@vip.com"}}},
            "http": {"sourceIp": "1.2.3.4", "userAgent": "ua"},
        }
    }
    if body is not None:
        e["body"] = json.dumps(body)
    if qs is not None:
        e["queryStringParameters"] = qs
    return e


def test_normalize_phone_accepts_10_digit():
    from handlers.deny_list import normalize_phone

    assert normalize_phone("914-262-8237") == "+19142628237"


def test_normalize_phone_accepts_11_digit_with_leading_1():
    from handlers.deny_list import normalize_phone

    assert normalize_phone("19142628237") == "+19142628237"


def test_normalize_phone_accepts_e164_passthrough():
    from handlers.deny_list import normalize_phone

    assert normalize_phone("+19142628237") == "+19142628237"


def test_normalize_phone_rejects_invalid():
    from handlers.deny_list import normalize_phone

    assert normalize_phone("123") is None
    assert normalize_phone("") is None
    assert normalize_phone("+44 20 7946 0958") is None


def test_normalize_phone_rejects_unicode_digits():
    from handlers.deny_list import normalize_phone

    # Full-width Unicode digits (U+FF10-FF19) match Python's default \d/\D,
    # so without re.ASCII these would pass through unstripped and produce a
    # non-None but non-ASCII value that can never match a real caller.
    fullwidth = "１９１４２６２８２３７"
    assert normalize_phone(fullwidth) is None


def test_add_blocked_number_requires_phone():
    from handlers.deny_list import add_blocked_number

    with pytest.raises(ValueError, match="phoneNumber"):
        add_blocked_number(_event(body={}), {})


def test_add_blocked_number_rejects_bad_format():
    from handlers.deny_list import add_blocked_number

    with pytest.raises(ValueError, match="E.164"):
        add_blocked_number(_event(body={"phoneNumber": "123"}), {})


def test_add_blocked_number_rejects_non_string_phone():
    from handlers.deny_list import add_blocked_number

    with pytest.raises(ValueError, match="phoneNumber must be a string"):
        add_blocked_number(_event(body={"phoneNumber": 9142628237}), {})


def test_add_blocked_number_rejects_non_string_reason():
    from handlers.deny_list import add_blocked_number

    with pytest.raises(ValueError, match="reason must be a string"):
        add_blocked_number(
            _event(body={"phoneNumber": "+19142628237", "reason": 123}), {}
        )


def test_add_blocked_number_writes_normalized_key_and_audits():
    from handlers.deny_list import add_blocked_number

    mock_table = MagicMock()
    mock_table.get_item.return_value = {}
    mock_audit = MagicMock()

    with (
        patch("handlers.deny_list._table", return_value=mock_table),
        patch("handlers.deny_list.build_audit", return_value=mock_audit),
    ):
        response = add_blocked_number(
            _event(body={"phoneNumber": "914-262-8237", "reason": "spam"}), {}
        )

    body = json.loads(response["body"])
    assert response["statusCode"] == 201
    assert body["phoneNumber"] == "+19142628237"
    assert body["alreadyBlocked"] is False

    put_kwargs = mock_table.put_item.call_args.kwargs
    assert put_kwargs["Item"]["ContactNumber"] == "+19142628237"
    assert put_kwargs["Item"]["reason"] == "spam"
    assert put_kwargs["Item"]["source"] == "manual-ui"
    assert put_kwargs["Item"]["addedBy"] == "agent@vip.com"

    mock_audit.record.assert_called_once()
    audit_kwargs = mock_audit.record.call_args.kwargs
    # PHI (the full number) must never reach the audit trail's entity_id.
    assert "9142628237" not in audit_kwargs["entity_id"]
    assert audit_kwargs["after"]["reason"] == "spam"


def test_add_blocked_number_reports_already_blocked():
    from handlers.deny_list import add_blocked_number

    mock_table = MagicMock()
    mock_table.get_item.return_value = {"Item": {"ContactNumber": "+19142628237"}}

    with (
        patch("handlers.deny_list._table", return_value=mock_table),
        patch("handlers.deny_list.build_audit", return_value=MagicMock()),
    ):
        response = add_blocked_number(_event(body={"phoneNumber": "+19142628237"}), {})

    body = json.loads(response["body"])
    assert body["alreadyBlocked"] is True


def test_add_blocked_number_preserves_reason_on_reblock_without_new_reason():
    from handlers.deny_list import add_blocked_number

    mock_table = MagicMock()
    mock_table.get_item.return_value = {
        "Item": {"ContactNumber": "+19142628237", "reason": "original spam report"}
    }
    mock_audit = MagicMock()

    with (
        patch("handlers.deny_list._table", return_value=mock_table),
        patch("handlers.deny_list.build_audit", return_value=mock_audit),
    ):
        add_blocked_number(_event(body={"phoneNumber": "+19142628237"}), {})

    put_kwargs = mock_table.put_item.call_args.kwargs
    assert put_kwargs["Item"]["reason"] == "original spam report"

    audit_kwargs = mock_audit.record.call_args.kwargs
    assert audit_kwargs["action"] == "update"
    assert audit_kwargs["before"]["reason"] == "original spam report"


def test_add_blocked_number_audits_create_for_new_block():
    from handlers.deny_list import add_blocked_number

    mock_table = MagicMock()
    mock_table.get_item.return_value = {}
    mock_audit = MagicMock()

    with (
        patch("handlers.deny_list._table", return_value=mock_table),
        patch("handlers.deny_list.build_audit", return_value=mock_audit),
    ):
        add_blocked_number(_event(body={"phoneNumber": "+19142628237"}), {})

    audit_kwargs = mock_audit.record.call_args.kwargs
    assert audit_kwargs["action"] == "create"
    assert audit_kwargs["before"] is None


def test_list_blocked_numbers_sorts_newest_first():
    from handlers.deny_list import list_blocked_numbers

    mock_table = MagicMock()
    mock_table.scan.return_value = {
        "Items": [
            {"ContactNumber": "+11111111111", "addedAt": "2026-01-01T00:00:00Z"},
            {"ContactNumber": "+12222222222", "addedAt": "2026-06-01T00:00:00Z"},
        ]
    }

    with patch("handlers.deny_list._table", return_value=mock_table):
        response = list_blocked_numbers(_event(qs={}), {})

    body = json.loads(response["body"])
    assert body["count"] == 2
    assert body["blockedNumbers"][0]["phoneNumber"] == "+12222222222"


def test_list_blocked_numbers_defaults_source_for_legacy_rows():
    from handlers.deny_list import list_blocked_numbers

    mock_table = MagicMock()
    mock_table.scan.return_value = {
        "Items": [{"ContactNumber": "+11111111111", "addedAt": "2026-01-01T00:00:00Z"}]
    }

    with patch("handlers.deny_list._table", return_value=mock_table):
        response = list_blocked_numbers(_event(qs={}), {})

    body = json.loads(response["body"])
    assert body["blockedNumbers"][0]["source"] == "legacy"


def test_list_blocked_numbers_paginates_before_sorting():
    """A Scan(Limit=N) that truncates before sorting can silently omit the
    true newest row — this must follow LastEvaluatedKey across pages before
    sorting/truncating, not sort only the first page."""
    from handlers.deny_list import list_blocked_numbers

    mock_table = MagicMock()
    mock_table.scan.side_effect = [
        {
            "Items": [
                {"ContactNumber": "+11111111111", "addedAt": "2026-01-01T00:00:00Z"}
            ],
            "LastEvaluatedKey": {"ContactNumber": "+11111111111"},
        },
        {
            "Items": [
                {"ContactNumber": "+12222222222", "addedAt": "2026-09-09T00:00:00Z"}
            ],
        },
    ]

    with patch("handlers.deny_list._table", return_value=mock_table):
        response = list_blocked_numbers(_event(qs={"limit": "1"}), {})

    body = json.loads(response["body"])
    assert mock_table.scan.call_count == 2
    second_call_kwargs = mock_table.scan.call_args_list[1].kwargs
    assert second_call_kwargs["ExclusiveStartKey"] == {"ContactNumber": "+11111111111"}
    # limit=1 must return the TRUE newest row across both pages, not just
    # whatever the first page happened to contain.
    assert body["count"] == 1
    assert body["blockedNumbers"][0]["phoneNumber"] == "+12222222222"


def test_list_blocked_numbers_rejects_non_integer_limit():
    from handlers.deny_list import list_blocked_numbers

    with pytest.raises(ValueError, match="limit must be an integer"):
        list_blocked_numbers(_event(qs={"limit": "not-a-number"}), {})


def test_list_blocked_numbers_clamps_non_positive_limit_to_one():
    from handlers.deny_list import list_blocked_numbers

    mock_table = MagicMock()
    mock_table.scan.return_value = {
        "Items": [
            {"ContactNumber": "+11111111111", "addedAt": "2026-01-01T00:00:00Z"},
            {"ContactNumber": "+12222222222", "addedAt": "2026-06-01T00:00:00Z"},
        ]
    }

    with patch("handlers.deny_list._table", return_value=mock_table):
        response = list_blocked_numbers(_event(qs={"limit": "0"}), {})

    body = json.loads(response["body"])
    assert body["count"] == 1
