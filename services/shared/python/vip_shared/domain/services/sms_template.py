"""Pre-call SMS template rendering with a deliberately narrow placeholder allowlist.

WHY AN ALLOWLIST RATHER THAN FREE INTERPOLATION:

  handlers/plans.py's _validate_sms_campaign blocks {{...}} entirely today. That
  ban existed for two reasons, and only one of them is going away:

    1. NO RENDERER EXISTED. sms_processor_handler.py passes the template straight
       to EUM SendTextMessage, so {{FirstName}} would reach the patient as
       literal braces. This module is that renderer — reason (1) is now resolved.

    2. PHI. An unrestricted placeholder mechanism is a channel for putting
       anything from a profile into an outbound message. That reason STANDS.

  So interpolation is allowed only for named fields, split by origin:

    RECIPIENT_FIELDS — per-recipient, from the patient's own Customer Profile.
      Sending patients their own first name is ordinary clinical communication:
      the recipient IS the data subject, so there is no third-party disclosure.
      LastName is deliberately NOT here — CP exposes it, but a surname adds
      identifiability with no engagement benefit.

    CAMPAIGN_FIELDS — campaign-level constants, identical for every recipient.
      Not recipient data at all.

  Widening either set is a PHI decision, not an implementation detail. Every
  other pattern in _PHI_PATTERNS (SSN, email, dates, MRN-like numbers, URLs,
  clinical terms) remains enforced against the template independently.
"""

from __future__ import annotations

import re

RECIPIENT_FIELDS = {"FirstName"}
# Specialty is deliberately NOT here: the business-approved copy bakes the
# specialty into the sentence ("your vein consultation request") rather than
# substituting a noun, so an interpolatable {{Specialty}} would be dead surface.
# See Task 5, "The approved copy", point (1) — re-adding it is additive.
CAMPAIGN_FIELDS = {"ClinicName"}
ALLOWED_FIELDS = RECIPIENT_FIELDS | CAMPAIGN_FIELDS

_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")

# Used when a profile has no usable first name. Keeps the copy grammatical
# instead of delivering "Hi !" or a literal placeholder.
_FIRST_NAME_FALLBACK = "there"

# A first name is one word of a person's name, not a free-text field. CP holds
# test/junk rows ("TEST 12345"), and interpolating those would push digits into
# the message body — the exact shape the plan-side MRN pattern guards against.
_NAME_MAX_LEN = 20
_NAME_OK_RE = re.compile(r"^[A-Za-z][A-Za-z'\- ]*$")

# Length budget for validation: assume the longest name we would ever render.
_NAME_BUDGET = _NAME_MAX_LEN


def extract_placeholders(template: str) -> set[str]:
    """Return every `{{Field}}` name appearing in `template`."""
    return set(_PLACEHOLDER_RE.findall(template or ""))


def _clean_first_name(raw: object) -> str:
    if not isinstance(raw, str):
        return _FIRST_NAME_FALLBACK
    name = raw.strip()
    if not name or not _NAME_OK_RE.match(name):
        return _FIRST_NAME_FALLBACK
    return name[:_NAME_MAX_LEN].title()


def render(template: str, *, recipient: dict, campaign: dict) -> str:
    """Interpolate allowlisted placeholders in `template`.

    Raises ValueError for any placeholder outside ALLOWED_FIELDS — validation
    should already have rejected such a template, so reaching here means the
    guard was bypassed and sending would be worse than failing.
    """
    unknown = extract_placeholders(template) - ALLOWED_FIELDS
    if unknown:
        raise ValueError(
            f"template contains non-allowlisted placeholder(s): {sorted(unknown)}"
        )

    values = {
        "FirstName": _clean_first_name(recipient.get("FirstName")),
        "ClinicName": str(campaign.get("clinicName") or "").strip(),
    }
    return _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], template)


def max_rendered_length(
    template: str, *, campaign: dict, name_budget: int = _NAME_BUDGET
) -> int:
    """Worst-case rendered length, for the SMS length validation budget.

    A template can be under the ceiling and still render over it once a name is
    substituted, so validation must measure the rendered worst case. The default
    budget is _NAME_MAX_LEN because render() truncates there, which makes it a
    real upper bound rather than a guess.

    `name_budget` is overridable only so a test can ask "what if we truncated
    names shorter?" without mutating a module global. It is not a tuning knob:
    _NAME_MAX_LEN stays 20 (see OQ-9, where lowering it was rejected).
    """
    return len(
        render(
            template,
            recipient={"FirstName": "A" * name_budget},
            campaign=campaign,
        )
    )
