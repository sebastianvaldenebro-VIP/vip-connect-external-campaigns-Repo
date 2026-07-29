"""Tests for _validate_sms_campaign PHI guard and field validation."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

_stub_modules = {
    "store": MagicMock(),
    "scheduler_manager": MagicMock(),
    "vip_shared": MagicMock(),
    "vip_shared.application": MagicMock(),
    "vip_shared.application.http": MagicMock(),
    "vip_shared.infrastructure": MagicMock(),
    "vip_shared.infrastructure.persistence": MagicMock(),
    "vip_shared.infrastructure.persistence.audit": MagicMock(),
}
with patch.dict(sys.modules, _stub_modules):
    with patch("boto3.client"), patch("boto3.resource"):
        from handlers import plans as plans_handler  # noqa: E402

_validate = plans_handler._validate_sms_campaign


def _campaign(
    template: str = "Your appointment is confirmed. Reply STOP to opt out.",
    origination_arn: str = "arn:aws:sms-voice:us-east-1:123:phone-number/p-1",
    phi_acknowledged: bool = True,
) -> dict:
    return {
        "deliveryType": "sms",
        "campaignConfig": {
            "smsMessageTemplate": template,
            "smsOriginationNumberArn": origination_arn,
            "phiAcknowledged": phi_acknowledged,
        },
    }


# ── Valid templates ───────────────────────────────────────────────────────────


def test_valid_generic_template_passes():
    errors = _validate(_campaign(), "bucket-1", 0)
    assert errors == []


def test_valid_template_with_generic_date_passes():
    errors = _validate(_campaign("Reminder: tomorrow at 10am. Reply STOP to opt out."), "b", 0)
    assert errors == []


# ── Missing / too-long template ───────────────────────────────────────────────


def test_missing_template_errors():
    c = _campaign(template="")
    errors = _validate(c, "bucket-1", 0)
    assert any("smsMessageTemplate" in e for e in errors)


def test_template_over_160_chars_errors():
    long_tmpl = "A" * 161
    errors = _validate(_campaign(template=long_tmpl), "bucket-1", 0)
    assert any("160" in e for e in errors)


def test_template_exactly_160_chars_passes():
    tmpl = "A" * 160
    errors = _validate(_campaign(template=tmpl), "bucket-1", 0)
    assert not any("160" in e for e in errors)


# ── PHI pattern detection ─────────────────────────────────────────────────────


def test_ssn_dash_format_blocked():
    errors = _validate(_campaign(template="Your SSN 123-45-6789 is on file."), "b", 0)
    assert any("PHI" in e or "SSN" in e for e in errors)


def test_ssn_space_format_blocked():
    errors = _validate(_campaign(template="SSN 123 45 6789"), "b", 0)
    assert any("PHI" in e or "SSN" in e for e in errors)


def test_email_blocked():
    errors = _validate(_campaign(template="Contact us at patient@example.com."), "b", 0)
    assert any("PHI" in e or "email" in e for e in errors)


def test_date_with_day_month_blocked():
    errors = _validate(_campaign(template="DOB 01/15/1985 on file."), "b", 0)
    assert any("PHI" in e or "date" in e for e in errors)


def test_date_dash_format_blocked():
    errors = _validate(_campaign(template="01-15-2000"), "b", 0)
    assert any("PHI" in e or "date" in e for e in errors)


def test_long_numeric_id_blocked():
    errors = _validate(_campaign(template="Account 1234567890"), "b", 0)
    assert any("PHI" in e or "numeric" in e.lower() or "MRN" in e for e in errors)


def test_clinical_term_diagnosis_blocked():
    errors = _validate(_campaign(template="Your diagnosis has been updated."), "b", 0)
    assert any("PHI" in e or "clinical" in e for e in errors)


def test_clinical_term_medication_blocked():
    errors = _validate(_campaign(template="Your medication is ready."), "b", 0)
    assert any("PHI" in e or "clinical" in e for e in errors)


def test_clinical_term_prescribed_blocked():
    errors = _validate(_campaign(template="You have been prescribed treatment."), "b", 0)
    assert any("PHI" in e or "clinical" in e for e in errors)


def test_clinical_term_condition_blocked():
    errors = _validate(_campaign(template="Your condition update."), "b", 0)
    assert any("PHI" in e or "clinical" in e for e in errors)


# ── Missing required fields ───────────────────────────────────────────────────


def test_missing_origination_arn_errors():
    c = _campaign(origination_arn="")
    errors = _validate(c, "b", 0)
    assert any("smsOriginationNumberArn" in e for e in errors)


def test_phi_not_acknowledged_errors():
    errors = _validate(_campaign(phi_acknowledged=False), "b", 0)
    assert any("phiAcknowledged" in e for e in errors)


def test_phi_acknowledged_true_required_true_passes():
    errors = _validate(_campaign(phi_acknowledged=True), "b", 0)
    assert not any("phiAcknowledged" in e for e in errors)


# ── Multiple errors returned ──────────────────────────────────────────────────


def test_all_fields_missing_returns_multiple_errors():
    c = {"deliveryType": "sms", "campaignConfig": {}}
    errors = _validate(c, "b", 0)
    assert len(errors) >= 3  # template, arn, phi_acknowledged


# ── _validate_plan_body integration ──────────────────────────────────────────


def test_plan_body_with_valid_sms_campaign_passes():
    plan = {
        "buckets": [
            {
                "name": "Bucket A",
                "campaigns": [
                    {
                        "deliveryType": "sms",
                        "campaignConfig": {
                            "smsMessageTemplate": "Your appointment is confirmed. Reply STOP to opt out.",
                            "smsOriginationNumberArn": "arn:aws:sms-voice:us-east-1:123:phone-number/p-1",
                            "phiAcknowledged": True,
                        },
                    }
                ],
            }
        ]
    }
    errors = plans_handler._validate_plan_body(plan)
    assert errors == []


def test_plan_body_with_phi_in_sms_template_blocked():
    plan = {
        "buckets": [
            {
                "name": "Bucket A",
                "campaigns": [
                    {
                        "deliveryType": "sms",
                        "campaignConfig": {
                            "smsMessageTemplate": "SSN 123-45-6789 on file.",
                            "smsOriginationNumberArn": "arn:aws:sms-voice:us-east-1:123:phone-number/p-1",
                            "phiAcknowledged": True,
                        },
                    }
                ],
            }
        ]
    }
    errors = plans_handler._validate_plan_body(plan)
    assert any("PHI" in e for e in errors)


def test_plan_body_with_non_sms_campaign_not_validated_by_sms_validator():
    plan = {
        "buckets": [
            {
                "name": "Bucket A",
                "campaigns": [
                    {"deliveryType": "campaign"},
                ]
            }
        ]
    }
    errors = plans_handler._validate_plan_body(plan)
    assert errors == []


# ── H-D1: ISO date, URL, placeholder patterns ─────────────────────────────────


def test_iso_date_dob_blocked():
    errors = _validate(_campaign(template="DOB 1985-01-15 on file."), "b", 0)
    assert any("PHI" in e or "ISO date" in e for e in errors)


def test_url_blocked():
    errors = _validate(_campaign(template="Visit https://patient.portal.com for results."), "b", 0)
    assert any("PHI" in e or "URL" in e for e in errors)


def test_double_brace_placeholder_blocked():
    errors = _validate(_campaign(template="Hello {{firstName}}, your appointment is ready."), "b", 0)
    assert any("PHI" in e or "placeholder" in e for e in errors)


def test_dollar_brace_placeholder_blocked():
    errors = _validate(_campaign(template="Hello ${firstName}, your appointment is ready."), "b", 0)
    assert any("PHI" in e or "placeholder" in e for e in errors)


def test_generic_template_with_year_only_passes():
    errors = _validate(_campaign(template="Your 2026 plan benefits are active. Reply STOP to opt out."), "b", 0)
    assert errors == []
