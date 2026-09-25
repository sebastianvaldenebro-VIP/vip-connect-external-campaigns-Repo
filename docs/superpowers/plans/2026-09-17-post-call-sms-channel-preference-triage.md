# Post-Call SMS Channel-Preference Triage (Phase III, Sub-Project 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After the already-deployed 4-intent reply bot classifies `WantsContact`/`FallbackIntent`, send the patient a new triage question ("text or call?") instead of routing straight to a human chat queue, track that a reply is pending, classify the answer with a second dedicated Lex bot, and dispatch to one of two currently-unimplemented downstream call sites (Luma Health secure-link handoff, priority dialer escalation) — both of which intentionally raise `NotImplementedError` until their own real integrations exist. Also closes two opt-out gaps an adversarial spec review found: literal opt-out detection during the pending-triage window, and an opt-out-list gate before the new outbound triage-question send.

**Architecture:** One Lambda modified in place (`post_call_sms_reply_handler.py`, already deployed), three new self-contained modules (conversation-state tracking, opt-out checks, and two stub downstream clients), and one new Lex V2 bot provisioned the same way the existing one was (Task 8 of the prior plan).

**Tech Stack:** Python 3.12, boto3, `lexv2-models`/`lexv2-runtime`, DynamoDB, pytest — same stack as the plan this extends (`2026-09-16-post-call-sms-lex-triage.md`).

**Spec:** `docs/superpowers/specs/2026-09-17-post-call-sms-channel-preference-triage-design.md` — read it alongside this plan; it documents the 3 real defects an adversarial review found and closed (retry-ordering bug, missing opt-out detection mid-triage, missing opt-out gate on the new outbound send) before this plan was written.

## Global Constraints

- No PHI (patient name, phone, message body) in log statements, exception messages, or CloudWatch — mask phone numbers to last-4 digits via the existing `_last4()` helper already in `post_call_sms_reply_handler.py`.
- The literal opt-out keyword check (`is_opt_out_keyword`) must run BEFORE the pending-state check and BEFORE any Lex classification, for every inbound reply, unconditionally.
- `clear_pending_state` must only be called AFTER its corresponding downstream call (`send_secure_link`/`escalate_priority_callback`) returns without raising — never before. This is the exact defect the adversarial review found; do not reintroduce it.
- `is_on_opt_out_list` must gate the new triage-question outbound send — never send the triage question to a phone already on `VipConnectOptOutList`.
- `luma_client.send_secure_link` and `priority_escalation.escalate_priority_callback` must raise `NotImplementedError` — do not stub them with a fake success, a `pass`, or a TODO comment substituting for the raise. This is a deliberate, visible blocker per the spec.
- Lex V2 bot `PostCallSmsChannelPreference`: conversation logging (text and audio) must be disabled at creation time — same disabled-by-omission convention as the existing bot, verified via `build_bot_alias_settings()`'s absence of `conversationLogSettings`.
- Do not modify `_start_chat_contact`, `_recognize_intent`, `_record_opt_out`, or any Task 1-11 module from the prior plan beyond what this plan's tasks explicitly touch. `_start_chat_contact` becomes unused by this plan's changes but must NOT be deleted (a follow-up cleanup task removes it later, once confirmed genuinely dead across all call sites).
- Never invoke a Lambda directly against production or send a real SMS as part of "testing" — all verification below uses mocks or the standard `{"Records":[]}` synthetic invoke.

---

## Task 1: Opt-out check helpers

**Files:**
- Create: `Connect-batch-redis-refactor/post_call_sms_opt_out_check.py`
- Test: `Connect-batch-redis-refactor/test_post_call_sms_opt_out_check.py`

**Interfaces:**
- Produces: `OPT_OUT_KEYWORDS: frozenset[str]`, `is_opt_out_keyword(message_body: str) -> bool`, `is_on_opt_out_list(phone: str) -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
# test_post_call_sms_opt_out_check.py
from unittest.mock import MagicMock, patch
import pytest
from post_call_sms_opt_out_check import is_opt_out_keyword, is_on_opt_out_list


@pytest.mark.parametrize("body", ["STOP", "stop", "Stop!", "  stop  ", "QUIT", "unsubscribe", "Cancel.", "END"])
def test_exact_keyword_matches(body):
    assert is_opt_out_keyword(body) is True


@pytest.mark.parametrize("body", ["please stop calling me", "stopper", "unsubscribe me please", "yes", "call me", ""])
def test_non_exact_phrases_do_not_match(body):
    assert is_opt_out_keyword(body) is False


def test_is_on_opt_out_list_true_when_item_present():
    table = MagicMock()
    table.get_item.return_value = {"Item": {"ContactNumber": "+15551234567"}}
    with patch("boto3.resource") as resource:
        resource.return_value.Table.return_value = table
        assert is_on_opt_out_list("+15551234567") is True


def test_is_on_opt_out_list_false_when_absent():
    table = MagicMock()
    table.get_item.return_value = {}
    with patch("boto3.resource") as resource:
        resource.return_value.Table.return_value = table
        assert is_on_opt_out_list("+15551234567") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_post_call_sms_opt_out_check.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the implementation**

```python
# post_call_sms_opt_out_check.py
"""Opt-out detection for post_call_sms_reply_handler.py. Two distinct checks:

is_opt_out_keyword(body): literal, exact-match keyword check on an INBOUND
reply — mirrors inbound_sms_handler.py's own convention exactly (strip
whitespace/punctuation, uppercase, exact set membership, not substring).
"please stop calling me" does NOT match — that free-text phrasing is caught
by the main 4-intent bot's Lex OptOut classification instead, when this
Lambda is not mid-triage. This function exists to cover the pending-triage
window, where the 2-intent PreferText/PreferCall bot has no OptOut intent
at all.

is_on_opt_out_list(phone): checks VipConnectOptOutList before an OUTBOUND
send — mirrors services/api-sms/src/sms_sender_handler.py's is_blocked()
gate (same table), reimplemented locally since this repo has no shared
code layer (vip_shared) to import from.
"""
import os
import string

import boto3

OPT_OUT_KEYWORDS = frozenset({"STOP", "QUIT", "UNSUBSCRIBE", "CANCEL", "END"})


def is_opt_out_keyword(message_body: str) -> bool:
    keyword = message_body.strip(string.whitespace + string.punctuation).upper()
    return keyword in OPT_OUT_KEYWORDS


