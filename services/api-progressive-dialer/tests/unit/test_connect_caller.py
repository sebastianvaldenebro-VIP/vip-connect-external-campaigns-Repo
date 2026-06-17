from unittest.mock import MagicMock
import pytest
from connect_caller import ConnectCaller, DialResult

INSTANCE_ID = "6b3f17ba-68a4-472a-9b20-db1991507009"
FLOW_ID = "3d24320b-c1e3-40f3-90a2-b6867ef70c85"


def _make_caller():
    mock_boto = MagicMock()
    return ConnectCaller(instance_id=INSTANCE_ID, contact_flow_id=FLOW_ID, boto_client=mock_boto), mock_boto


def test_dial_returns_contact_id_on_success():
    caller, mock_boto = _make_caller()
    mock_boto.start_outbound_voice_contact.return_value = {"ContactId": "contact-001"}
    result = caller.dial(destination_phone="+15551234567", queue_id="queue-001")
    assert result.contact_id == "contact-001"
    assert result.success is True


def test_dial_passes_correct_params():
    caller, mock_boto = _make_caller()
    mock_boto.start_outbound_voice_contact.return_value = {"ContactId": "contact-001"}
    caller.dial(destination_phone="+15551234567", queue_id="queue-001", source_phone="+19174105649")

    call_kwargs = mock_boto.start_outbound_voice_contact.call_args[1]
    assert call_kwargs["InstanceId"] == INSTANCE_ID
    assert call_kwargs["ContactFlowId"] == FLOW_ID
    # Phone number must NOT appear in the logged call — verify it IS passed
    assert call_kwargs["DestinationPhoneNumber"] == "+15551234567"
    assert call_kwargs["QueueId"] == "queue-001"
    assert call_kwargs["TrafficType"] == "GENERAL"


def test_dial_returns_failure_on_throttle():
    from botocore.exceptions import ClientError
    caller, mock_boto = _make_caller()
    mock_boto.start_outbound_voice_contact.side_effect = ClientError(
        {"Error": {"Code": "TooManyRequestsException", "Message": "Rate exceeded"}},
        "StartOutboundVoiceContact",
    )
    result = caller.dial(destination_phone="+15551234567", queue_id="queue-001")
    assert result.success is False
    assert result.error_code == "TooManyRequestsException"
    assert result.throttled is True


def test_dial_returns_failure_on_limit_exceeded():
    from botocore.exceptions import ClientError
    caller, mock_boto = _make_caller()
    mock_boto.start_outbound_voice_contact.side_effect = ClientError(
        {"Error": {"Code": "LimitExceededException", "Message": "Concurrent calls exceeded"}},
        "StartOutboundVoiceContact",
    )
    result = caller.dial(destination_phone="+15551234567", queue_id="queue-001")
    assert result.success is False
    assert result.error_code == "LimitExceededException"
