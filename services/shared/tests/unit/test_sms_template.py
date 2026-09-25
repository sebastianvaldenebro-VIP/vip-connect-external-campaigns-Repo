"""Tests for pre-call SMS template rendering with a scoped placeholder allowlist."""

from __future__ import annotations

import pytest

from vip_shared.domain.services.sms_template import (
    CAMPAIGN_FIELDS,
    RECIPIENT_FIELDS,
    extract_placeholders,
    max_rendered_length,
    render,
    strip_placeholders,
)

_CAMPAIGN = {"clinicName": "VIP Medical Group"}


def test_allowlists_are_narrow_and_explicit():
    """Guard against scope creep: widening these sets is a PHI decision."""
    assert RECIPIENT_FIELDS == {"FirstName"}
    assert CAMPAIGN_FIELDS == {"ClinicName"}


def test_extract_finds_all_placeholders():
    """extract_placeholders is deliberately allowlist-blind — it reports what the
    template contains so validation can diff against ALLOWED_FIELDS and name the
    offender. {{Specialty}} is used here precisely because it is NOT allowlisted."""
    tmpl = "Hi {{FirstName}}! This is {{ClinicName}} about {{Specialty}}."
    assert extract_placeholders(tmpl) == {"FirstName", "ClinicName", "Specialty"}


def test_renders_recipient_and_campaign_fields():
    out = render(
        "Hi {{FirstName}}! This is {{ClinicName}} about your visit.",
        recipient={"FirstName": "Maria"},
        campaign=_CAMPAIGN,
    )
    assert out == "Hi Maria! This is VIP Medical Group about your visit."


def test_missing_first_name_uses_neutral_fallback():
    """Never deliver 'Hi !' or a literal placeholder when CP has no name."""
    out = render("Hi {{FirstName}}!", recipient={}, campaign=_CAMPAIGN)
    assert out == "Hi there!"
    assert "{{" not in out


@pytest.mark.parametrize("junk", ["", "   ", None])
def test_blank_first_name_uses_fallback(junk):
    out = render("Hi {{FirstName}}!", recipient={"FirstName": junk}, campaign=_CAMPAIGN)
    assert out == "Hi there!"


def test_name_is_normalized_to_title_case():
    out = render(
        "Hi {{FirstName}}!", recipient={"FirstName": "  mARIA  "}, campaign=_CAMPAIGN
    )
    assert out == "Hi Maria!"


def test_junk_name_containing_digits_falls_back():
    """CP records contain test rows like 'TEST 12345'. Interpolating that would
    inject a long numeric string into an SMS — exactly what the MRN pattern in
    the plan-side PHI guard exists to prevent."""
    out = render(
        "Hi {{FirstName}}!", recipient={"FirstName": "TEST 12345"}, campaign=_CAMPAIGN
    )
    assert out == "Hi there!"


def test_absurdly_long_name_is_truncated():
    out = render(
        "Hi {{FirstName}}!", recipient={"FirstName": "A" * 200}, campaign=_CAMPAIGN
    )
    assert len(out) < 60


def test_unknown_placeholder_is_never_rendered():
    """Defense in depth: validation should have rejected this template, but the
    renderer must not silently interpolate an unlisted field if one slips in."""
    with pytest.raises(ValueError, match="Diagnosis"):
        render("Your {{Diagnosis}}", recipient={"Diagnosis": "x"}, campaign=_CAMPAIGN)


def test_max_rendered_length_budgets_for_the_longest_realistic_name():
    """A 140-char template with {{FirstName}} can exceed 160 once rendered."""
    tmpl = "Hi {{FirstName}}! " + ("x" * 140)
    assert max_rendered_length(tmpl, campaign=_CAMPAIGN) > len(tmpl)


def test_strip_placeholders_removes_only_well_formed_tokens():
    """strip_placeholders must strip EXACTLY what extract_placeholders/render()
    recognize as a placeholder — no broader, no narrower — so PHI disguised
    inside malformed braces (not a \\w+ token) stays in the string for the
    caller's PHI scanner to catch, instead of being silently discarded."""
    out = strip_placeholders("Hi {{FirstName}}, SSN {{123-45-6789}}")
    assert out == "Hi , SSN {{123-45-6789}}"


def test_strip_placeholders_matches_extract_placeholders_exactly():
    """Single source of truth: anything extract_placeholders finds must be what
    gets stripped, and anything it does NOT find must be left untouched."""
    tmpl = "Hi {{FirstName}}, SSN {{123-45-6789}}, email {{jane@example.com}}"
    found = extract_placeholders(tmpl)
    stripped = strip_placeholders(tmpl)
    assert found == {"FirstName"}
    assert "{{123-45-6789}}" in stripped
    assert "{{jane@example.com}}" in stripped
    assert "{{FirstName}}" not in stripped


def test_strip_placeholders_handles_empty_and_none():
    assert strip_placeholders("") == ""
    assert strip_placeholders(None) == ""


@pytest.mark.parametrize(
    "template",
    [
        "{{First Name}}", "{{First-Name}}", "{{}}", "{{   }}",
        "{{FirstName", "FirstName}}", "{{FirstName}", "{FirstName}}",
        "{{{FirstName}}}", "{{FirstName}}}", "{{{FirstName}}",
        "{{outer {{FirstName}} }}", "{{FirstName}}{{}}", "{{Unknown}}",
    ],
)
def test_render_rejects_unresolved_placeholder_expressions(template):
    with pytest.raises(ValueError, match="placeholder"):
        render(template, recipient={"FirstName": "Jane"}, campaign=_CAMPAIGN)


@pytest.mark.parametrize("clinic", ["{{FirstName}}", "{{Unknown}}", "{{First Name}}", "{{", "}}"])
def test_render_rejects_placeholder_expressions_inserted_by_clinic(clinic):
    with pytest.raises(ValueError, match="placeholder"):
        render("Hi {{ClinicName}}", recipient={}, campaign={"clinicName": clinic})


def test_render_preserves_supported_spacing_adjacent_tokens_and_literal_single_braces():
    assert render(
        "{ Hi {{ FirstName }}{{ ClinicName }} }",
        recipient={"FirstName": "Jane"}, campaign={"clinicName": " VIP "},
    ) == "{ Hi JaneVIP }"