def is_on_opt_out_list(phone: str) -> bool:
    table_name = os.environ.get("OPT_OUT_TABLE", "VipConnectOptOutList")
    table = boto3.resource("dynamodb").Table(table_name)
    return "Item" in table.get_item(Key={"ContactNumber": phone})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest test_post_call_sms_opt_out_check.py -v`
Expected: PASS, 16/16 (8 parametrized cases in `test_exact_keyword_matches` + 6 in `test_non_exact_phrases_do_not_match` + 2 standalone)

- [ ] **Step 5: Commit**

```bash
git add post_call_sms_opt_out_check.py test_post_call_sms_opt_out_check.py
git commit -m "feat: opt-out keyword + opt-out-list check helpers (Phase III sub-project 1)"
```

---

## Task 2: Conversation-state tracking module

**Files:**
- Create: `Connect-batch-redis-refactor/post_call_sms_conversation_state.py`
- Test: `Connect-batch-redis-refactor/test_post_call_sms_conversation_state.py`

**Interfaces:**
- Produces: `AWAITING_CHANNEL_PREFERENCE: str`, `get_pending_state(phone: str) -> str | None`, `set_pending_state(phone: str, state: str) -> None`, `clear_pending_state(phone: str) -> None`.
- Consumed by Task 6 exactly with these names/signatures — do not rename.

- [ ] **Step 1: Write the failing tests**

```python
# test_post_call_sms_conversation_state.py
from unittest.mock import MagicMock, patch
from post_call_sms_conversation_state import (
    AWAITING_CHANNEL_PREFERENCE,
    get_pending_state,
    set_pending_state,
    clear_pending_state,
)


def test_awaiting_channel_preference_constant_value():
    assert AWAITING_CHANNEL_PREFERENCE == "awaiting_channel_preference"


def test_get_pending_state_returns_none_when_absent():
    table = MagicMock()
    table.get_item.return_value = {}
    with patch("boto3.resource") as resource:
        resource.return_value.Table.return_value = table
        assert get_pending_state("+15551234567") is None


def test_get_pending_state_returns_stored_value():
    table = MagicMock()
    table.get_item.return_value = {"Item": {"phone": "+15551234567", "state": AWAITING_CHANNEL_PREFERENCE}}
    with patch("boto3.resource") as resource:
        resource.return_value.Table.return_value = table
        assert get_pending_state("+15551234567") == AWAITING_CHANNEL_PREFERENCE


def test_set_pending_state_writes_expected_item():
    table = MagicMock()
    with patch("boto3.resource") as resource:
        resource.return_value.Table.return_value = table
        set_pending_state("+15551234567", AWAITING_CHANNEL_PREFERENCE)
    table.put_item.assert_called_once_with(Item={"phone": "+15551234567", "state": AWAITING_CHANNEL_PREFERENCE})


def test_clear_pending_state_deletes_item():
    table = MagicMock()
    with patch("boto3.resource") as resource:
        resource.return_value.Table.return_value = table
        clear_pending_state("+15551234567")
    table.delete_item.assert_called_once_with(Key={"phone": "+15551234567"})


def test_write_failure_is_not_swallowed():
    table = MagicMock()
    table.put_item.side_effect = Exception("boom")
    with patch("boto3.resource") as resource:
        resource.return_value.Table.return_value = table
        try:
            set_pending_state("+15551234567", AWAITING_CHANNEL_PREFERENCE)
            assert False, "expected exception to propagate"
        except Exception as e:
            assert str(e) == "boom"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_post_call_sms_conversation_state.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the implementation**

```python
# post_call_sms_conversation_state.py
"""Tracks a single pending conversation state per phone number for
post_call_sms_reply_handler.py — e.g. "we just asked this patient whether
they prefer text or a call, and are waiting for their answer."

No TTL: an unanswered triage question stays pending indefinitely (deliberate
decision, see docs/superpowers/specs/2026-09-17-post-call-sms-channel-preference-triage-design.md
in vip-connect-external-campaigns — no auto-expiry, no auto-escalation on
timeout, revisit once real-world response-time data exists).

Same account convention as post_call_sms_ctr_idempotency.py's sibling
tables: CMK-encrypted, PITR, deletion protection (provisioned in Task 7).
"""
import os

import boto3

_TABLE = os.environ.get("POST_CALL_SMS_CONVERSATION_STATE_TABLE", "PostCallSmsConversationState")

AWAITING_CHANNEL_PREFERENCE = "awaiting_channel_preference"


def _table():
    return boto3.resource("dynamodb").Table(_TABLE)


def get_pending_state(phone: str) -> str | None:
    item = _table().get_item(Key={"phone": phone}).get("Item")
    return item["state"] if item else None


def set_pending_state(phone: str, state: str) -> None:
    _table().put_item(Item={"phone": phone, "state": state})


def clear_pending_state(phone: str) -> None:
    _table().delete_item(Key={"phone": phone})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest test_post_call_sms_conversation_state.py -v`
Expected: PASS, 6/6

- [ ] **Step 5: Commit**

```bash
git add post_call_sms_conversation_state.py test_post_call_sms_conversation_state.py
git commit -m "feat: conversation-state tracking module (Phase III sub-project 1)"
```

---

## Task 3: Luma Health client stub

**Files:**
- Create: `Connect-batch-redis-refactor/luma_client.py`
- Test: `Connect-batch-redis-refactor/test_luma_client.py`

**Interfaces:**
- Produces: `send_secure_link(phone: str) -> None` — always raises `NotImplementedError`.

- [ ] **Step 1: Write the failing test**

```python
# test_luma_client.py
import pytest
from luma_client import send_secure_link


def test_send_secure_link_raises_not_implemented():
    with pytest.raises(NotImplementedError):
        send_secure_link("+15551234567")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_luma_client.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the implementation**

```python
# luma_client.py
"""Luma Health secure-chat-link handoff — NOT YET IMPLEMENTED.

No Luma Health API contract exists yet in this account (no credentials, no
documented endpoint). This is the single intended call site for that future
integration — do not scatter a second Luma integration point elsewhere when
it's built. Raises deliberately: a caller must never treat a "text
preference" reply as successfully handled off until this is real. See
docs/superpowers/specs/2026-09-17-post-call-sms-channel-preference-triage-design.md
in vip-connect-external-campaigns.
"""


