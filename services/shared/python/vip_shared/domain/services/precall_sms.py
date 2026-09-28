"""Versioned pre-call copy and narrowly scoped, per-profile personalization.

The exact approved copy retains Unicode punctuation. Messages may contain up to
six SMS parts; the legacy manual renderer and its 160-character ceiling are
unchanged. No patient values belong in exception messages or logs.

Only phase1-v1 is supported. A future catalog must retain this version's copy
and policy semantics for persisted runs; never replace v1 in place
or silently interpret an unknown version using the latest catalog.
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass

CATALOG_VERSION = "phase1-v1"
FIRST_NAME_MAX_CHARS = 20
MAX_CLINIC_NAME_CHARS = 80
MAX_SMS_PARTS = 6
CATALOG = {
    "Vein": "Hi {{FirstName}}! This is {{ClinicName}}. We’re about to give you a quick call regarding your vein consultation request. Look out for a call!",
    "Pain": "Hi {{FirstName}}! {{ClinicName}} here. We’re calling you in just a moment to discuss your pain management request. Talk soon!",
}

CATALOG_WITHOUT_CLINIC = {
    "Vein": "Hi {{FirstName}}! We’re about to give you a quick call regarding your vein consultation request. Look out for a call!",
    "Pain": "Hi {{FirstName}}! We’re calling you in just a moment to discuss your pain management request. Talk soon!",
}

_GSM_BASIC = set("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà")
_GSM_EXTENSION = set("\f^{}\\[~]|€")
_UNSAFE_CLINIC = re.compile(
    r"[{}\[\]]|https?://|www\.|\S+@\S+|\b\d{7,}\b|"
    r"\b\d{3}[- ]\d{2}[- ]\d{4}\b|\b\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}\b|"
    r"\b\d{4}-\d{2}-\d{2}\b|\b(?:diagnosis|dx|condition|prescribed|medication)\b",
    re.IGNORECASE,
)


class PersonalizationError(ValueError):
    """A technical, non-PHI reason for suppressing one recipient."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class PersonalizedMessage:
    body: str
    specialty: str
    clinic_name: str
    parts: int


def normalize_policy(policy: dict) -> dict:
    """Return the immutable run policy with an optional campaign clinic.

    Blank strings omit the whole clinic phrase. Profile attributes never
    supply a clinic when campaign configuration omitted it.
    """
    if not isinstance(policy, dict) or policy.get("mode") != "profile":
        raise PersonalizationError("invalid_policy")
    required = {"mode", "catalogVersion"}
    if not required.issubset(policy) or set(policy) - required - {"clinicName"}:
        raise PersonalizationError("invalid_policy")
    if policy.get("catalogVersion") != CATALOG_VERSION:
        raise PersonalizationError("unsupported_catalog_version")
    normalized = {"mode": "profile", "catalogVersion": CATALOG_VERSION}
    if "clinicName" in policy:
        raw = policy["clinicName"]
        if not isinstance(raw, str):
            raise PersonalizationError("invalid_clinic")
        clinic = unicodedata.normalize("NFC", raw).strip()
        if clinic:
            normalized["clinicName"] = clean_clinic_name(clinic)
    return normalized


def validate_policy(policy: dict) -> None:
    normalize_policy(policy)


def clean_first_name(raw: object) -> str:
    if not isinstance(raw, str):
        return "there"
    name = unicodedata.normalize("NFC", raw).strip()
    if not name or not name[0].isalpha() or any(
        not (unicodedata.category(c).startswith(("L", "M")) or c in " '-’")
        for c in name
    ):
        return "there"
    return name.title()[:FIRST_NAME_MAX_CHARS]


def resolve_specialty(attributes: dict) -> str:
    campaign = attributes.get("campaign") or ""
    explicit = attributes.get("specialty") or ""
    if not isinstance(campaign, str) or not isinstance(explicit, str):
        raise PersonalizationError("invalid_specialty")
    matches = {
        name for name in CATALOG
        if re.search(r"\b" + name + r"\b", campaign, flags=re.IGNORECASE)
    }
    if explicit.strip():
        aliases = {"vein": "Vein", "pain": "Pain", "pain management": "Pain"}
        selected = aliases.get(explicit.strip().casefold())
        if selected is None:
            raise PersonalizationError("unsupported_specialty")
        matches.add(selected)
    if len(matches) > 1:
        raise PersonalizationError("ambiguous_specialty")
    if not matches:
        raise PersonalizationError("missing_specialty")
    return matches.pop()


def clean_clinic_name(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise PersonalizationError("missing_clinic")
    clinic = unicodedata.normalize("NFC", raw).strip()
    if not any(c.isalpha() for c in clinic) or len(clinic) > MAX_CLINIC_NAME_CHARS or _UNSAFE_CLINIC.search(clinic) or any(
        not (unicodedata.category(c).startswith(("L", "M", "N")) or c in " &'’.,()-/:")
        for c in clinic
    ):
        raise PersonalizationError("invalid_clinic")
    return clinic


def sms_message_parts(body: str) -> int:
    if all(c in _GSM_BASIC or c in _GSM_EXTENSION for c in body):
        units = sum(2 if c in _GSM_EXTENSION else 1 for c in body)
        return 1 if units <= 160 else math.ceil(units / 153)
    units = len(body.encode("utf-16-le")) // 2
    return 1 if units <= 70 else math.ceil(units / 67)


def personalize(recipient: dict, policy: dict) -> PersonalizedMessage:
    policy = normalize_policy(policy)
    attributes = recipient.get("Attributes") or {}
    if not isinstance(attributes, dict):
        raise PersonalizationError("invalid_profile_attributes")
    specialty = resolve_specialty(attributes)
    clinic = policy.get("clinicName", "")
    catalog = CATALOG if clinic else CATALOG_WITHOUT_CLINIC
    body = catalog[specialty].replace("{{FirstName}}", clean_first_name(recipient.get("FirstName")))
    body = body.replace("{{ClinicName}}", clinic)
    parts = sms_message_parts(body)
    if parts > MAX_SMS_PARTS:
        raise PersonalizationError("message_too_long")
    return PersonalizedMessage(body, specialty, clinic, parts)
