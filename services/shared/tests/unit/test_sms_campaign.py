"""Exact approved booking copy and encoding-aware, versioned content bounds."""
import pytest

from vip_shared.domain.services.precall_sms import sms_message_parts
from vip_shared.domain.services.sms_campaign import (
    APPROVED_BOOKING_URL,
    DEFAULT_TEMPLATE,
    TEMPLATE_VERSION,
    SmsCampaignError,
    render,
    validate_body,
    validate_template,
    validate_version,
)


def test_exact_default_message_keeps_full_link_and_renders_two_gsm_parts():
    expected = (
        "Hey Test! It's VIP Medical Group and we noticed that booking your consult "
        "might still be on your to-do list. Let's cross it off! Book your own appointment "
        "in 30 seconds - no phone calls needed: https://luma-link.com/ly8aKJQzsjm"
    )
    assert render(DEFAULT_TEMPLATE, recipient={"FirstName": "Test"}) == expected
    assert sms_message_parts(expected) == 2
    assert len(DEFAULT_TEMPLATE) == 236


@pytest.mark.parametrize("name,expected", [
    ("  jose\u0301  ", "José"), ("李", "李"), ("𠀀" * 25, "𠀀" * 20),
    ("o’brien", "O’Brien"), (None, "there"), ([], "there"),
    ("", "there"), ("123456789", "there"), ("{{FirstName}}", "there"),
])
def test_names_unicode_safe_fallback_and_no_recursive_interpolation(name, expected):
    assert render("Hi {{FirstName}}!", recipient={"FirstName": name}) == f"Hi {expected}!"


def test_edited_copy_and_duplicate_name_tokens_preserved_verbatim():
    template = "  {{ FirstName }}, ready?\nBook here: " + APPROVED_BOOKING_URL + "\nThanks, {{FirstName}}!  "
    assert render(template, recipient={"FirstName": "Ana"}) == template.replace("{{ FirstName }}", "Ana").replace("{{FirstName}}", "Ana")


@pytest.mark.parametrize("template", [
    "Hello {{LastName}}", "Hello {{ClinicName}}", "Hello {{Specialty}}", "{{}}",
    "{{FirstName", "{{{FirstName}}}", "{{123-45-6789}}", "{{jane@example.com}}",
])
def test_rejects_unknown_or_malformed_placeholders(template):
    with pytest.raises(SmsCampaignError):
        validate_template(template)


@pytest.mark.parametrize("link", [
    APPROVED_BOOKING_URL + "?x=1", APPROVED_BOOKING_URL + "#x", APPROVED_BOOKING_URL + "/",
    APPROVED_BOOKING_URL + ".", APPROVED_BOOKING_URL + "evil", APPROVED_BOOKING_URL + "'",
    "(" + APPROVED_BOOKING_URL + ")", "https://example.com", "http://luma-link.com/ly8aKJQzsjm",
    "HTTPS://luma-link.com/ly8aKJQzsjm", "www.example.com", "ftp://example.com",
    APPROVED_BOOKING_URL + "{{FirstName}}", "{{FirstName}}" + APPROVED_BOOKING_URL,
    "example.com", "//example.com", "luma-link.com/other", "médico.com", "例子.公司",
    "127.0.0.1", "https://", "www.",
])
def test_only_exact_standalone_approved_link_allowed(link):
    with pytest.raises(SmsCampaignError):
        validate_template("Book here: " + link)


@pytest.mark.parametrize("text", [
    "123-45-6789", "123 45 6789", "jane@example.com", "12/31/2020", "2020-12-31",
    "123456789", "${FirstName}", "${", "diagnosis", "prescribed medication", "DX",
])
def test_existing_phi_and_expression_guards_remain(text):
    with pytest.raises(SmsCampaignError, match="unsafe_campaign_content"):
        validate_template("Hello " + text)


@pytest.mark.parametrize("body,parts", [
    ("A" * 160, 1), ("A" * 161, 2), ("A" * 1530, 10),
    ("^" * 765, 10), ("界" * 70, 1), ("界" * 71, 2), ("界" * 630, 10),
    ("😀" * 315, 10),
])
def test_provider_encoding_limits_and_parts(body, parts):
    validate_body(body)
    validate_template(body)
    assert sms_message_parts(body) == parts


@pytest.mark.parametrize("body", ["A" * 1531, "^" * 766, "界" * 631, "😀" * 316, "A" * 1601])
def test_exceeding_a_provider_limit_is_rejected_without_truncation(body):
    with pytest.raises(SmsCampaignError, match="message_too_long"):
        validate_body(body)


def test_template_guarantees_twenty_astral_letter_names_fit_for_all_recipients():
    validate_template("A" * 590 + "{{FirstName}}")
    assert len(render("A" * 590 + "{{FirstName}}", recipient={"FirstName": "𠀀" * 20}).encode("utf-16-le")) == 1260
    with pytest.raises(SmsCampaignError, match="message_too_long"):
        validate_template("A" * 591 + "{{FirstName}}")
    with pytest.raises(SmsCampaignError, match="message_too_long"):
        validate_template("A" * 551 + "{{FirstName}}{{FirstName}}")


def test_unpaired_surrogate_is_invalid_unicode_with_technical_reason_only():
    with pytest.raises(SmsCampaignError, match="invalid_campaign_unicode"):
        validate_body("Hello \ud800")


@pytest.mark.parametrize("version", [None, "", "campaign-v2", [], {}, False, 1])
def test_unknown_or_non_string_versions_fail_closed(version):
    with pytest.raises(SmsCampaignError, match="unsupported_sms_template_version"):
        validate_version(version)


def test_known_version_supported():
    assert validate_version(TEMPLATE_VERSION) is None


@pytest.mark.parametrize("template", [None, [], {}, False, 0, "", " \n\t "])
def test_no_implicit_default_for_missing_or_invalid_editable_message(template):
    with pytest.raises(SmsCampaignError, match="missing_campaign_template"):
        validate_template(template)
