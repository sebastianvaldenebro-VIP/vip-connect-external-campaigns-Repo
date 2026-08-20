import os
import sys

import pytest

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
    monkeypatch.setattr(guard, "_is_first_occurrence_of_state", lambda code, exclude_locations: True)

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
    monkeypatch.setattr(guard, "_is_first_occurrence_of_state", lambda code, exclude_locations: True)

    event = {"Records": [_insert_record("WA - Seattle", "WA", "+12065551234")]}
    guard.lambda_handler(event, None)

    assert published == []


def test_no_alarm_when_state_already_existed(monkeypatch):
    published = []

    class FakeSns:
        def publish(self, **kwargs):
            published.append(kwargs)

    monkeypatch.setattr(guard, "_sns_client", lambda: FakeSns())
    monkeypatch.setattr(guard, "_is_first_occurrence_of_state", lambda code, exclude_locations: False)

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


def test_bulk_insert_of_same_new_state_alerts_exactly_once(monkeypatch):
    """Regression for the bug this fix round addresses: when a brand-new
    state's locations are added together (realistic onboarding pattern —
    today's PA data already has multiple location rows per state, and any
    future new state will likely be onboarded the same way), all INSERT
    records land in the same Streams batch and are already durably
    committed by the time this Lambda scans. Excluding only the single
    triggering record's own location (the old, buggy behavior) would make
    every sibling see the others and wrongly conclude the state pre-existed
    — silencing the alert entirely. The fix excludes the full batch-sibling
    set per stateCode and dedupes so exactly ONE alert fires per stateCode
    per invocation, regardless of how many sibling records qualify.
    """
    published = []

    class FakeSns:
        def publish(self, **kwargs):
            published.append(kwargs)

    seen_exclude_sets = []

    def fake_first_occurrence(code, exclude_locations):
        seen_exclude_sets.append(exclude_locations)
        # Simulate the real scan: the only items with this stateCode are
        # the batch's own siblings, so once they're all excluded nothing
        # else remains -> True (first occurrence).
        return True

    monkeypatch.setattr(guard, "_sns_client", lambda: FakeSns())
    monkeypatch.setattr(guard, "_is_first_occurrence_of_state", fake_first_occurrence)

    event = {
        "Records": [
            _insert_record("ZZ - Townsville", "ZZ", None),
            _insert_record("ZZ - Villageburg", "ZZ", None),
        ]
    }
    guard.lambda_handler(event, None)

    assert len(published) == 1
    assert published[0]["MessageAttributes"]["stateCode"]["StringValue"] == "ZZ"
    # Both sibling locations must have been passed as the exclude set, not
    # just the location of whichever record happened to trigger the alert.
    assert seen_exclude_sets[0] == {"ZZ - Townsville", "ZZ - Villageburg"}


def test_bulk_insert_same_new_state_one_with_phone_still_alerts(monkeypatch):
    """Within one batch, if one sibling location already has canonicalPhone
    set but another sibling for the SAME brand-new state does not, we
    still alert. Rationale: the alert means "this state's onboarding is
    incomplete" — the state as a whole isn't fully configured until every
    location has a canonical phone, so the location still missing one
    should still trigger the ops alarm even though a sibling is already
    set up correctly.
    """
    published = []

    class FakeSns:
        def publish(self, **kwargs):
            published.append(kwargs)

    monkeypatch.setattr(guard, "_sns_client", lambda: FakeSns())
    monkeypatch.setattr(guard, "_is_first_occurrence_of_state", lambda code, exclude_locations: True)

    event = {
        "Records": [
            _insert_record("ZZ - Townsville", "ZZ", "+12065551234"),
            _insert_record("ZZ - Villageburg", "ZZ", None),
        ]
    }
    guard.lambda_handler(event, None)

    assert len(published) == 1
    assert published[0]["MessageAttributes"]["stateCode"]["StringValue"] == "ZZ"


def test_sns_publish_failure_reraises_so_lambda_errors_metric_fires(monkeypatch):
    """The CRITICAL fix this Lambda exists for: a publish failure (e.g. a
    missing KMS grant on the SSE-encrypted topic) must propagate out of
    lambda_handler, not be swallowed — otherwise the alarm's own failures
    are invisible and the feature ships as a silent no-op."""
    class FailingSns:
        def publish(self, **kwargs):
            raise RuntimeError("KMSAccessDeniedException: simulated")

    monkeypatch.setattr(guard, "_sns_client", lambda: FailingSns())
    monkeypatch.setattr(guard, "_is_first_occurrence_of_state", lambda code, exclude_locations: True)

    event = {"Records": [_insert_record("ZZ - Townsville", "ZZ", None)]}

    with pytest.raises(RuntimeError, match="KMSAccessDeniedException"):
        guard.lambda_handler(event, None)
