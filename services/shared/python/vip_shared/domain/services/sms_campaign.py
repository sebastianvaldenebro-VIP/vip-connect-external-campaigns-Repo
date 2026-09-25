"""Editable, versioned SMS-only campaign copy; legacy/precall stay separate.

Only a first name may come from a recipient. The approved booking URL is a
static campaign link, never a profile URL. Exceptions contain technical reasons
only, and the editable template is preserved verbatim (never truncated).
"""
from __future__ import annotations

import re

from vip_shared.domain.services.precall_sms import (
    FIRST_NAME_MAX_CHARS,
    _GSM_BASIC,
    _GSM_EXTENSION,
    clean_first_name,
)
from vip_shared.domain.services.sms_template import (
    extract_placeholders,
    has_malformed_placeholders,
    strip_placeholders,
)

TEMPLATE_VERSION = "campaign-v1"
APPROVED_BOOKING_URL = "https://luma-link.com/ly8aKJQzsjm"
DEFAULT_TEMPLATE = (
    "Hey {{FirstName}}! It's VIP Medical Group and we noticed that booking your consult "
    "might still be on your to-do list. Let's cross it off! Book your own appointment "
    "in 30 seconds - no phone calls needed: " + APPROVED_BOOKING_URL
)
MAX_API_CHARACTERS = 1600
MAX_GSM_UNITS = 1530
MAX_UNICODE_UNITS = 630
_TOKEN = re.compile(r"\{\{\s*(\w+)\s*\}\}")
_URL = re.compile(
    r"(?:[a-z][a-z0-9+.-]*://|www\.|//)\S*|"
    r"(?:[\w-]+\.)+[^\W\d_]{2,}(?::\d+)?(?:[/?#]\S*)?|"
    r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?(?:[/?#]\S*)?",
    re.IGNORECASE,
)
_PHI = re.compile(
    r"\b\d{3}[-\s]\d{2}[-\s]\d{4}\b|\S+@\S+\.\S+|"
    r"\b\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}\b|\b\d{4}-\d{2}-\d{2}\b|"
    r"\b\d{7,}\b|\$\{|\b(?:diagnosis|dx|condition|prescribed|medication)\b",
    re.IGNORECASE,
)


class SmsCampaignError(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def validate_version(value: object) -> None:
    if value != TEMPLATE_VERSION or not isinstance(value, str):
        raise SmsCampaignError("unsupported_sms_template_version")


def _screen_content(text: str) -> None:
    if _PHI.search(text):
        raise SmsCampaignError("unsafe_campaign_content")
    for match in _URL.finditer(text):
        if match[0] != APPROVED_BOOKING_URL or (
            match.start() and not text[match.start() - 1].isspace()
        ):
            raise SmsCampaignError("unapproved_booking_url")


def validate_body(body: str) -> None:
    """Check actual outbound text against EUM limits and the v1 content guard."""
    if not isinstance(body, str) or not body.strip():
        raise SmsCampaignError("missing_campaign_message")
    if len(body) > MAX_API_CHARACTERS:
        raise SmsCampaignError("message_too_long")
    if "{{" in body or "}}" in body:
        raise SmsCampaignError("unresolved_campaign_placeholder")
    _screen_content(body)
    if all(c in _GSM_BASIC or c in _GSM_EXTENSION for c in body):
        units = sum(2 if c in _GSM_EXTENSION else 1 for c in body)
        too_long = units > MAX_GSM_UNITS
    else:
        try:
            units = len(body.encode("utf-16-le")) // 2
        except UnicodeEncodeError:
            raise SmsCampaignError("invalid_campaign_unicode") from None
        too_long = units > MAX_UNICODE_UNITS
    if too_long:
        raise SmsCampaignError("message_too_long")


def validate_template(template: str) -> None:
    """Guarantee the length bound for every name the Unicode cleaner accepts."""
    if not isinstance(template, str) or not template.strip():
        raise SmsCampaignError("missing_campaign_template")
    if len(template) > MAX_API_CHARACTERS:
        raise SmsCampaignError("message_too_long")
    if has_malformed_placeholders(template):
        raise SmsCampaignError("malformed_campaign_placeholder")
    if extract_placeholders(template) - {"FirstName"}:
        raise SmsCampaignError("unsupported_campaign_placeholder")
    _screen_content(strip_placeholders(template))
    # A name can be twenty astral Unicode letters (forty UTF-16 units). The
    # GSM scenario also checks the larger capacity of a literal GSM template.
    for name in ("A" * FIRST_NAME_MAX_CHARS, "𠀀" * FIRST_NAME_MAX_CHARS, "there"):
        validate_body(_TOKEN.sub(lambda _: name, template))


def render(template: str, *, recipient: dict) -> str:
    validate_template(template)
    name = clean_first_name(recipient.get("FirstName"))
    body = _TOKEN.sub(lambda _: name, template)
    validate_body(body)
    return body
