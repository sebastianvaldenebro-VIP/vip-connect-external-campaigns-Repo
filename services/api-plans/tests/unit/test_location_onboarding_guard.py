import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

os.environ["SNS_ALERTS_TOPIC_ARN"] = "arn:aws:sns:us-east-1:165505826690:vip-plans-alerts"
os.environ["LOCATION_MAPPING_TABLE"] = "VipLocationMapping"

import location_onboarding_guard as guard  # noqa: E402


def _insert_record(location: str, state_code: str, canonical_phone: str | None) -> dict:
    new_image = {
        "location": {"S": location},
        "stateCode": {"S": state_code},
    }
    if canonical_phone is not None:
        new_image["canonicalPhone"] = {"S": canonical_phone}
    return {
        "eventName": "INSERT",
        "dynamodb": {"NewImage": new_image},
    }


def test_alarms_when_new_state_has_no_canonical_phone(monkeypatch):
    published = []

    class FakeSns:
        def publish(self, **kwargs):
            published.append(kwargs)

    monkeypatch.setattr(guard, "_sns_client", lambda: FakeSns())
    monkeypatch.setattr(guard, "_is_first_occurrence_of_state", lambda code, exclude_location: True)

    event = {"Records": [_insert_record("WA - Seattle", "WA", None)]}
    guard.lambda_handler(event, None)

    assert len(published) == 1
    assert published[0]["TopicArn"] == "arn:aws:sns:us-east-1:165505826690:vip-plans-alerts"
    assert "WA" in published[0]["Subject"]
    assert published[0]["MessageAttributes"]["stateCode"]["StringValue"] == "WA"


def test_no_alarm_when_new_state_has_canonical_phone(monkeypatch):
    published = []

    class FakeSns:
        def publish(self, **kwargs):
            published.append(kwargs)

    monkeypatch.setattr(guard, "_sns_client", lambda: FakeSns())
    monkeypatch.setattr(guard, "_is_first_occurrence_of_state", lambda code, exclude_location: True)

    event = {"Records": [_insert_record("WA - Seattle", "WA", "+12065551234")]}
    guard.lambda_handler(event, None)

    assert published == []


def test_no_alarm_when_state_already_existed(monkeypatch):
    published = []

    class FakeSns:
        def publish(self, **kwargs):
            published.append(kwargs)

    monkeypatch.setattr(guard, "_sns_client", lambda: FakeSns())
    monkeypatch.setattr(guard, "_is_first_occurrence_of_state", lambda code, exclude_location: False)

    event = {"Records": [_insert_record("NJ - New Town", "NJ", None)]}
    guard.lambda_handler(event, None)

    assert published == []


def test_ignores_non_insert_events(monkeypatch):
    published = []

    class FakeSns:
        def publish(self, **kwargs):
            published.append(kwargs)

    monkeypatch.setattr(guard, "_sns_client", lambda: FakeSns())

    event = {
        "Records": [
            {
                "eventName": "MODIFY",
                "dynamodb": {"NewImage": {"location": {"S": "NJ - Hoboken"}, "stateCode": {"S": "NJ"}}},
            }
        ]
    }
    guard.lambda_handler(event, None)

    assert published == []
