"""Tests for _validate_sms_campaign PHI guard and field validation."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

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

from vip_shared.domain.services.sms_template import max_rendered_length  # noqa: E402

_validate = plans_handler._validate_sms_campaign
_MAX_SMS_CHARS = plans_handler._MAX_SMS_CHARS
validate_plan = plans_handler._validate_plan_body


def _campaign(
    template: str = "Your appointment is confirmed. Reply STOP to opt out.",
    origination_arn: str = "arn:aws:sms-voice:us-east-1:123:phone-number/p-1",
    phi_acknowledged: bool = True,
    clinic_name: str | None = None,
) -> dict:
    cfg = {
        "smsMessageTemplate": template,
        "smsOriginationNumberArn": origination_arn,
        "phiAcknowledged": phi_acknowledged,
    }
    if clinic_name is not None:
        cfg["clinicName"] = clinic_name
    return {
        "deliveryType": "sms",
        "campaignConfig": cfg,
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
    """Stays rejected after the allowlist narrowing: the allowlist is
    case-sensitive ("FirstName"), so lowercase "firstName" is not an allowed
    field — this is deliberate case-sensitivity, not an oversight to "fix"."""
    errors = _validate(_campaign(template="Hello {{firstName}}, your appointment is ready."), "b", 0)
    assert any("PHI" in e or "placeholder" in e for e in errors)


def test_dollar_brace_placeholder_blocked():
    """${...} stays banned outright — no renderer supports it, unlike {{...}}."""
    errors = _validate(_campaign(template="Hello ${firstName}, your appointment is ready."), "b", 0)
    assert any("PHI" in e or "placeholder" in e for e in errors)


def test_generic_template_with_year_only_passes():
    errors = _validate(_campaign(template="Your 2026 plan benefits are active. Reply STOP to opt out."), "b", 0)
    assert errors == []


# ── Task 3: placeholder allowlist narrowing (renderer now exists) ────────────


def test_allowlisted_placeholders_are_accepted():
    """{{ClinicName}} is allowlisted, but bulk-SMS's campaignConfig has no
    clinicName set here — an unset value would render {{ClinicName}} as an
    empty string ("This is ." shipped to a patient), so this must now be
    rejected by the same clinicName-required guard _validate_precall_sms
    already has. See test_allowlisted_placeholders_are_accepted_with_clinicname_set
    for the corresponding accept path."""
    errors = _validate(
        _campaign(
            template="Hi {{FirstName}}! This is {{ClinicName}}, calling shortly."
        ),
        "b",
        0,
    )
    assert any("clinicName" in e for e in errors)


def test_allowlisted_placeholders_are_accepted_with_clinicname_set():
    errors = _validate(
        _campaign(
            template="Hi {{FirstName}}! This is {{ClinicName}}, calling shortly.",
            clinic_name="VIP Medical Group",
        ),
        "b",
        0,
    )
    assert errors == []


def test_bulk_sms_requires_clinicname_when_template_uses_the_placeholder():
    """Mirrors _validate_precall_sms's identical guard: bulk-SMS's
    campaignConfig has no clinicName field anywhere in the frontend or
    executor invocation, so an unset value always renders empty."""
    errors = _validate(
        _campaign(template="This is {{ClinicName}} calling."),
        "b",
        0,
    )
    assert any("clinicName" in e for e in errors)


def test_bulk_sms_does_not_require_clinicname_when_template_omits_the_placeholder():
    errors = _validate(_campaign(template="Hi {{FirstName}}, quick reminder."), "b", 0)
    assert not any("clinicName" in e for e in errors)


def test_non_allowlisted_placeholder_is_rejected_by_name():
    errors = _validate(
        _campaign(template="Your {{Diagnosis}} result is ready."), "b", 0
    )
    assert any("Diagnosis" in e for e in errors)


def test_lastname_is_not_allowlisted():
    errors = _validate(_campaign(template="Hi {{FirstName}} {{LastName}}!"), "b", 0)
    assert any("LastName" in e for e in errors)


def test_dollar_brace_syntax_stays_banned():
    """No renderer supports ${...}; it can only be a mistake."""
    errors = _validate(_campaign(template="Hi ${FirstName}!"), "b", 0)
    assert errors != []


def test_phi_disguised_inside_malformed_braces_is_still_caught():
    """{{123-45-6789}} is not a well-formed \\w+ placeholder token — it must not
    be stripped by the PHI-scan's brace-removal step, or the SSN inside it
    would sail past the PHI regexes and reach render() untouched (since
    render()'s own _PLACEHOLDER_RE would also skip it), landing verbatim in
    the outbound SMS. Confirmed via live repro: the old `{{[^}]+}}` strip
    regex in this function was broader than extract_placeholders'/render's
    `\\{\\{\\s*(\\w+)\\s*\\}\\}`, so it silently deleted this from the
    scannable text before any PHI pattern ever saw it."""
    errors = _validate(
        _campaign(template="Your SSN is {{123-45-6789}}, please confirm."),
        "b",
        0,
    )
    assert any("PHI" in e or "SSN" in e for e in errors)


def test_email_disguised_inside_malformed_braces_is_still_caught():
    errors = _validate(
        _campaign(template="Contact {{jane@example.com}} for details."),
        "b",
        0,
    )
    assert any("PHI" in e or "email" in e for e in errors)


def test_precall_phi_disguised_inside_malformed_braces_is_still_caught():
    """Same guard via _validate_precall_sms's shared _screen_sms_template_content
    call — the pre-call channel must not diverge from bulk-SMS here."""
    plan = _plan_with_precall(
        precall={
            "enabled": True,
            "messageTemplate": "Hi {{FirstName}}, your SSN is {{123-45-6789}}.",
            "clinicName": "VIP Medical Group",
            "originationNumberArn": "arn:x",
        }
    )
    errors = validate_plan(plan)
    assert any("PHI" in e or "SSN" in e for e in errors)


def test_other_phi_patterns_still_enforced_alongside_placeholders():
    """Narrowing the placeholder rule must not weaken the eight non-placeholder
    patterns (SSN, email, dates, long numeric IDs, URLs, clinical terms, ...)."""
    for bad in (
        "Hi {{FirstName}}, ssn 123-45-6789",
        "Hi {{FirstName}}, see https://x.co",
        "Hi {{FirstName}}, your diagnosis is ready",
        "Hi {{FirstName}}, acct 12345678",
        "Hi {{FirstName}}, on 01/02/2026",
    ):
        assert _validate(_campaign(template=bad), "b", 0) != [], bad


def test_length_is_measured_on_the_rendered_worst_case():
    """158 raw chars passes a raw check but renders to 165, over the ceiling.

    "Hi {{FirstName}}! " is 18 chars, of which the placeholder is 13; at the
    20-char name budget the prefix becomes 25, so 25 + 140 = 165 > 160.
    A raw len() check would have accepted this template.
    """
    tmpl = "Hi {{FirstName}}! " + "x" * 140
    errors = _validate(_campaign(template=tmpl), "b", 0)
    assert any("160" in e for e in errors)


# ── PHI guard also screens the RESOLVED clinicName value, not just the raw ──
# ── template with placeholders stripped ──────────────────────────────────────


def test_bulk_sms_clinicname_containing_ssn_is_rejected_even_with_clean_template():
    """_screen_sms_template_content must scan the resolved clinicName VALUE,
    not just the template with placeholders stripped. clinicName is free text
    with no character-class restriction (unlike FirstName, which
    _clean_first_name/_NAME_OK_RE constrain), so an operator could put an SSN
    directly into campaignConfig.clinicName and it would never appear in the
    raw template text — only in the value substituted at send time."""
    errors = _validate(
        _campaign(
            template="Reminder: your appointment is confirmed. Reply STOP to opt out.",
            clinic_name="123-45-6789",
        ),
        "b",
        0,
    )
    assert any("PHI" in e or "SSN" in e for e in errors)


def test_bulk_sms_clinicname_containing_email_is_rejected():
    errors = _validate(
        _campaign(
            template="Reminder: your appointment is confirmed. Reply STOP to opt out.",
            clinic_name="jane@example.com",
        ),
        "b",
        0,
    )
    assert any("PHI" in e or "email" in e for e in errors)


def test_bulk_sms_legitimate_clinicname_still_passes_cleanly():
    """No false positive: a real clinic name must not itself trip the PHI scan
    just because it is now included in what gets screened."""
    errors = _validate(
        _campaign(
            template="Reminder: your appointment is confirmed. Reply STOP to opt out.",
            clinic_name="VIP Medical Group",
        ),
        "b",
        0,
    )
    assert errors == []


def test_precall_clinicname_containing_ssn_is_rejected_even_with_clean_template():
    """Same guard via _validate_precall_sms's shared _screen_sms_template_content
    call — precallSms.clinicName is the pre-call channel's own equivalent
    free-text field and must not be a PHI side-channel either."""
    plan = _plan_with_precall(
        precall={
            "enabled": True,
            "messageTemplate": "Hi {{FirstName}}, calling shortly.",
            "clinicName": "123-45-6789",
            "originationNumberArn": "arn:x",
        }
    )
    assert any("PHI" in e or "SSN" in e for e in validate_plan(plan))


def test_precall_clinicname_containing_email_is_rejected():
    plan = _plan_with_precall(
        precall={
            "enabled": True,
            "messageTemplate": "Hi {{FirstName}}, calling shortly.",
            "clinicName": "jane@example.com",
            "originationNumberArn": "arn:x",
        }
    )
    assert any("PHI" in e or "email" in e for e in validate_plan(plan))


def test_precall_legitimate_clinicname_still_passes_cleanly():
    """No false positive on the precall path either — mirrors
    test_bulk_sms_legitimate_clinicname_still_passes_cleanly."""
    plan = _plan_with_precall(
        precall={
            "enabled": True,
            "messageTemplate": "Hi {{FirstName}}, calling shortly.",
            "clinicName": "VIP Medical Group",
            "originationNumberArn": "arn:x",
        }
    )
    assert validate_plan(plan) == []


# ── Task 5: precall SMS config validation (_validate_precall_sms) ────────────


def _plan_with_precall(
    precall: dict | None = None,
    delivery_type: str = "campaign",
    depends_on: list[str] | None = None,
) -> dict:
    """Build a minimal single-bucket plan around one campaign carrying a
    campaignConfig.precallSms block.

    Default `precall`, when None, is the valid business-approved Pain
    Management config (see the plan's Task 5 "Origination number" example) —
    not merely present but valid — so test_valid_precall_campaign_has_no_errors
    exercises the real accept path rather than a false negative.
    """
    if precall is None:
        precall = {
            "enabled": True,
            "messageTemplate": (
                "Hi {{FirstName}}! {{ClinicName}} here. We're calling you in "
                "just a moment to discuss your pain management request. Talk soon!"
            ),
            "clinicName": "VIP Medical Group",
            "originationNumberArn": "arn:aws:sms-voice:us-east-1:165505826690:phone-number/phone-ba711707215947e3a0e5112c0872014b",
        }
    campaign: dict = {
        "id": "c1",
        "name": "Campaign",
        "deliveryType": delivery_type,
        "campaignConfig": {"precallSms": precall},
    }
    if depends_on is not None:
        campaign["dependsOn"] = depends_on
    return {
        "name": "Test Plan",
        "buckets": [
            {
                "name": "Bucket A",
                "campaigns": [campaign],
            }
        ],
    }


def test_precall_requires_template_when_enabled():
    plan = _plan_with_precall(precall={"enabled": True})
    assert any("messageTemplate" in e for e in validate_plan(plan))


def test_precall_requires_origination_number_when_enabled():
    plan = _plan_with_precall(precall={"enabled": True, "messageTemplate": "Hi!"})
    assert any("originationNumberArn" in e for e in validate_plan(plan))


def test_precall_requires_clinic_name_if_template_uses_it():
    """An unset config value renders as an empty string — 'This is .' shipped
    to a patient. Require the value whenever the placeholder is present."""
    plan = _plan_with_precall(
        precall={
            "enabled": True,
            "messageTemplate": "Hi {{FirstName}}! This is {{ClinicName}}.",
            "originationNumberArn": "arn:x",
            "clinicName": "",
        }
    )
    assert any("clinicName" in e for e in validate_plan(plan))


def test_precall_template_goes_through_the_same_phi_guard():
    plan = _plan_with_precall(
        precall={
            "enabled": True,
            "messageTemplate": "Hi {{FirstName}}, your {{Diagnosis}} is ready.",
            "originationNumberArn": "arn:x",
        }
    )
    assert any("Diagnosis" in e for e in validate_plan(plan))


def test_precall_is_rejected_on_a_non_voice_campaign():
    """precallSms on an SMS campaign is nonsense — there is no dial to precede."""
    plan = _plan_with_precall(delivery_type="sms")
    assert any("precallSms" in e for e in validate_plan(plan))


def test_precall_is_rejected_when_the_campaign_has_dependsOn():
    """A campaign with dependsOn is never pre-warmed (executor.py:2180, 2571-2573),
    so it has no segmentArn at activation and the pre-call SMS would silently
    never fire. Reject at save time instead of failing quietly at run time."""
    plan = _plan_with_precall(depends_on=["other"])
    assert any("dependsOn" in e for e in validate_plan(plan))


# The literal approved strings, not a paraphrase. If these ever diverge from the
# business document the test is worthless, so keep them verbatim and dated.
_APPROVED_COPY_2026_09_10 = {
    "Vein": (
        "Hi {{FirstName}}! This is {{ClinicName}}. We're about to give you a "
        "quick call regarding your vein consultation request. "
        "Look out for our call!"
    ),
    "Pain": (
        "Hi {{FirstName}}! {{ClinicName}} here. We're calling you in just a "
        "moment to discuss your pain management request. Talk soon!"
    ),
}


def test_approved_pain_copy_renders_within_the_length_ceiling():
    """Pain fits at the 20-char worst case: 135 rendered."""
    rendered = max_rendered_length(
        _APPROVED_COPY_2026_09_10["Pain"],
        campaign={"clinicName": "VIP Medical Group"},
    )
    assert rendered == 135
    assert rendered <= _MAX_SMS_CHARS


def test_approved_vein_copy_renders_within_the_length_ceiling():
    """Vein fits at the 20-char worst case: 153 rendered.

    This is the SHORTENED closing Sebastian approved on 2026-09-10 (OQ-9,
    option 3): "Look out for our call!". His first draft ended "Look out for a
    call from this number!", which rendered 168 and did not fit. Asserting the
    exact number, not just <= the ceiling, so restoring the longer closing fails
    here instead of failing silently at the API boundary.
    """
    rendered = max_rendered_length(
        _APPROVED_COPY_2026_09_10["Vein"],
        campaign={"clinicName": "VIP Medical Group"},
    )
    assert rendered == 153
    assert rendered <= _MAX_SMS_CHARS


def test_both_approved_templates_pass_the_real_validator():
    """The measurements above are worthless if the validator disagrees."""
    for specialty, tmpl in _APPROVED_COPY_2026_09_10.items():
        plan = _plan_with_precall(
            precall={
                "enabled": True,
                "messageTemplate": tmpl,
                "clinicName": "VIP Medical Group",
                "originationNumberArn": "arn:x",
            }
        )
        assert validate_plan(plan) == [], specialty


def test_approved_copy_is_pure_gsm7():
    """A curly apostrophe (U+2019) instead of ASCII ' silently forces UCS-2
    encoding, which cuts the per-segment budget from 160 to 70 — Vein would
    split into three segments and Pain into two, and this plan's whole length
    analysis would be wrong. Copy pasted out of a Word/Google doc is the usual
    source.
    """
    for specialty, tmpl in _APPROVED_COPY_2026_09_10.items():
        assert "’" not in tmpl, specialty
        assert "‘" not in tmpl, specialty
        assert "“" not in tmpl and "”" not in tmpl, specialty
        assert "—" not in tmpl and "–" not in tmpl, specialty
        assert "…" not in tmpl, specialty


def test_specialty_placeholder_is_not_allowlisted():
    """No approved template uses {{Specialty}}, so it is not interpolatable and a
    template using it must be rejected like any other unknown field."""
    plan = _plan_with_precall(
        precall={
            "enabled": True,
            "messageTemplate": "Hi {{FirstName}}, about your {{Specialty}} visit.",
            "originationNumberArn": "arn:x",
            "clinicName": "VIP Medical Group",
        }
    )
    assert any("Specialty" in e for e in validate_plan(plan))


def test_valid_precall_campaign_has_no_errors():
    assert validate_plan(_plan_with_precall()) == []


def test_valid_precall_campaign_has_no_errors_on_journey_delivery_type():
    """journey places a dial just like campaign/branded (executor.py:4275,
    4704 resolve_journey_flow_arn and its dedicated dial-flow-resolution
    logic) — a journey campaign with a valid precallSms block must be
    ACCEPTED, not rejected as having "no dial for it to precede"."""
    assert validate_plan(_plan_with_precall(delivery_type="journey")) == []


def _content_plan(template: str, clinic: str, channel: str) -> dict:
    if channel == "precall":
        return _plan_with_precall(precall={
            "enabled": True, "messageTemplate": template,
            "clinicName": clinic, "originationNumberArn": "arn:x",
        })
    return {"buckets": [{"name": "B", "campaigns": [_campaign(template, clinic_name=clinic)]}]}


@pytest.mark.parametrize("channel", ["precall", "bulk"])
@pytest.mark.parametrize("template,clinic", [
    ("Your identifier is 123-{{ClinicName}}-6789.", "45"),
    ("Contact jane@{{ClinicName}}.", "example.com"),
    ("Your visit {{ClinicName}}/2000", "01/15"),
    ("Visit {{ClinicName}}s://example.com", "http"),
    ("Account 123{{ClinicName}}", "4567"),
])
def test_rejects_phi_assembled_across_clinic_placeholder_boundary(channel, template, clinic):
    errors = validate_plan(_content_plan(template, clinic, channel))
    assert any("PHI" in error for error in errors)


@pytest.mark.parametrize("channel", ["precall", "bulk"])
@pytest.mark.parametrize("template", [
    "{{First Name}}", "{{First-Name}}", "{{}}", "{{   }}",
    "{{FirstName", "FirstName}}", "{{FirstName}", "{FirstName}}",
    "{{{FirstName}}}", "{{FirstName}}}", "{{{FirstName}}",
    "{{outer {{FirstName}} }}", "{{FirstName}}{{}}", "{{Unknown}}",
])
def test_plan_rejects_unresolved_placeholder_expressions(channel, template):
    errors = validate_plan(_content_plan(template, "VIP", channel))
    assert any("placeholder" in error for error in errors)


@pytest.mark.parametrize("channel", ["precall", "bulk"])
@pytest.mark.parametrize("clinic", ["{{FirstName}}", "{{Unknown}}", "{{First Name}}", "{{", "}}"])
def test_plan_rejects_placeholder_expressions_inserted_by_clinic(channel, clinic):
    errors = validate_plan(_content_plan("Hi {{ClinicName}}", clinic, channel))
    assert any("placeholder" in error for error in errors)


@pytest.mark.parametrize("channel", ["precall", "bulk"])
def test_plan_keeps_supported_spacing_adjacent_tokens_and_single_braces(channel):
    assert validate_plan(_content_plan("{ Hi {{ FirstName }}{{ ClinicName }} }", "VIP", channel)) == []


@pytest.mark.parametrize("channel", ["precall", "bulk"])
@pytest.mark.parametrize("clinic", ["   ", "\t\n"])
def test_whitespace_clinic_is_rejected_when_interpolated(channel, clinic):
    errors = validate_plan(_content_plan("Hi from {{ClinicName}}.", clinic, channel))
    assert any("clinicName" in error for error in errors)


@pytest.mark.parametrize("channel", ["precall", "bulk"])
def test_unused_whitespace_clinic_does_not_block_a_valid_template(channel):
    assert validate_plan(_content_plan("Hi {{FirstName}}!", "   ", channel)) == []


def _profile_precall(**overrides):
    return {
        "enabled": True,
        "mode": "profile",
        "catalogVersion": "phase1-v1",
        "originationNumberArn": "arn:x",
        **overrides,
    }


@pytest.mark.parametrize("delivery_type", ["campaign", "journey", "branded"])
def test_profile_mode_accepts_embedded_copy_without_manual_fields(delivery_type):
    campaign = {
        "deliveryType": delivery_type,
        "campaignConfig": {"precallSms": _profile_precall()},
    }
    assert plans_handler._validate_precall_sms(campaign, "b", 0) == []


def test_profile_mode_preserves_campaign_dependencies():
    campaign = {
        "deliveryType": "campaign",
        "dependsOn": ["parent-campaign"],
        "campaignConfig": {"precallSms": _profile_precall()},
    }
    assert plans_handler._validate_precall_sms(campaign, "b", 0) == []
    assert campaign["dependsOn"] == ["parent-campaign"]


@pytest.mark.parametrize("version", [None, "", "phase1-v2", [], 1])
def test_profile_mode_rejects_missing_or_unknown_catalog_version(version):
    errors = validate_plan(_plan_with_precall(
        precall=_profile_precall(catalogVersion=version),
    ))
    assert any("catalogVersion" in error for error in errors)


@pytest.mark.parametrize("mode", [None, "", "automatic", [], {}, True])
def test_enabled_precall_rejects_unknown_mode_without_crashing(mode):
    errors = validate_plan(_plan_with_precall(precall=_profile_precall(mode=mode)))
    assert any("precallSms.mode" in error for error in errors)


def test_profile_mode_rejects_manual_template_override_instead_of_ignoring_it():
    errors = validate_plan(_plan_with_precall(precall=_profile_precall(
        messageTemplate="Use a different specialty for everyone",
    )))
    assert any("messageTemplate" in error and "profile mode" in error for error in errors)


@pytest.mark.parametrize("clinic", ["Example Clinic", "  Clínica del Valle  ", "", " \t\n", "A" * 80])
def test_profile_mode_accepts_optional_clinic_with_embedded_template(clinic):
    assert validate_plan(_plan_with_precall(
        precall=_profile_precall(clinicName=clinic),
    )) == []


@pytest.mark.parametrize("clinic", [
    None, False, 0, 123, [], {}, "A" * 81, "{{FirstName}}", "{{Unknown}}",
    "someone@example.com", "https://example.com", "123-45-6789", "Clinic\nName",
])
def test_profile_mode_rejects_invalid_optional_clinic_before_execution(clinic):
    errors = validate_plan(_plan_with_precall(precall=_profile_precall(clinicName=clinic)))
    assert any("clinicName" in error for error in errors)
    # Validation errors must describe the field, not echo its supplied value.
    if isinstance(clinic, str):
        assert all(clinic not in error for error in errors)


@pytest.mark.parametrize("origin", [None, "", "   ", 123, {}])
def test_profile_mode_requires_a_nonblank_origin(origin):
    errors = validate_plan(_plan_with_precall(
        precall=_profile_precall(originationNumberArn=origin),
    ))
    assert any("originationNumberArn" in error for error in errors)


def test_disabled_profile_configuration_remains_inert():
    assert validate_plan(_plan_with_precall(
        precall=_profile_precall(enabled=False, mode="future", catalogVersion="future"),
        depends_on=["parent-campaign"],
    )) == []


@pytest.mark.parametrize("enabled", [1, "true", "false", [True], {"enabled": True}])
def test_profile_mode_requires_real_boolean_opt_in(enabled):
    errors = validate_plan(_plan_with_precall(precall=_profile_precall(enabled=enabled)))
    assert any("enabled must be a boolean" in error for error in errors)


def test_profile_mode_does_not_allow_precall_on_bulk_sms():
    errors = plans_handler._validate_precall_sms({
        "deliveryType": "sms",
        "campaignConfig": {"precallSms": _profile_precall()},
    }, "b", 0)
    assert any("only valid on a voice" in error for error in errors)


def test_explicit_manual_mode_retains_existing_length_and_dependency_rules():
    plan = _plan_with_precall(precall={
        "enabled": True, "mode": "manual", "originationNumberArn": "arn:x",
        "messageTemplate": "x" * 161,
    }, depends_on=["parent-campaign"])
    errors = validate_plan(plan)
    assert any("160" in error for error in errors)
    assert any("dependsOn" in error for error in errors)


@pytest.mark.parametrize("precall", ["profile", [{"enabled": True}], True, 1, 1.5])
def test_create_plan_rejects_non_object_precall_before_persisting(precall, monkeypatch):
    """A malformed request must raise the ValueError mapped to HTTP 400."""
    body = {
        "name": "Example plan",
        "buckets": [{
            "name": "Example bucket",
            "campaigns": [{
                "deliveryType": "campaign",
                "campaignConfig": {"precallSms": precall},
            }],
        }],
    }
    monkeypatch.setattr(plans_handler, "parse_body", lambda event: body)
    monkeypatch.setattr(plans_handler, "extract_caller", lambda event: MagicMock())
    put_plan = MagicMock()
    monkeypatch.setattr(plans_handler.store, "put_plan", put_plan)

    with pytest.raises(ValueError, match=r"precallSms must be an object"):
        plans_handler.create_plan({}, {})

    put_plan.assert_not_called()


@pytest.mark.parametrize("precall", [None, {}, {"enabled": False}])
def test_inert_precall_blocks_keep_their_existing_validation_contract(precall):
    assert plans_handler._validate_precall_sms({
        "campaignConfig": {"precallSms": precall},
    }, "Example bucket", 0) == []
