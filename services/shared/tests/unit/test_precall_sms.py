import pytest

from vip_shared.domain.services import precall_sms as sms

POLICY = {"mode": "profile", "catalogVersion": "phase1-v1"}


def profile(**attributes):
    return {"FirstName": "José", "Attributes": {"campaign": "Vein", "clinic_name": "Example Clinic", **attributes}}


def test_approved_copy_is_embedded_without_same_number_promise():
    assert sms.CATALOG == {
        "Vein": "Hi {{FirstName}}! This is {{ClinicName}}. We’re about to give you a quick call regarding your vein consultation request. Look out for a call!",
        "Pain": "Hi {{FirstName}}! {{ClinicName}} here. We’re calling you in just a moment to discuss your pain management request. Talk soon!",
    }
    message = sms.personalize(profile(), {**POLICY, "clinicName": "Example Clinic"})
    assert message.body.startswith("Hi José! This is Example Clinic. We’re")
    assert message.body.endswith("Look out for a call!")
    assert "from this number" not in message.body


def test_exact_copy_can_exceed_legacy_160_character_limit():
    p = profile()
    p["FirstName"] = "A" * 20
    message = sms.personalize(p, {**POLICY, "clinicName": "Example Vein Treatment Clinic"})
    assert len(message.body) > 160
    assert message.parts == 3


@pytest.mark.parametrize("campaign,expected", [("VEIN", "Vein"), ("Pain Management", "Pain"), ("New Lead - Vein", "Vein")])
def test_specialty_comes_from_this_profiles_campaign(campaign, expected):
    assert sms.personalize(profile(campaign=campaign), POLICY).specialty == expected


@pytest.mark.parametrize("attrs,reason", [
    ({"campaign": "Vein + Pain"}, "ambiguous_specialty"),
    ({"campaign": "Vein", "specialty": "Pain"}, "ambiguous_specialty"),
    ({"campaign": "Fibroid"}, "missing_specialty"),
    ({"campaign": "", "specialty": "Other"}, "unsupported_specialty"),
])
def test_unsupported_or_missing_profile_data_has_only_a_technical_reason(attrs, reason):
    with pytest.raises(sms.PersonalizationError) as error:
        sms.personalize(profile(**attrs), POLICY)
    assert str(error.value) == reason


@pytest.mark.parametrize("clinic", ["1234", "...", "Clinic\nOther", "{{FirstName}}", "[Name]", "clinic@example.com", "https://example.com", "Clinic 123456789", "Clinic diagnosis", "A" * 81])
def test_invalid_clinic_is_never_substituted(clinic):
    with pytest.raises(sms.PersonalizationError, match="invalid_clinic"):
        sms.personalize(profile(), {**POLICY, "clinicName": clinic})


@pytest.mark.parametrize("specialty,expected", [
    ("Vein", "Hi José! We’re about to give you a quick call regarding your vein consultation request. Look out for a call!"),
    ("Pain", "Hi José! We’re calling you in just a moment to discuss your pain management request. Talk soon!"),
])
@pytest.mark.parametrize("optional", [{}, {"clinicName": ""}, {"clinicName": " \t\n\u00a0 "}])
def test_absent_or_blank_campaign_clinic_omits_complete_phrase(specialty, expected, optional):
    message = sms.personalize(profile(campaign=specialty, clinic_name="Ignored Profile Clinic"), {**POLICY, **optional})
    assert message.body == expected
    assert message.clinic_name == ""
    assert "{{" not in message.body


@pytest.mark.parametrize("profile_clinic", [None, "", "Different Clinic", "{{unsafe}}", ["invalid"]])
def test_campaign_clinic_is_used_without_requiring_or_consulting_profile_clinic(profile_clinic):
    recipient = profile(clinic_name=profile_clinic)
    message = sms.personalize(recipient, {**POLICY, "clinicName": " Campaign Clinic "})
    assert message.body.startswith("Hi José! This is Campaign Clinic. We’re")
    assert message.clinic_name == "Campaign Clinic"
    del recipient["Attributes"]["clinic_name"]
    assert sms.personalize(recipient, {**POLICY, "clinicName": " Campaign Clinic "}) == message


def test_profile_and_location_catalogs_cannot_supply_an_omitted_campaign_clinic(monkeypatch):
    monkeypatch.setattr(sms, "CLINIC_NAMES_BY_SPECIALTY", {"Vein": "Ignored Specialty Clinic"}, raising=False)
    monkeypatch.setattr(sms, "CLINIC_NAMES_BY_LOCATION_SPECIALTY", {("123", "Vein"): "Ignored Location Clinic"}, raising=False)
    message = sms.personalize(profile(location_id="123"), POLICY)
    assert message.clinic_name == "" and "Clinic" not in message.body


def test_policy_normalization_is_canonical_idempotent_and_does_not_mutate_input():
    policy = {**POLICY, "clinicName": "  Clinica Jose\u0301  "}
    normalized = sms.normalize_policy(policy)
    assert normalized == {**POLICY, "clinicName": "Clinica José"}
    assert sms.normalize_policy(normalized) == normalized
    assert policy["clinicName"] == "  Clinica Jose\u0301  "
    assert sms.normalize_policy({**POLICY, "clinicName": " \n\t "}) == POLICY
    assert sms.validate_policy(policy) is None


@pytest.mark.parametrize("value", [None, [], {}, 0, False, True])
def test_optional_clinic_rejects_non_strings_even_when_falsy(value):
    with pytest.raises(sms.PersonalizationError, match="invalid_clinic"):
        sms.normalize_policy({**POLICY, "clinicName": value})


@pytest.mark.parametrize("policy", [None, [], {}, {"mode": "profile"}, {"catalogVersion": "phase1-v1"},
                                   {**POLICY, "unexpected": "value"}, {**POLICY, "mode": "manual"}])
def test_policy_requires_exact_contract(policy):
    with pytest.raises(sms.PersonalizationError, match="invalid_policy"):
        sms.normalize_policy(policy)


@pytest.mark.parametrize("name,expected", [("  marÍA  ", "María"), ("Анна", "Анна"), ("O’NEIL", "O’Neil"), (None, "there"), ("TEST123", "there"), ("A\nB", "there")])
def test_names_keep_safe_unicode_and_use_neutral_fallback(name, expected):
    assert sms.clean_first_name(name) == expected
    assert len(sms.clean_first_name("ß" * 40)) <= 20


def test_maximum_clinic_and_name_are_valid_multipart_messages():
    p = profile()
    p["FirstName"] = "A" * 20
    assert sms.personalize(p, {**POLICY, "clinicName": "C" * 80}).parts == 4


def test_sms_part_count_handles_gsm_extension_and_utf16_units():
    assert sms.sms_message_parts("a" * 160) == 1
    assert sms.sms_message_parts("^" * 81) == 2
    assert sms.sms_message_parts("漢" * 71) == 2
    assert sms.sms_message_parts("😀" * 36) == 2


def test_unknown_catalogue_is_rejected_without_falling_back_to_latest():
    with pytest.raises(sms.PersonalizationError, match="unsupported_catalog_version"):
        sms.personalize(profile(), {"mode": "profile", "catalogVersion": "phase1-v2"})
