import pytest
from agent_event_filter import is_agent_available, extract_agent_info

# ── is_agent_available ────────────────────────────────────────────────

def _state_change_event(status_type: str, status_name: str, next_status_name: str | None = None) -> dict:
    event = {
        "EventType": "STATE_CHANGE",
        "AgentSnapshot": {
            "AgentStatus": {
                "Type": status_type,
                "Name": status_name,
                "StartTimestamp": "2026-06-16T14:00:00.000Z",
            },
            "Configuration": {
                "RoutingProfile": {
                    "Concurrency": [
                        {"Channel": "VOICE", "AvailableSlots": 1, "MaximumSlots": 1}
                    ]
                }
            }
        }
    }
    if next_status_name is not None:
        event["AgentSnapshot"]["NextAgentStatus"] = {
            "Name": next_status_name,
            "EnqueuedTimestamp": "2026-06-16T14:05:00.000Z"
        }
    return event

def test_routable_available_is_available():
    assert is_agent_available(_state_change_event("ROUTABLE", "Available")) is True

def test_custom_status_not_available():
    assert is_agent_available(_state_change_event("CUSTOM", "Break")) is False

def test_offline_not_available():
    assert is_agent_available(_state_change_event("OFFLINE", "Offline")) is False

def test_routable_but_pending_break_not_available():
    # Agent queued a break — will go offline after current contact
    assert is_agent_available(_state_change_event("ROUTABLE", "Available", next_status_name="Lunch")) is False

def test_routable_next_status_also_available_is_available():
    # NextAgentStatus=Available is fine (e.g., returning from break)
    assert is_agent_available(_state_change_event("ROUTABLE", "Available", next_status_name="Available")) is True

def test_heartbeat_not_available():
    assert is_agent_available({"EventType": "HEART_BEAT"}) is False

def test_login_not_available():
    assert is_agent_available({"EventType": "LOGIN"}) is False

def test_logout_not_available():
    assert is_agent_available({"EventType": "LOGOUT"}) is False

def test_state_change_routing_profile_update_not_available():
    # STATE_CHANGE fired for config change, not status — AgentStatus.Type=CUSTOM
    assert is_agent_available(_state_change_event("CUSTOM", "Available")) is False

# ── extract_agent_info ────────────────────────────────────────────────

def test_extract_agent_info_returns_id_and_queue():
    event = _state_change_event("ROUTABLE", "Available")
    event["AgentARN"] = "arn:aws:connect:us-east-1:165505826690:instance/abc/agent/agent-001"
    event["AgentSnapshot"]["Configuration"] = {
        "RoutingProfile": {
            "DefaultOutboundQueue": {
                "ARN": "arn:aws:connect:us-east-1:165505826690:instance/abc/queue/queue-001"
            },
            "Concurrency": []
        }
    }
    info = extract_agent_info(event)
    assert info["agent_arn"] == event["AgentARN"]
    assert info["queue_arn"] == "arn:aws:connect:us-east-1:165505826690:instance/abc/queue/queue-001"

def test_extract_agent_info_missing_queue_returns_none():
    event = _state_change_event("ROUTABLE", "Available")
    event["AgentARN"] = "arn:aws:connect:us-east-1:165505826690:instance/abc/agent/agent-001"
    info = extract_agent_info(event)
    assert info["queue_arn"] is None

# ── is_queue_allowed ──────────────────────────────────────────────────

def test_is_queue_allowed_returns_true_when_allowed_set_is_empty():
    # Empty set means "allow all queues" (no filter configured)
    from agent_event_filter import is_queue_allowed
    assert is_queue_allowed("arn:aws:connect:us-east-1:123:instance/abc/queue/q1", set()) is True

def test_is_queue_allowed_returns_true_when_queue_in_set():
    from agent_event_filter import is_queue_allowed
    allowed = {"arn:aws:connect:us-east-1:123:instance/abc/queue/q1"}
    assert is_queue_allowed("arn:aws:connect:us-east-1:123:instance/abc/queue/q1", allowed) is True

