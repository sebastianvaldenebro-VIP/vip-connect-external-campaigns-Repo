"""Plans owns the SMS scheduling source at both Lambda invocation boundaries."""

import json

import pytest

from .test_executor_sms_pending import (
    environment as _shared_environment,
    executor,
    model,
    response,
)


@pytest.fixture
def environment(monkeypatch):
    monkeypatch.setenv("SMS_RETRY_FUNCTION_ARN", "arn/retry")
    yield from _shared_environment.__wrapped__(monkeypatch)


@pytest.mark.parametrize("supplied", [{}, {"scheduleSource": "recipient"},
                                     {"scheduleSource": None}, {"scheduleSource": True}])
def test_sender_wrapper_owns_schedule_source_at_sdk_boundary(environment, supplied):
    client, _ = environment
    client.invoke.return_value = response({"enqueued": 0, "failed": 0})

    executor._invoke_sms_sender(
        campaignId="sms-c", planId="p", runId="r", **supplied,
    )

    client.invoke.assert_called_once()
    request = client.invoke.call_args.kwargs
    assert request["FunctionName"] == "arn/sender"
    assert request["InvocationType"] == "RequestResponse"
    assert json.loads(request["Payload"]) == {
        "campaignId": "sms-c", "planId": "p", "runId": "r",
        "scheduleSource": "plans",
    }


def test_retry_wrapper_stamps_same_source_and_identity(environment):
    client, _ = environment
    client.invoke.return_value = response({"retried": 0, "stillSkipped": 0})

    executor._invoke_sms_retry_quiet_hours(
        campaign_id="sms-c", plan_id="p", run_id="r",
    )

    client.invoke.assert_called_once()
    request = client.invoke.call_args.kwargs
    assert request["FunctionName"] == "arn/retry"
    assert request["InvocationType"] == "RequestResponse"
    assert json.loads(request["Payload"]) == {
        "campaignId": "sms-c", "planId": "p", "runId": "r",
        "scheduleSource": "plans",
    }


@pytest.mark.parametrize("mode", [None, "manual", "profile", "sms-only"])
def test_all_sender_paths_stamp_source_on_initial_and_pending_resume(environment, mode):
    """Keep the real common wrapper while faking only its SDK transport."""
    client, _ = environment
    run, plan, cs = model("sms" if mode == "sms-only" else "campaign")
    cfg = plan["buckets"][0]["campaigns"][0]["campaignConfig"]
    cfg["scheduleSource"] = "recipient"  # Public config cannot choose the source.
    precall = cfg["precallSms"]
    precall["scheduleSource"] = "recipient"
    if mode in {"manual", "profile"}:
        precall["mode"] = mode
    if mode == "profile":
        precall["catalogVersion"] = "phase1-v1"
        result = {
            "enqueued": 0, "failed": 0, "pending": True,
            "initializationComplete": False, "totalEnqueued": 0,
            "totalSent": 0, "totalFailed": 0, "totalOptedOut": 0,
        }
    else:
        result = {"enqueued": 0, "failed": 0, "pending": True}
    client.invoke.side_effect = [response(result), response(result)]

    for _ in range(2):
        if mode == "sms-only":
            cs["smsCampaignId"] = "sms-c"
            executor._continue_bulk_sms_initialization(run, plan, 0, 0)
        else:
            executor._attempt_precall_sms_send(
                run=run, cs=cs, precall=precall, segment_arn="arn/segment",
                segment_name="segment", bucket_index=0, campaign_index=0,
                sms_campaign_id="sms-c",
            )

    assert client.invoke.call_count == 2
    requests = [call.kwargs for call in client.invoke.call_args_list]
    payloads = [json.loads(request["Payload"]) for request in requests]
    assert payloads[0] == payloads[1]
    for request, payload in zip(requests, payloads):
        assert request["FunctionName"] == "arn/sender"
        assert payload["scheduleSource"] == "plans"
        assert (payload["campaignId"], payload["planId"], payload["runId"]) == (
            "sms-c", "p", "r",
        )
        assert payload["segmentArn"] == "arn/segment"
        assert payload["segmentName"] == "segment"
        if mode == "profile":
            assert payload["precallPolicy"] == {
                "mode": "profile", "catalogVersion": "phase1-v1",
            }
            assert "messageTemplate" not in payload
        else:
            assert payload["messageTemplate"] == "Hi"
            assert "precallPolicy" not in payload