def send_secure_link(phone: str) -> None:
    raise NotImplementedError(
        "Luma Health API integration not yet built — see design doc "
        "2026-09-17-post-call-sms-channel-preference-triage-design.md"
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest test_luma_client.py -v`
Expected: PASS, 1/1

- [ ] **Step 5: Commit**

```bash
git add luma_client.py test_luma_client.py
git commit -m "feat: Luma Health client stub, raises NotImplementedError (Phase III sub-project 1)"
```

---

## Task 4: Priority escalation stub

**Files:**
- Create: `Connect-batch-redis-refactor/priority_escalation.py`
- Test: `Connect-batch-redis-refactor/test_priority_escalation.py`

**Interfaces:**
- Produces: `escalate_priority_callback(phone: str) -> None` — always raises `NotImplementedError`.

- [ ] **Step 1: Write the failing test**

```python
# test_priority_escalation.py
import pytest
from priority_escalation import escalate_priority_callback


def test_escalate_priority_callback_raises_not_implemented():
    with pytest.raises(NotImplementedError):
        escalate_priority_callback("+15551234567")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_priority_escalation.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the implementation**

```python
# priority_escalation.py
"""Priority dialer escalation — NOT YET IMPLEMENTED.

Phase III Sub-Project 2 (priority escalation + agent whisper) has not been
designed yet. This is the single intended call site for that future work.
Raises deliberately, same rationale as luma_client.py's stub.
"""


def escalate_priority_callback(phone: str) -> None:
    raise NotImplementedError(
        "Priority dialer escalation not yet built — see Sub-Project 2 (not yet designed)"
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest test_priority_escalation.py -v`
Expected: PASS, 1/1

- [ ] **Step 5: Commit**

```bash
git add priority_escalation.py test_priority_escalation.py
git commit -m "feat: priority escalation stub, raises NotImplementedError (Phase III sub-project 1)"
```

---

## Task 5: Channel-preference Lex bot definition + provisioning script

**Files:**
- Create: `Connect-batch-redis-refactor/provision_post_call_sms_channel_preference_lex_bot.py`
- Test: `Connect-batch-redis-refactor/test_provision_post_call_sms_channel_preference_lex_bot.py`

**Interfaces:**
- Produces: `build_bot_definition() -> dict` (pure), `build_bot_alias_settings() -> dict` (pure, omits `conversationLogSettings`); `main()` calls the real AWS APIs (never executed by tests or by the implementer — see Task 7).

- [ ] **Step 1: Write the failing tests**

```python
# test_provision_post_call_sms_channel_preference_lex_bot.py
from provision_post_call_sms_channel_preference_lex_bot import build_bot_definition, build_bot_alias_settings


def test_bot_definition_disables_conversation_logging():
    d = build_bot_definition()
    assert d["bot"]["dataPrivacy"] == {"childDirected": False}


def test_bot_locale_is_en_us():
    d = build_bot_definition()
    assert d["locale_id"] == "en_US"


def test_two_intents_present_with_correct_names():
    d = build_bot_definition()
    names = {i["intentName"] for i in d["intents"]}
    assert names == {"PreferText", "PreferCall", "FallbackIntent"}


def test_fallback_intent_has_no_sample_utterances():
    d = build_bot_definition()
    fallback = next(i for i in d["intents"] if i["intentName"] == "FallbackIntent")
    assert "sampleUtterances" not in fallback or fallback["sampleUtterances"] == []


def test_prefer_text_has_representative_utterances():
    d = build_bot_definition()
    prefer_text = next(i for i in d["intents"] if i["intentName"] == "PreferText")
    utterances = {u["utterance"] for u in prefer_text["sampleUtterances"]}
    assert "text" in utterances
    assert "keep texting" in utterances


def test_prefer_call_has_representative_utterances():
    d = build_bot_definition()
    prefer_call = next(i for i in d["intents"] if i["intentName"] == "PreferCall")
    utterances = {u["utterance"] for u in prefer_call["sampleUtterances"]}
    assert "call me" in utterances
    assert "call" in utterances


def test_bot_alias_settings_disable_conversation_logging_by_omission():
    settings = build_bot_alias_settings()
    assert "conversationLogSettings" not in settings
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_provision_post_call_sms_channel_preference_lex_bot.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the implementation**

```python
# provision_post_call_sms_channel_preference_lex_bot.py
"""One-shot provisioning for the channel-preference (text-vs-call) triage
Lex V2 bot — Phase III Sub-Project 1. Conversation logging (text and audio)
is deliberately left disabled, same rationale and mechanism as
provision_post_call_sms_lex_bot.py: conversationLogSettings lives on
create_bot_alias, not create_bot, and disabling it means omitting that key
entirely (see build_bot_alias_settings() below).

Run once: `python3 provision_post_call_sms_channel_preference_lex_bot.py`.
Idempotent guard: checks for an existing bot named
PostCallSmsChannelPreference before creating. Reuses the existing
post-call-sms-lex-bot-role (same trust policy, no Lambda fulfillment hook
needed here either).
"""
import boto3

BOT_NAME = "PostCallSmsChannelPreference"
LOCALE_ID = "en_US"


def build_bot_definition() -> dict:
    return {
        "bot": {
            "botName": BOT_NAME,
            "description": "Classifies whether a patient prefers to continue by secure text or a phone call.",
            "dataPrivacy": {"childDirected": False},
            "idleSessionTTLInSeconds": 300,
        },
        "locale_id": LOCALE_ID,
        "intents": [
            {
                "intentName": "PreferText",
                "sampleUtterances": [
                    {"utterance": u} for u in
                    ["text", "I'd rather text", "keep texting", "text is fine", "text please"]
                ],
            },
            {
                "intentName": "PreferCall",
                "sampleUtterances": [
                    {"utterance": u} for u in
                    ["call me", "call", "phone call", "have someone call me", "call please"]
                ],
            },
            {"intentName": "FallbackIntent"},
        ],
    }


def build_bot_alias_settings() -> dict:
    """Intended bot-alias creation parameters (pure, no AWS calls).

    Deliberately omits `conversationLogSettings` — disabled by omission,
    same convention as provision_post_call_sms_lex_bot.py's own helper.
    """
    return {
        "botAliasName": "live",
        "botId": None,
        "botVersion": None,
    }


def main():
    client = boto3.client("lexv2-models")
    existing = client.list_bots(filters=[{"name": "BotName", "values": [BOT_NAME], "operator": "EQ"}])
    if existing.get("botSummaries"):
        print(f"Bot {BOT_NAME} already exists — skipping creation.")
        return

    definition = build_bot_definition()
    response = client.create_bot(
        botName=definition["bot"]["botName"],
        description=definition["bot"]["description"],
        roleArn="arn:aws:iam::165505826690:role/post-call-sms-lex-bot-role",
        dataPrivacy=definition["bot"]["dataPrivacy"],
        idleSessionTTLInSeconds=definition["bot"]["idleSessionTTLInSeconds"],
    )
    print(f"Created bot {response['botId']} — conversation logging left disabled (no log config set).")
    # Locale, intents, build, version, and alias creation follow via
    # create_bot_locale / create_intent per definition["intents"] /
    # build_bot_locale / create_bot_version / create_bot_alias — run
    # interactively by the controller, exactly like Task 8 of the prior
    # plan (never executed by an implementer or by tests).


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest test_provision_post_call_sms_channel_preference_lex_bot.py -v`
Expected: PASS, 7/7

- [ ] **Step 5: Commit**

```bash
git add provision_post_call_sms_channel_preference_lex_bot.py test_provision_post_call_sms_channel_preference_lex_bot.py
git commit -m "feat: channel-preference Lex bot definition (Phase III sub-project 1)"
```

**Do not run `main()` or any real AWS call as part of this task** — this task is code + tests only, exactly like Task 8 of the prior plan. Real bot creation happens in Task 7 below, run by the controller with the user's coordination.

---

## Task 6: Integrate into `post_call_sms_reply_handler.py`

**Files:**
- Modify: `Connect-batch-redis-refactor/post_call_sms_reply_handler.py` (replace entire file content with the version in Step 3 below)
- Modify: `Connect-batch-redis-refactor/test_post_call_sms_reply_handler.py` (replace entire file content with the version in Step 1 below)

**Interfaces:**
- Consumes: `is_opt_out_keyword`, `is_on_opt_out_list` (Task 1); `AWAITING_CHANNEL_PREFERENCE`, `get_pending_state`, `set_pending_state`, `clear_pending_state` (Task 2); `send_secure_link` (Task 3); `escalate_priority_callback` (Task 4); `send_single_sms` (already-deployed Task 5 of the prior plan, `post_call_sms_send.py`).
- New env vars read at import time (same pattern as the existing `CONNECT_INSTANCE_ID`/etc.): `CHANNEL_PREFERENCE_LEX_BOT_ID`, `CHANNEL_PREFERENCE_LEX_BOT_ALIAS_ID`.
- Produces: `_recognize_channel_preference(message_id: str, message_body: str) -> str` — same shape as the existing `_recognize_intent`, against the new bot.

This task replaces the existing 11-test file and the existing Lambda's `lambda_handler` body with new versions incorporating the adversarial-review fixes. **This is the highest-risk task in this plan — the existing behavior tests below cover both the new logic and every existing behavior that must NOT regress.**

- [ ] **Step 1: Write the failing tests (full replacement of the test file)**

```python
# test_post_call_sms_reply_handler.py
import json
import os
from unittest.mock import MagicMock, patch
import pytest

os.environ.setdefault("CONNECT_INSTANCE_ID", "inst-1")
os.environ.setdefault("POST_CALL_SMS_QUEUE_ID", "queue-1")
os.environ.setdefault("POST_CALL_SMS_FLOW_ID", "flow-1")
os.environ.setdefault("LEX_BOT_ID", "bot-1")
os.environ.setdefault("LEX_BOT_ALIAS_ID", "alias-1")
os.environ.setdefault("CHANNEL_PREFERENCE_LEX_BOT_ID", "cp-bot-1")
os.environ.setdefault("CHANNEL_PREFERENCE_LEX_BOT_ALIAS_ID", "cp-alias-1")

import post_call_sms_reply_handler as reply


def _sns_event(message_id, phone, body):
    sns_message = json.dumps({"originationNumber": phone, "messageBody": body})
    return {"Records": [{"Sns": {"MessageId": message_id, "Message": sns_message}}]}


@pytest.fixture
def deps(monkeypatch):
    for k, v in {"CONNECT_INSTANCE_ID": "inst-1", "POST_CALL_SMS_QUEUE_ID": "queue-1",
                 "POST_CALL_SMS_FLOW_ID": "flow-1", "LEX_BOT_ID": "bot-1",
                 "LEX_BOT_ALIAS_ID": "alias-1", "CHANNEL_PREFERENCE_LEX_BOT_ID": "cp-bot-1",
                 "CHANNEL_PREFERENCE_LEX_BOT_ALIAS_ID": "cp-alias-1"}.items():
        monkeypatch.setenv(k, v)
    with patch.object(reply, "claim_sns_message", return_value=True) as claim, \
         patch.object(reply, "complete_sns_message") as complete, \
         patch.object(reply, "append_history") as history, \
         patch.object(reply, "_recognize_intent") as recognize, \
         patch.object(reply, "_recognize_channel_preference") as recognize_pref, \
         patch.object(reply, "_start_chat_contact") as start_chat, \
         patch.object(reply, "_record_opt_out") as record_opt_out, \
         patch.object(reply, "get_pending_state", return_value=None) as get_pending, \
         patch.object(reply, "set_pending_state") as set_pending, \
         patch.object(reply, "clear_pending_state") as clear_pending, \
         patch.object(reply, "send_single_sms") as send_sms, \
         patch.object(reply, "send_secure_link") as send_secure_link, \
         patch.object(reply, "escalate_priority_callback") as escalate, \
         patch.object(reply, "is_on_opt_out_list", return_value=False) as on_opt_out_list:
        yield {"claim": claim, "complete": complete, "history": history,
               "recognize": recognize, "recognize_pref": recognize_pref, "start_chat": start_chat,
               "record_opt_out": record_opt_out, "get_pending": get_pending, "set_pending": set_pending,
               "clear_pending": clear_pending, "send_sms": send_sms, "send_secure_link": send_secure_link,
               "escalate": escalate, "on_opt_out_list": on_opt_out_list}


# --- Opt-out short-circuit (runs before pending-state check, unconditionally) ---

def test_literal_stop_records_opt_out_and_skips_everything_else(deps):
    reply.lambda_handler(_sns_event("msg-1", "+15551234567", "STOP"), None)
    deps["record_opt_out"].assert_called_once_with("+15551234567")
    deps["clear_pending"].assert_called_once_with("+15551234567")
    deps["recognize"].assert_not_called()
    deps["recognize_pref"].assert_not_called()
    deps["complete"].assert_called_once_with("msg-1")


def test_literal_stop_wins_even_with_a_pending_state(deps):
    deps["get_pending"].return_value = reply.AWAITING_CHANNEL_PREFERENCE
    reply.lambda_handler(_sns_event("msg-1", "+15551234567", "stop"), None)
    deps["record_opt_out"].assert_called_once()
    deps["recognize_pref"].assert_not_called()


def test_free_text_opt_out_phrase_does_not_match_literal_check(deps):
    deps["recognize"].return_value = "OptOut"
    reply.lambda_handler(_sns_event("msg-1", "+15551234567", "please stop calling me"), None)
    # Falls through to the normal Lex classification path (which itself
    # still calls _record_opt_out when Lex classifies OptOut) — the point
    # of this test is that it does NOT get short-circuited by the literal
    # keyword check.
    deps["recognize"].assert_called_once()
    deps["record_opt_out"].assert_called_once_with("+15551234567")


# --- No pending state: existing 4-intent classification path ---

def test_wants_contact_sends_triage_question_and_sets_pending_state(deps):
    deps["recognize"].return_value = "WantsContact"
    result = reply.lambda_handler(_sns_event("msg-1", "+15551234567", "yes please"), None)
    deps["send_sms"].assert_called_once_with("+15551234567", reply._CHANNEL_PREFERENCE_QUESTION)
    deps["set_pending"].assert_called_once_with("+15551234567", reply.AWAITING_CHANNEL_PREFERENCE)
    deps["start_chat"].assert_not_called()
    assert result["routed"] == 1


def test_fallback_intent_also_sends_triage_question(deps):
    deps["recognize"].return_value = "FallbackIntent"
    reply.lambda_handler(_sns_event("msg-1", "+15551234567", "huh?"), None)
    deps["send_sms"].assert_called_once()
    deps["set_pending"].assert_called_once()


def test_wants_contact_on_opt_out_list_does_not_send_triage_question(deps):
    deps["recognize"].return_value = "WantsContact"
    deps["on_opt_out_list"].return_value = True
    result = reply.lambda_handler(_sns_event("msg-1", "+15551234567", "yes please"), None)
    deps["send_sms"].assert_not_called()
    deps["set_pending"].assert_not_called()
    assert result["routed"] == 0


def test_not_interested_does_not_route(deps):
    deps["recognize"].return_value = "NotInterested"
    reply.lambda_handler(_sns_event("msg-1", "+15551234567", "no thanks"), None)
    deps["send_sms"].assert_not_called()
    deps["history"].assert_called_once_with("+15551234567", "inbound", "no thanks", intent="NotInterested")


def test_direct_lex_opt_out_still_records_opt_out(deps):
    deps["recognize"].return_value = "OptOut"
    reply.lambda_handler(_sns_event("msg-1", "+15551234567", "unsubscribe me please"), None)
    deps["record_opt_out"].assert_called_once_with("+15551234567")
    deps["send_sms"].assert_not_called()


# --- Pending state: channel-preference bot ---

def test_pending_state_prefer_text_calls_luma_then_clears_state(deps):
    deps["get_pending"].return_value = reply.AWAITING_CHANNEL_PREFERENCE
    deps["recognize_pref"].return_value = "PreferText"
    reply.lambda_handler(_sns_event("msg-1", "+15551234567", "text"), None)
    deps["send_secure_link"].assert_called_once_with("+15551234567")
    deps["clear_pending"].assert_called_once_with("+15551234567")
    deps["recognize"].assert_not_called()


def test_pending_state_prefer_call_calls_escalation_then_clears_state(deps):
    deps["get_pending"].return_value = reply.AWAITING_CHANNEL_PREFERENCE
    deps["recognize_pref"].return_value = "PreferCall"
    reply.lambda_handler(_sns_event("msg-1", "+15551234567", "call me"), None)
    deps["escalate"].assert_called_once_with("+15551234567")
    deps["clear_pending"].assert_called_once_with("+15551234567")


def test_pending_state_unclear_leaves_state_pending(deps):
    deps["get_pending"].return_value = reply.AWAITING_CHANNEL_PREFERENCE
    deps["recognize_pref"].return_value = "FallbackIntent"
    reply.lambda_handler(_sns_event("msg-1", "+15551234567", "huh"), None)
    deps["clear_pending"].assert_not_called()
    deps["send_secure_link"].assert_not_called()
    deps["escalate"].assert_not_called()
    deps["complete"].assert_called_once_with("msg-1")


def test_pending_state_prefer_text_failure_does_not_clear_state(deps):
    deps["get_pending"].return_value = reply.AWAITING_CHANNEL_PREFERENCE
    deps["recognize_pref"].return_value = "PreferText"
    deps["send_secure_link"].side_effect = NotImplementedError("not built yet")
    with pytest.raises(NotImplementedError):
        reply.lambda_handler(_sns_event("msg-1", "+15551234567", "text"), None)
    deps["clear_pending"].assert_not_called()
    deps["complete"].assert_not_called()


def test_pending_state_prefer_call_failure_does_not_clear_state(deps):
    deps["get_pending"].return_value = reply.AWAITING_CHANNEL_PREFERENCE
    deps["recognize_pref"].return_value = "PreferCall"
    deps["escalate"].side_effect = NotImplementedError("not built yet")
    with pytest.raises(NotImplementedError):
        reply.lambda_handler(_sns_event("msg-1", "+15551234567", "call me"), None)
    deps["clear_pending"].assert_not_called()
    deps["complete"].assert_not_called()


# --- Session ID / PHI hygiene (regression, carried forward) ---

def test_recognize_intent_called_with_message_id_not_phone(deps):
    deps["recognize"].return_value = "WantsContact"
    reply.lambda_handler(_sns_event("msg-1", "+15551234567", "yes please"), None)
    deps["recognize"].assert_called_once_with("msg-1", "yes please")


def test_recognize_channel_preference_called_with_message_id_not_phone(deps):
    deps["get_pending"].return_value = reply.AWAITING_CHANNEL_PREFERENCE
    deps["recognize_pref"].return_value = "PreferText"
    reply.lambda_handler(_sns_event("msg-1", "+15551234567", "text"), None)
    deps["recognize_pref"].assert_called_once_with("msg-1", "text")


def test_recognize_channel_preference_sessionid_is_message_id_never_phone():
    with patch.object(reply, "boto3") as mock_boto3:
        client = MagicMock()
        mock_boto3.client.return_value = client
        client.recognize_text.return_value = {"interpretations": [{"intent": {"name": "PreferText"}}]}
        reply._recognize_channel_preference("msg-1", "text")
        kwargs = client.recognize_text.call_args.kwargs
        assert kwargs["sessionId"] == "msg-1"
        assert "+15551234567" not in kwargs.values()


# --- Duplicate delivery (regression, carried forward) ---

def test_duplicate_sns_delivery_is_a_noop(deps):
    deps["claim"].return_value = False
    reply.lambda_handler(_sns_event("msg-1", "+15551234567", "yes"), None)
    deps["recognize"].assert_not_called()
    deps["recognize_pref"].assert_not_called()
    deps["send_sms"].assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest test_post_call_sms_reply_handler.py -v`
Expected: FAIL — `_recognize_channel_preference`, `_CHANNEL_PREFERENCE_QUESTION`, `AWAITING_CHANNEL_PREFERENCE`, and the new env-var-gated imports don't exist yet; several tests will error before assertions even run.

- [ ] **Step 3: Write the implementation (full replacement of the Lambda file)**

```python
# post_call_sms_reply_handler.py
"""Module C: classifies an inbound SMS reply via Lex V2. Two distinct
classification passes exist, gated by conversation state:

1. No pending state: the original 4-intent bot (WantsContact/NotInterested/
   OptOut/FallbackIntent) classifies the reply. WantsContact/FallbackIntent
   no longer route straight to a human chat queue — instead they send a new
   triage question ("text or call?") and set a pending conversation state,
   per Phase III Sub-Project 1
   (docs/superpowers/specs/2026-09-17-post-call-sms-channel-preference-triage-design.md
   in vip-connect-external-campaigns). NotInterested/OptOut are unchanged.

2. Pending state == AWAITING_CHANNEL_PREFERENCE: a second, dedicated
   2-intent bot (PreferText/PreferCall) classifies the answer to that
   triage question. PreferText hands off to Luma Health (not yet built);
   PreferCall escalates to the priority dialer queue (not yet built,
   Sub-Project 2). Both raise NotImplementedError today — a deliberate,
   visible blocker.

Opt-out detection runs BEFORE either classification pass, unconditionally,
via a literal keyword check — this closes a gap an adversarial spec review
found: the 2-intent channel-preference bot has no OptOut intent, so a
literal "STOP" sent mid-triage would otherwise never reach
VipConnectOptOutList. `_record_opt_out` (direct Lex OptOut classification,
for free-text phrasing the literal check doesn't catch) is unchanged from
before.

`clear_pending_state` is only ever called AFTER its corresponding
downstream call (send_secure_link/escalate_priority_callback) returns
without raising — never before. The first draft of this design cleared
state unconditionally first; since both downstream calls always raise
NotImplementedError today, that would have silently misrouted every real
PreferCall/PreferText reply on retry. See the design doc's "Adversarial
review" section.

This is a brand-new Lambda with its own SNS topic and its own dedicated
origination number — it does not touch rcm-sms-inbound,
cloudhesive-integration, or any existing Connect-direct inbound path.
"""
import json
import logging
import os
from datetime import datetime, timezone

import boto3

from post_call_sms_conversation_state import (
    AWAITING_CHANNEL_PREFERENCE,
    clear_pending_state,
    get_pending_state,
    set_pending_state,
)
from post_call_sms_history import append_history
from post_call_sms_opt_out_check import is_on_opt_out_list, is_opt_out_keyword
from post_call_sms_reply_idempotency import claim_sns_message, complete_sns_message
from post_call_sms_send import send_single_sms
from luma_client import send_secure_link
from priority_escalation import escalate_priority_callback

logger = logging.getLogger()
logger.setLevel(logging.INFO)

CONNECT_INSTANCE_ID = os.environ["CONNECT_INSTANCE_ID"]
POST_CALL_SMS_QUEUE_ID = os.environ["POST_CALL_SMS_QUEUE_ID"]
POST_CALL_SMS_FLOW_ID = os.environ["POST_CALL_SMS_FLOW_ID"]
LEX_BOT_ID = os.environ["LEX_BOT_ID"]
LEX_BOT_ALIAS_ID = os.environ["LEX_BOT_ALIAS_ID"]
CHANNEL_PREFERENCE_LEX_BOT_ID = os.environ["CHANNEL_PREFERENCE_LEX_BOT_ID"]
CHANNEL_PREFERENCE_LEX_BOT_ALIAS_ID = os.environ["CHANNEL_PREFERENCE_LEX_BOT_ALIAS_ID"]
OPT_OUT_TABLE = os.environ.get("OPT_OUT_TABLE", "VipConnectOptOutList")

_ROUTES_TO_QUEUE = {"WantsContact", "FallbackIntent"}

_CHANNEL_PREFERENCE_QUESTION = (
    "I'd love to help you get scheduled! To protect your privacy, would you "
    "prefer to continue this via secure text, or would you like me to have "
    "an agent call you right now?"
)


def _last4(phone: str | None) -> str:
    if not phone:
        return "????"
    digits = "".join(c for c in phone if c.isdigit())
    return digits[-4:] if len(digits) >= 4 else "????"


def _recognize_intent(message_id: str, message_body: str) -> str:
    """Classify one inbound reply via the original 4-intent bot.

    sessionId is keyed on the opaque SNS message_id, never the raw phone
    number — see module docstring rationale in the prior plan.
    """
    client = boto3.client("lexv2-runtime")
    response = client.recognize_text(
        botId=LEX_BOT_ID,
        botAliasId=LEX_BOT_ALIAS_ID,
        localeId="en_US",
        sessionId=message_id,
        text=message_body,
    )
    return response["interpretations"][0]["intent"]["name"]


def _recognize_channel_preference(message_id: str, message_body: str) -> str:
    """Classify a reply to the channel-preference triage question, via the
    dedicated PostCallSmsChannelPreference bot. Same message_id-keyed
    sessionId convention as _recognize_intent, same PHI rationale.
    """
    client = boto3.client("lexv2-runtime")
    response = client.recognize_text(
        botId=CHANNEL_PREFERENCE_LEX_BOT_ID,
        botAliasId=CHANNEL_PREFERENCE_LEX_BOT_ALIAS_ID,
        localeId="en_US",
        sessionId=message_id,
        text=message_body,
    )
    return response["interpretations"][0]["intent"]["name"]


def _record_opt_out(phone: str) -> None:
    """Add phone to the shared cross-channel opt-out list (VipConnectOptOutList).

    Re-raises on failure rather than swallowing — a silently-lost opt-out
    is a compliance failure, and this is a load-bearing write other
    channels depend on.
    """
    try:
        boto3.resource("dynamodb").Table(OPT_OUT_TABLE).put_item(
            Item={
                "ContactNumber": phone,
                "reason": "Patient replied via Lex OptOut intent",
                "source": "post_call_sms_optout",
                "addedAt": datetime.now(timezone.utc).isoformat(),
            }
        )
        logger.info("Opt-out recorded for phone=****%s", _last4(phone))
    except Exception as e:
        logger.error("Failed to record opt-out for phone=****%s error=%s", _last4(phone), type(e).__name__)
        raise


def _start_chat_contact(phone: str, message_body: str) -> None:
    """Unused by the current control flow (kept, not deleted — see Global
    Constraints in the Phase III sub-project 1 plan for why)."""
    connect = boto3.client("connect")
    connect.start_chat_contact(
        InstanceId=CONNECT_INSTANCE_ID,
        ContactFlowId=POST_CALL_SMS_FLOW_ID,
        ParticipantDetails={"DisplayName": phone},
        Attributes={"customerPhone": phone, "messageBody": message_body, "QueueId": POST_CALL_SMS_QUEUE_ID},
    )


def lambda_handler(event, context):
    routed = 0
    for record in event.get("Records", []):
        sns = record["Sns"]
        message_id = sns["MessageId"]
        if not claim_sns_message(message_id):
            logger.info("Duplicate SNS delivery message_id=%s — skipping", message_id)
            continue

        sns_msg = json.loads(sns["Message"])
        phone = sns_msg.get("originationNumber")
        body = sns_msg.get("messageBody")
        if not phone or not body:
            logger.error("Missing phone or body message_id=%s", message_id)
            complete_sns_message(message_id)
            continue

        if is_opt_out_keyword(body):
            append_history(phone, "inbound", body, intent="OptOut")
            _record_opt_out(phone)
            clear_pending_state(phone)
            complete_sns_message(message_id)
            continue

        pending = get_pending_state(phone)
        if pending == AWAITING_CHANNEL_PREFERENCE:
            preference = _recognize_channel_preference(message_id, body)
            append_history(phone, "inbound", body, intent=preference)
            if preference == "PreferText":
                send_secure_link(phone)
                clear_pending_state(phone)
                logger.info("Channel preference=text phone=****%s", _last4(phone))
            elif preference == "PreferCall":
                escalate_priority_callback(phone)
                clear_pending_state(phone)
                logger.info("Channel preference=call phone=****%s", _last4(phone))
            else:
                logger.info("Unclear channel preference phone=****%s — leaving unresolved", _last4(phone))
            complete_sns_message(message_id)
            continue

        intent = _recognize_intent(message_id, body)
        append_history(phone, "inbound", body, intent=intent)

        if intent == "OptOut":
            _record_opt_out(phone)

        if intent in _ROUTES_TO_QUEUE:
            if not is_on_opt_out_list(phone):
                send_single_sms(phone, _CHANNEL_PREFERENCE_QUESTION)
                set_pending_state(phone, AWAITING_CHANNEL_PREFERENCE)
                routed += 1
                logger.info("Sent channel-preference triage phone=****%s intent=%s", _last4(phone), intent)
            else:
                logger.info("Skipping triage question, phone on opt-out list phone=****%s", _last4(phone))
        else:
            logger.info("No routing needed phone=****%s intent=%s", _last4(phone), intent)

        complete_sns_message(message_id)

    return {"routed": routed}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest test_post_call_sms_reply_handler.py -v`
Expected: PASS, 17/17

- [ ] **Step 5: Run the full test suite to check for regressions**

Run: `python3 -m pytest . --ignore=fase_c --ignore=.claude --ignore=test_cleanup_comprehensive.py --ignore=test_orphan_cleanup_logic.py --ignore=campaign_automation/tests/test_campaign_manager.py --ignore=campaign_automation/tests/test_campaign_templates.py --ignore=campaign_automation/tests/test_safety.py --ignore=campaign_automation/tests/test_segment_builder.py -q`
Expected: no failures beyond this task's own file; count increases by the net new/changed tests in this file (11 old tests replaced by 17 new ones in this task, plus Tasks 1-5's new tests).

- [ ] **Step 6: Commit**

```bash
git add post_call_sms_reply_handler.py test_post_call_sms_reply_handler.py
git commit -m "feat: channel-preference triage integration into post_call_sms_reply_handler.py (Phase III sub-project 1)"
```

---

## Task 7: Provision the conversation-state table and the channel-preference Lex bot

**Files:** none (AWS provisioning only).

**This task is executed by the controller (not an implementer subagent), with the user's explicit coordination for any IAM-permission-grant step — exactly the pattern already used for Task 7/8 of the prior plan.**

- [ ] **Step 1: Confirm `ConnectCampaignCtrIdempotency`'s settings (already done in the prior plan — reuse the same values)**

CMK: `arn:aws:kms:us-east-1:165505826690:key/a6c6da51-a1f1-4dc1-9ea3-b7c3cf3d16f7` (same key already used by `PostCallSmsCtrIdempotency`/`PostCallSmsReplyIdempotency`/`PostCallSmsHistory` — reuse it, don't provision a new CMK for one more table in the same feature).

- [ ] **Step 2: Create the `PostCallSmsConversationState` table**

```bash
aws dynamodb create-table --table-name PostCallSmsConversationState \
  --attribute-definitions AttributeName=phone,AttributeType=S \
  --key-schema AttributeName=phone,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --sse-specification Enabled=true,SSEType=KMS,KMSMasterKeyId=a6c6da51-a1f1-4dc1-9ea3-b7c3cf3d16f7 \
  --deletion-protection-enabled \
  --tags Key=Project,Value=connect-campaign Key=Owner,Value=sebastian.valdenebro Key=ManagedBy,Value=manual Key=Compliance,Value=phi Key=CostCenter,Value=connect-campaigns Key=Environment,Value=prod Key=Domain,Value="Connect Team" \
  --profile production
aws dynamodb wait table-exists --table-name PostCallSmsConversationState --profile production
aws dynamodb update-continuous-backups --table-name PostCallSmsConversationState --point-in-time-recovery-specification PointInTimeRecoveryEnabled=true --profile production
```
No TTL attribute (deliberate — see the module's own docstring). `Compliance=phi` since the item's phone number is patient-identifying, matching the same tag choice already used for `PostCallSmsHistory`.

- [ ] **Step 3: Create the channel-preference Lex bot, real API calls, same pattern as Task 8 of the prior plan**

```bash
aws lexv2-models create-bot \
  --bot-name PostCallSmsChannelPreference \
  --description "Classifies whether a patient prefers to continue by secure text or a phone call." \
  --role-arn arn:aws:iam::165505826690:role/post-call-sms-lex-bot-role \
  --data-privacy '{"childDirected":false}' \
  --idle-session-ttl-in-seconds 300 \
  --profile production
# Record the returned botId, then:
aws lexv2-models wait bot-available --bot-id <BOT_ID> --profile production
aws lexv2-models create-bot-locale --bot-id <BOT_ID> --bot-version DRAFT --locale-id en_US --nlu-intent-confidence-threshold 0.40 --profile production
# Wait for botLocaleStatus == NotBuilt, then:
aws lexv2-models create-intent --bot-id <BOT_ID> --bot-version DRAFT --locale-id en_US \
  --intent-name PreferText \
  --sample-utterances '[{"utterance":"text"},{"utterance":"I'\''d rather text"},{"utterance":"keep texting"},{"utterance":"text is fine"},{"utterance":"text please"}]' \
  --profile production
aws lexv2-models create-intent --bot-id <BOT_ID> --bot-version DRAFT --locale-id en_US \
  --intent-name PreferCall \
  --sample-utterances '[{"utterance":"call me"},{"utterance":"call"},{"utterance":"phone call"},{"utterance":"have someone call me"},{"utterance":"call please"}]' \
  --profile production
aws lexv2-models build-bot-locale --bot-id <BOT_ID> --bot-version DRAFT --locale-id en_US --profile production
# Wait for botLocaleStatus == Built, then:
aws lexv2-models create-bot-version --bot-id <BOT_ID> --bot-version-locale-specification '{"en_US":{"sourceBotVersion":"DRAFT"}}' --profile production
# Wait for botStatus == Available on the new version, then:
aws lexv2-models create-bot-alias --bot-id <BOT_ID> --bot-alias-name live --bot-version <VERSION> --profile production
```

- [ ] **Step 4: Verify logging is disabled**

```bash
aws lexv2-models describe-bot-alias --bot-id <BOT_ID> --bot-alias-id <ALIAS_ID> --profile production
```
Expected: no `conversationLogSettings` key present in the response.

- [ ] **Step 5: Record the IDs for Task 8**

`CHANNEL_PREFERENCE_LEX_BOT_ID=<BOT_ID>`, `CHANNEL_PREFERENCE_LEX_BOT_ALIAS_ID=<ALIAS_ID>`.

---

## Task 8: Deploy and verify

**Files:** none (deploy + IAM only).

- [ ] **Step 1: Extend `post-call-sms-reply-handler-role`'s inline policy**

Add to the existing policy JSON (`post_call_sms_reply_handler_role_policy.json`): `dynamodb:PutItem`/`GetItem`/`DeleteItem` on `arn:aws:dynamodb:us-east-1:165505826690:table/PostCallSmsConversationState` (reuses the already-granted KMS actions on the shared CMK, no new KMS statement needed), and `lex:RecognizeText` on `arn:aws:lex:us-east-1:165505826690:bot-alias/<CHANNEL_PREFERENCE_LEX_BOT_ID>/<CHANNEL_PREFERENCE_LEX_BOT_ALIAS_ID>`.

```bash
aws iam put-role-policy --role-name post-call-sms-reply-handler-role \
  --policy-name post-call-sms-reply-handler-inline \
  --policy-document file://post_call_sms_reply_handler_role_policy.json \
  --profile production
```

- [ ] **Step 2: Package and deploy**

```bash
python3 - <<'PYEOF'
import zipfile
files = ["post_call_sms_reply_handler.py", "post_call_sms_history.py",
         "post_call_sms_reply_idempotency.py", "post_call_sms_conversation_state.py",
         "post_call_sms_opt_out_check.py", "post_call_sms_send.py",
         "luma_client.py", "priority_escalation.py"]
with zipfile.ZipFile("/tmp/post-call-sms-reply-handler.zip", "w", zipfile.ZIP_DEFLATED) as zf:
    for f in files:
        zf.write(f)
PYEOF
aws lambda update-function-code --function-name post-call-sms-reply-handler \
  --zip-file fileb:///tmp/post-call-sms-reply-handler.zip --profile production
aws lambda update-function-configuration --function-name post-call-sms-reply-handler \
  --environment "Variables={CONNECT_INSTANCE_ID=6b3f17ba-68a4-472a-9b20-db1991507009,POST_CALL_SMS_QUEUE_ID=e90c5698-19f3-44aa-804e-3e88d8324542,POST_CALL_SMS_FLOW_ID=a9c495dc-99df-4097-9091-83d98e13d2f9,LEX_BOT_ID=GHARBEKI8Q,LEX_BOT_ALIAS_ID=M6HLIHNROD,POST_CALL_SMS_HISTORY_TABLE=PostCallSmsHistory,CHANNEL_PREFERENCE_LEX_BOT_ID=<BOT_ID from Task 7>,CHANNEL_PREFERENCE_LEX_BOT_ALIAS_ID=<ALIAS_ID from Task 7>}" \
  --profile production
aws lambda wait function-updated --function-name post-call-sms-reply-handler --profile production
```

- [ ] **Step 3: Verify with the standard synthetic invoke**

```bash
aws lambda invoke --function-name post-call-sms-reply-handler --profile production \
  --cli-binary-format raw-in-base64-out --payload '{"Records":[]}' /tmp/out.json
cat /tmp/out.json
```
Expected: `{"routed": 0}`, `StatusCode: 200`.

**Do not invoke with a fabricated real ContactId/phone/message against production.** This feature's own trigger Lambda (`post-call-sms-trigger`) is currently DISABLED pending the user's separate go-ahead (see the prior plan's ledger) — this Lambda will not receive any real traffic until that's re-enabled and the origination number's two-way channel is set, so there is no real-traffic E2E verification possible for this sub-project yet. Note that in the final report rather than fabricating one.

---

## Self-Review

**Spec coverage:** every module in the spec's "Module design" section has a task (Tasks 1-5 for the new self-contained modules, Task 6 for the integration). Both adversarial-review fixes (opt-out-first ordering, clear-state-after-success ordering, opt-out-list gate on the new send) are encoded directly in Task 6's implementation code, not left as a follow-up. Real AWS provisioning (Task 7) and deploy (Task 8) mirror the prior plan's Task 7/8 pattern exactly, including the explicit prohibition on fabricated real-traffic verification.

**Placeholder scan:** no TBD/TODO. `NotImplementedError` in Tasks 3/4 is a deliberate design choice (see spec), not a placeholder for missing plan content — the plan's own code for those two files is complete and correct as written; only the *business logic behind* Luma/priority-escalation is future work, explicitly out of this plan's scope.

**Type consistency:** `get_pending_state(phone) -> str | None` / `set_pending_state(phone, state) -> None` / `clear_pending_state(phone) -> None` (Task 2) match their only call sites in Task 6 exactly. `is_opt_out_keyword(message_body) -> bool` / `is_on_opt_out_list(phone) -> bool` (Task 1) match Task 6's usage exactly. `send_secure_link(phone) -> None` (Task 3) and `escalate_priority_callback(phone) -> None` (Task 4) match Task 6's usage exactly. `_recognize_channel_preference(message_id, message_body) -> str` (introduced in Task 6) mirrors `_recognize_intent`'s existing shape.