def test_is_queue_allowed_returns_false_when_queue_not_in_set():
    from agent_event_filter import is_queue_allowed
    allowed = {"arn:aws:connect:us-east-1:123:instance/abc/queue/q2"}
    assert is_queue_allowed("arn:aws:connect:us-east-1:123:instance/abc/queue/q1", allowed) is False

def test_is_queue_allowed_returns_false_when_queue_arn_is_none():
    from agent_event_filter import is_queue_allowed
    assert is_queue_allowed(None, {"arn:aws:connect:us-east-1:123:instance/abc/queue/q1"}) is False

# ── CurrentAgentSnapshot (real AWS Kinesis format) ────────────────────────────

def _current_snapshot_event(status_type: str, status_name: str) -> dict:
    """Build an event using the real Kinesis key (CurrentAgentSnapshot, not AgentSnapshot)."""
    return {
        "EventType": "STATE_CHANGE",
        "AgentARN": "arn:aws:connect:us-east-1:123:instance/abc/agent/agent-001",
        "CurrentAgentSnapshot": {
            "AgentStatus": {
                "Type": status_type,
                "Name": status_name,
                "StartTimestamp": "2026-07-01T10:00:00.000Z",
            },
            "Configuration": {
                "RoutingProfile": {
                    "DefaultOutboundQueue": {
                        "ARN": "arn:aws:connect:us-east-1:123:instance/abc/queue/outbound"
                    },
                    "InboundQueues": [
                        {"ARN": "arn:aws:connect:us-east-1:123:instance/abc/queue/inbound-1"},
                        {"ARN": "arn:aws:connect:us-east-1:123:instance/abc/queue/inbound-2"},
                    ],
                    "Concurrency": [
                        {"Channel": "VOICE", "AvailableSlots": 1, "MaximumSlots": 1}
                    ],
                }
            },
        },
    }


def test_is_agent_available_with_current_agent_snapshot():
    """Real Kinesis events use CurrentAgentSnapshot — availability must be detected."""
    assert is_agent_available(_current_snapshot_event("ROUTABLE", "Available")) is True


def test_is_agent_available_current_snapshot_non_routable():
    assert is_agent_available(_current_snapshot_event("CUSTOM", "Break")) is False


def test_current_snapshot_takes_priority_over_agent_snapshot():
    """When both keys exist (shouldn't happen but defensive), CurrentAgentSnapshot wins."""
    event = _current_snapshot_event("ROUTABLE", "Available")
    # Inject a conflicting AgentSnapshot that would return OFFLINE
    event["AgentSnapshot"] = {
        "AgentStatus": {"Type": "OFFLINE", "Name": "Offline"},
        "Configuration": {"RoutingProfile": {"Concurrency": []}},
    }
    assert is_agent_available(event) is True  # CurrentAgentSnapshot wins


def test_extract_agent_info_returns_inbound_queue_arns():
    """extract_agent_info must return all InboundQueue ARNs alongside the outbound one."""
    event = _current_snapshot_event("ROUTABLE", "Available")
    info = extract_agent_info(event)
    assert info["agent_arn"] == event["AgentARN"]
    assert info["queue_arn"] == "arn:aws:connect:us-east-1:123:instance/abc/queue/outbound"
    assert info["inbound_queue_arns"] == [
        "arn:aws:connect:us-east-1:123:instance/abc/queue/inbound-1",
        "arn:aws:connect:us-east-1:123:instance/abc/queue/inbound-2",
    ]


def test_extract_agent_info_inbound_queue_arns_empty_when_not_present():
    """Events without InboundQueues must return an empty list, not raise."""
    event = _state_change_event("ROUTABLE", "Available")
    event["AgentARN"] = "arn:aws:connect:us-east-1:123:instance/abc/agent/agent-001"
    event["AgentSnapshot"]["Configuration"] = {
        "RoutingProfile": {
            "DefaultOutboundQueue": {"ARN": "arn:aws:connect:us-east-1:123:instance/abc/queue/q1"},
            "Concurrency": [],
        }
    }
    info = extract_agent_info(event)
    assert info["inbound_queue_arns"] == []
