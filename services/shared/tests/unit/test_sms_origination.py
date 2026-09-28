"""An ARN alone is not authorization to send promotional campaign SMS."""
from unittest.mock import MagicMock

import pytest

from vip_shared.domain.services.sms_origination import SmsOriginationError, validate_promotional_origin

ARN = "arn:aws:sms-voice:us-east-1:123456789012:phone-number/phone-example"


def phone(**overrides):
    return {"PhoneNumberArn": ARN, "Status": "ACTIVE", "NumberCapabilities": ["SMS", "VOICE"],
            "MessageType": "PROMOTIONAL", **overrides}


def test_exact_active_sms_promotional_origin_is_accepted():
    client = MagicMock()
    client.describe_phone_numbers.return_value = {"PhoneNumbers": [phone()]}
    assert validate_promotional_origin(ARN, client=client) is None
    client.describe_phone_numbers.assert_called_once_with(PhoneNumberIds=[ARN])


@pytest.mark.parametrize("arn", [None, {}, [], 123, "", "phone-example", ARN + " ",
                                ARN.replace("phone-number", "pool"), ARN.replace("123456789012", "123")])
def test_malformed_or_non_phone_arn_is_rejected_before_provider_lookup(arn):
    client = MagicMock()
    with pytest.raises(SmsOriginationError, match="invalid_arn"):
        validate_promotional_origin(arn, client=client)
    client.describe_phone_numbers.assert_not_called()


@pytest.mark.parametrize("overrides,code", [
    ({"MessageType": "TRANSACTIONAL"}, "not_promotional"),
    ({"MessageType": None}, "not_promotional"),
    ({"Status": "PENDING"}, "not_active"),
    ({"Status": None}, "not_active"),
    ({"NumberCapabilities": ["VOICE"]}, "not_sms_capable"),
    ({"NumberCapabilities": "SMS"}, "not_sms_capable"),
    ({"NumberCapabilities": None}, "not_sms_capable"),
    ({"PhoneNumberArn": ARN + "other"}, "identity_mismatch"),
])
def test_authoritative_metadata_must_match_every_requirement(overrides, code):
    client = MagicMock()
    client.describe_phone_numbers.return_value = {"PhoneNumbers": [phone(**overrides)]}
    with pytest.raises(SmsOriginationError, match=code):
        validate_promotional_origin(ARN, client=client)


@pytest.mark.parametrize("response", [{}, {"PhoneNumbers": []}, {"PhoneNumbers": [phone(), phone()]},
                                    {"PhoneNumbers": [phone()], "NextToken": "unexpected"},
                                    {"PhoneNumbers": None}, None])
def test_missing_or_ambiguous_lookup_never_allows_sending(response):
    client = MagicMock()
    client.describe_phone_numbers.return_value = response
    with pytest.raises(SmsOriginationError):
        validate_promotional_origin(ARN, client=client)


def test_lookup_exception_is_closed_and_provider_details_are_not_exported():
    client = MagicMock()
    client.describe_phone_numbers.side_effect = RuntimeError("sensitive provider input")
    with pytest.raises(SmsOriginationError) as exc:
        validate_promotional_origin(ARN, client=client)
    assert str(exc.value) == "campaign_sms_origin_lookup_failed"
    assert exc.value.__suppress_context__ is True
