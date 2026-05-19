from datetime import datetime, timezone

from domain.entities.dial_request import DialRequest


def test_build_produces_e164_and_expiration():
    fixed = datetime(2026, 4, 22, 15, 0, 0, tzinfo=timezone.utc)
    req = DialRequest.build(
        lead_id="abc",
        phone_e164="+19734949660",
        expiration_minutes=30,
        attributes={"k": "v"},
        now=fixed,
    )
    assert req.phone_number == "+19734949660"
    assert req.expiration_iso == "2026-04-22T15:30:00Z"
    assert req.lead_id == "abc"
    assert len(req.client_token) == 36  # uuid4


def test_api_payload_shape():
    req = DialRequest.build(
        lead_id="abc",
        phone_e164="+15551234567",
        expiration_minutes=30,
        attributes={"lead_id": "abc", "first_name": "Test"},
    )
    payload = req.to_api_payload()
    assert set(payload.keys()) == {"clientToken", "phoneNumber", "expirationTime", "attributes"}
    assert payload["phoneNumber"] == "+15551234567"
