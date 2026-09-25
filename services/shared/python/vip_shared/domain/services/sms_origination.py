"""Authoritative EUM origination compatibility for promotional campaign SMS."""
from __future__ import annotations

import re
from typing import Any

_PHONE_ARN = re.compile(
    r"arn:aws(?:-us-gov|-cn)?:sms-voice:[a-z0-9-]+:[0-9]{12}:phone-number/[A-Za-z0-9-]+\Z"
)


class SmsOriginationError(ValueError):
    """Controlled code only: provider messages may contain input identifiers."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def validate_promotional_origin(origination_arn: str, *, client: Any) -> None:
    """Require the exact ACTIVE/SMS/PROMOTIONAL phone ARN from EUM.

    The caller owns the AWS client and decides whether its immutable campaign
    contract requires promotional SMS. This function never changes a number or
    sends a message, and never treats lookup failure as compatibility.
    """
    if not isinstance(origination_arn, str) or not _PHONE_ARN.fullmatch(origination_arn):
        raise SmsOriginationError("campaign_sms_origin_invalid_arn")
    try:
        response = client.describe_phone_numbers(PhoneNumberIds=[origination_arn])
    except Exception:
        raise SmsOriginationError("campaign_sms_origin_lookup_failed") from None
    phones = response.get("PhoneNumbers") if isinstance(response, dict) else None
    if not isinstance(phones, list) or len(phones) != 1 or response.get("NextToken"):
        raise SmsOriginationError("campaign_sms_origin_not_found")
    phone = phones[0]
    if not isinstance(phone, dict) or phone.get("PhoneNumberArn") != origination_arn:
        raise SmsOriginationError("campaign_sms_origin_identity_mismatch")
    if phone.get("Status") != "ACTIVE":
        raise SmsOriginationError("campaign_sms_origin_not_active")
    capabilities = phone.get("NumberCapabilities")
    if not isinstance(capabilities, list) or "SMS" not in capabilities:
        raise SmsOriginationError("campaign_sms_origin_not_sms_capable")
    if phone.get("MessageType") != "PROMOTIONAL":
        raise SmsOriginationError("campaign_sms_origin_not_promotional")
