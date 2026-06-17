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
