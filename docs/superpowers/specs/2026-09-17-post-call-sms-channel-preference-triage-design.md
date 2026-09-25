# Post-Call SMS Channel-Preference Triage (Phase III, Sub-Project 1) — Design

## Context and naming

This is the first of three sub-projects under the business's "Phase III: The Connect Bot Engagement" scope, which is distinct from — and comes after — this codebase's own "Phase II" plan (`2026-09-16-post-call-sms-lex-triage-design.md`), which built the post-call SMS trigger (Module A) and a 4-intent reply-classification bot (`PostCallSmsReplyTriage`, Module C) already deployed to production.

Per the business's master multi-phase doc: their "Phase II" = only the post-call SMS send (this codebase's Module A). Their "Phase III" = a new Lex bot that asks a patient replying to any SMS whether they prefer to continue by text or by phone call, with two downstream paths (secure-portal handoff via Luma Health, or priority dialer escalation with an agent whisper), plus several compliance guardrails (quiet hours, cross-channel opt-out SLA, frequency capping, collision suppression).

**Decision (already made, not re-litigated here):** the existing 4-intent bot is kept, not replaced. This sub-project's triage only fires after that bot classifies `WantsContact` or `FallbackIntent` — `NotInterested`/`OptOut` keep their current handling unchanged (including the just-shipped fix that records `OptOut` to `VipConnectOptOutList`).

**Scope of this document:** only the text-vs-call triage bot, its conversation-state tracking, and the two downstream handoffs — scoped as abstract interfaces where the real downstream system doesn't exist yet. Priority dialer escalation mechanics are Sub-Project 2. Quiet hours, frequency capping, and collision suppression are Sub-Project 3. Both are out of scope here except where this sub-project must expose a clean interface for them to plug into later.

## Verified ground truth

All file paths below (`post_call_sms_reply_handler.py` and its siblings) live in the `Connect-batch-redis-refactor` repo/worktree, not this repo (`vip-connect-external-campaigns`) — same cross-repo split as the Phase II design doc this one extends: specs live here, that feature's implementation lives there.

- `post_call_sms_reply_handler.py` (deployed, `post-call-sms-reply-handler` Lambda) currently: claims the SNS message via `claim_sns_message`/`complete_sns_message` (Task 4's idempotency module), classifies the reply via `_recognize_intent` (Lex bot `PostCallSmsReplyTriage`, botId `GHARBEKI8Q`, alias `M6HLIHNROD`), writes to `PostCallSmsHistory` via `append_history`, and for `WantsContact`/`FallbackIntent` calls `_start_chat_contact` (Connect `start_chat_contact` against flow `a9c495dc-99df-4097-9091-83d98e13d2f9` / queue `e90c5698-19f3-44aa-804e-3e88d8324542`). `OptOut` now also calls `_record_opt_out(phone)` (commit `8a48b52`, writing to `VipConnectOptOutList`).
- Lex bots in this account disable conversation logging by omission at `create_bot_alias` time (verified pattern from Task 8: no `conversationLogSettings` key present).
- No Luma Health integration exists anywhere in any repo (`vip-connect-external-campaigns`, `Connect-batch-redis-refactor`) — confirmed by direct search. One planning doc (`2026-09-09-precall-sms-phase1.md`) explicitly excludes it from Phase I scope.
- No conversation-state tracking exists anywhere in this feature — every inbound reply today is classified and handled independently, with no memory of prior turns.
- No priority-dialer-escalation mechanism exists anywhere yet (confirmed: no "NPL", "priority queue", or "HVO" hits in code) — this sub-project must not invent that mechanism, only expose a call site for it.

## Decisions (resolved during brainstorming)

- Single Lambda, not a second competing consumer: `post_call_sms_reply_handler.py` is extended in place, not split into a parallel Lambda. One consumer per SNS message avoids two Lambdas racing on the same idempotency claim.
- New DynamoDB table `PostCallSmsConversationState` (PK `phone`), tracking a single pending-state string per phone number. No TTL — an unanswered triage question stays pending indefinitely (no auto-expiry, no auto-escalation-to-call-on-timeout). This may be revisited once real-world response-time data exists, but is the explicit decision for this iteration.
- Same origination number (`+13466801790` / `arn:aws:sms-voice:us-east-1:165505826690:phone-number/phone-261674c2628b42c083361e9f8611bb2c`) sends the triage question — same conversation thread the patient already sees.
- New dedicated Lex bot `PostCallSmsChannelPreference` (2 intents: `PreferText`, `PreferCall`), logging disabled — mirrors the existing bot's provisioning pattern exactly (own bot, not new intents bolted onto the existing one — Lex sessions here are single-turn per reply, so a second bot for a second question is the same shape already established).
- Luma Health handoff and priority-call escalation are BOTH modeled as abstract call sites (`luma_client.send_secure_link(phone)`, `priority_escalation.escalate_priority_callback(phone)`) that currently raise `NotImplementedError` with a message pointing at the missing real integration. This is a deliberate, visible blocker — not a silently-simulated success — until Luma's real API contract exists and Sub-Project 2 is designed.
- **Opt-out detection runs on every inbound reply, unconditionally, before any state check.** (Added after adversarial review — see below.) A literal keyword scan (same `OPT_OUT_KEYWORDS` convention as `inbound_sms_handler.py`: STOP/QUIT/UNSUBSCRIBE/CANCEL/END) runs first, regardless of pending-state or intent classification. This closes two gaps the adversarial review found: (1) a free-text opt-out sent while a triage question is outstanding was never checked (the 2-intent `PreferText`/`PreferCall` bot has no `OptOut` intent, and pending state has no TTL, so that window could last indefinitely); (2) this design's own new outbound send (the triage question itself) had no opt-out gate before sending, unlike `sms_sender_handler.py`'s `is_blocked()` pattern.
- **`clear_pending_state` runs only after its downstream call succeeds, never before.** (Added after adversarial review.) The first draft cleared the pending state before calling `send_secure_link`/`escalate_priority_callback` — since both currently always raise `NotImplementedError`, that ordering meant every real `PreferCall`/`PreferText` reply would have its pending state wiped even though the intended action never happened, and a retry would silently fall through to the original 4-intent classification path instead of retrying the escalation. Corrected below.

## Module design

### `post_call_sms_conversation_state.py` (new)

Mirrors the shape of `post_call_sms_ctr_idempotency.py`/`post_call_sms_reply_idempotency.py` (same account convention: CMK-encrypted table, PITR, deletion protection, matching `ConnectCampaignCtrIdempotency`'s provisioned settings).

```python
def get_pending_state(phone: str) -> str | None: ...
def set_pending_state(phone: str, state: str) -> None: ...
def clear_pending_state(phone: str) -> None: ...
```

Only one state value exists today: exported as a module-level constant `AWAITING_CHANNEL_PREFERENCE = "awaiting_channel_preference"` (not a repeated literal string at each call site). The function signatures are state-value-agnostic (any string) so a future state can be added without a signature change.

### `luma_client.py` (new)

```python
def send_secure_link(phone: str) -> None:
    """Send a Luma Health secure-chat link to phone.

    NOT YET IMPLEMENTED: no Luma Health API contract exists yet (no
    credentials, no documented endpoint). Raises NotImplementedError
    until that integration is designed and built. This function is the
    intended single call site for that future work — do not scatter a
    second Luma integration point elsewhere.
    """
    raise NotImplementedError("Luma Health API integration not yet built — see design doc 2026-09-17-post-call-sms-channel-preference-triage-design.md")
```

### `priority_escalation.py` (new)

```python
def escalate_priority_callback(phone: str) -> None:
    """Move phone's lead to the front of the priority dialer queue.

    NOT YET IMPLEMENTED: Sub-Project 2 (priority escalation + agent
    whisper) has not been designed yet. Raises NotImplementedError until
    that design exists. This function is the intended single call site
    for that future work.
    """
    raise NotImplementedError("Priority dialer escalation not yet built — see Sub-Project 2 (not yet designed)")
```

### `provision_post_call_sms_channel_preference_lex_bot.py` (new)

Same shape as `provision_post_call_sms_lex_bot.py` (Task 8): `build_bot_definition() -> dict` (pure, testable), `build_bot_alias_settings() -> dict` (omits `conversationLogSettings`), `main()` calls the real `lexv2-models` APIs — never executed by tests or by an implementer, run manually by the controller with user coordination exactly like Task 8.

Two intents:
- `PreferText`: sample utterances `"text"`, `"I'd rather text"`, `"keep texting"`, `"text is fine"`.
- `PreferCall`: sample utterances `"call me"`, `"call"`, `"phone call"`, `"have someone call me"`.
- `FallbackIntent`: built-in (not created manually), same as the existing bot.

### `post_call_sms_opt_out_check.py` (new)

A small, self-contained helper — this repo has no shared-code layer (`vip_shared`), so this mirrors `inbound_sms_handler.py`'s own literal-keyword convention rather than importing anything cross-repo.

```python
OPT_OUT_KEYWORDS = {"STOP", "QUIT", "UNSUBSCRIBE", "CANCEL", "END"}

def is_opt_out_keyword(message_body: str) -> bool:
    """Exact-match (not substring) opt-out keyword check, matching
    inbound_sms_handler.py's own convention exactly: strips whitespace/
    punctuation, uppercases, then requires an exact match against
    OPT_OUT_KEYWORDS. "please stop calling me" does NOT match here — that
    free-text case is instead caught by the main 4-intent bot's OptOut
    classification when this Lambda is not mid-triage. This function's
    entire purpose is covering the pending-triage window, where the
    2-intent PreferText/PreferCall bot has no OptOut intent at all.
    """
    keyword = message_body.strip(string.whitespace + string.punctuation).upper()
    return keyword in OPT_OUT_KEYWORDS


def is_on_opt_out_list(phone: str) -> bool:
    """True if phone already has an item in VipConnectOptOutList.

    Mirrors services/api-sms/src/sms_sender_handler.py's is_blocked(phone)
    check (that repo's vip_shared.OptOutRepository) — same table, same
    read-before-send gate, reimplemented locally per this repo's
    self-contained-flat-file convention.
    """
    table = boto3.resource("dynamodb").Table(os.environ.get("OPT_OUT_TABLE", "VipConnectOptOutList"))
    return "Item" in table.get_item(Key={"ContactNumber": phone})
```

### `post_call_sms_reply_handler.py` (modified)

New env vars: `CHANNEL_PREFERENCE_LEX_BOT_ID`, `CHANNEL_PREFERENCE_LEX_BOT_ALIAS_ID` (Sub-Project 1's bot), reuses existing `POST_CALL_SMS_ORIGINATION_ARN`-equivalent send path (via `post_call_sms_send.send_single_sms`, Task 5's module — already used by the trigger Lambda, now imported here too).

New per-record control flow (inserted before the existing `_recognize_intent` call). **Opt-out detection now runs first, unconditionally, before any state check** — this is the fix for the two gaps the adversarial review found:

```python
# Runs BEFORE the pending-state check and BEFORE any Lex classification —
# closes the gap where a literal "STOP" sent mid-triage (2-intent bot has
# no OptOut intent) would never reach VipConnectOptOutList.
if is_opt_out_keyword(body):
    append_history(phone, "inbound", body, intent="OptOut")
    _record_opt_out(phone)
    clear_pending_state(phone)  # unconditional clear: opt-out ends any in-flight triage
    complete_sns_message(message_id)
    continue

pending = get_pending_state(phone)
if pending == "awaiting_channel_preference":
    preference = _recognize_channel_preference(message_id, body)  # new helper, same sessionId-on-message_id pattern as _recognize_intent
    append_history(phone, "inbound", body, intent=preference)
    if preference == "PreferText":
        send_secure_link(phone)
        clear_pending_state(phone)  # only after the call succeeds — see rationale below
    elif preference == "PreferCall":
        escalate_priority_callback(phone)
        clear_pending_state(phone)  # only after the call succeeds
    else:  # FallbackIntent on the channel-preference bot — no silent default per the "no timeout" decision
        logger.info("Unclear channel preference phone=****%s — leaving unresolved", _last4(phone))
        # state intentionally left pending: an unclear reply does not end the wait
    complete_sns_message(message_id)
    continue

# existing classification path, unchanged, EXCEPT:
intent = _recognize_intent(message_id, body)
append_history(phone, "inbound", body, intent=intent)
if intent == "OptOut":
    _record_opt_out(phone)  # kept for defense-in-depth: Lex may classify OptOut on
                             # phrasing the literal keyword check above doesn't catch
                             # (e.g. "please unsubscribe me"), same as it does today
if intent in _ROUTES_TO_QUEUE:  # WantsContact, FallbackIntent
    if not is_on_opt_out_list(phone):  # closes the gap: this Lambda's own new outbound
                                        # send had no opt-out gate before this fix
        send_single_sms(phone, _CHANNEL_PREFERENCE_QUESTION)
        set_pending_state(phone, "awaiting_channel_preference")
        routed += 1  # kept for existing metric continuity; renamed meaning: "advanced to next step", not "routed to a human queue" anymore
    else:
        logger.info("Skipping triage question, phone on opt-out list phone=****%s", _last4(phone))
else:
    logger.info(...)
complete_sns_message(message_id)
```

**Why `clear_pending_state` moved to after the downstream call, not before:** the first draft cleared the pending state unconditionally, then called `send_secure_link`/`escalate_priority_callback` — both of which currently always raise `NotImplementedError`. Since the exception aborts `lambda_handler` before `complete_sns_message` is reached, the SNS message itself gets retried — but the pending state had *already* been wiped, so the retry would see no pending state and fall through to the original 4-intent classification path instead of retrying the actual escalation, silently misrouting every real `PreferCall`/`PreferText` reply today. Clearing state only after the call returns without raising means a retry correctly finds the state still pending and retries the real action.

**Note the behavior change:** `WantsContact`/`FallbackIntent` no longer call `_start_chat_contact` directly — they now send the triage question and wait. `_start_chat_contact` itself is NOT deleted (still used nowhere in this design, since neither triage answer routes to a chat contact — `PreferCall` routes to the dialer, `PreferText` routes to Luma — but keeping the function avoids a churn-only deletion; a follow-up cleanup task can remove it once confirmed genuinely dead).

**Triage question copy (approved, mirrors the business's own script verbatim):**
```
"I'd love to help you get scheduled! To protect your privacy, would you prefer to continue this via secure text, or would you like me to have an agent call you right now?"
```

## Error handling

- `send_secure_link`/`escalate_priority_callback` raising `NotImplementedError`: NOT caught, and — critically — raised *before* `clear_pending_state` runs (see above). Propagates uncaught out of `lambda_handler`, same as any other unhandled exception in this Lambda today — the message is left unclaimed-complete (idempotency claim already committed via `claim_sns_message`, `complete_sns_message` never reached), so it is retried by SNS's own retry policy until the real implementation lands, and the retry correctly finds the pending state still set. This is intentional and visible, not a silent no-op.
- `set_pending_state`/`get_pending_state`/`clear_pending_state` follow the existing idempotency modules' pattern: real DynamoDB errors are not swallowed, they propagate.
- The literal opt-out keyword check (`is_opt_out_keyword`) runs before any other branch, including before the pending-state check — an opt-out always wins, even mid-triage, and unconditionally clears any pending state (an opt-out ends the conversation regardless of what question was outstanding).
- `is_on_opt_out_list` gates the new triage-question send — if a phone is already on `VipConnectOptOutList` by the time `WantsContact`/`FallbackIntent` would have triggered the triage question, the question is not sent and no pending state is set (matches `sms_sender_handler.py`'s existing pre-send gate pattern, reimplemented locally).
- No PHI in any new log statement — same masking convention (`_last4`), never log `phone` or `body` unmasked, never log the Luma/escalation call's payload.

## Testing

TDD throughout, mocking `boto3` clients exactly like every existing task in this feature. No real Luma or dialer-escalation calls are possible (both raise `NotImplementedError` — a test exercising that path asserts the exception, not a fake success). Test cases include:
- No pending state + `WantsContact` + not opted out → triage question sent + state set (not chat-started).
- No pending state + `WantsContact` + already on `VipConnectOptOutList` → triage question NOT sent, no state set.
- A literal `"STOP"` reply while a triage question is pending → `_record_opt_out` called, pending state cleared, `_recognize_channel_preference` NEVER called (opt-out short-circuits before the pending-state branch).
- A literal `"stop"`/`"Stop!"` (punctuation/case variants) still matches, `"please stop calling me"` does NOT match the literal check (falls through to the normal path, where Lex's own OptOut classification catches it as it does today).
- Pending state + `PreferText` → `send_secure_link` called, state cleared ONLY after that call returns; a test using a mock that raises `NotImplementedError` asserts `pytest.raises` AND asserts the pending state was never cleared (mock `clear_pending_state` and assert `assert_not_called()` on the exception path).
- Pending state + `PreferCall` → same shape as above, `escalate_priority_callback`.
- Pending state + unclear/Fallback → state intentionally left pending (not cleared), logged, no call, no crash.
- Existing `NotInterested`/direct-Lex-`OptOut` paths unchanged (regression coverage carried forward).

## Adversarial review

Reviewed via the `adversarial-code-review` workflow before implementation. Three real, distinct defects were found (all independently confirmed by 3 review dimensions each) and are closed in the design above: (1) `clear_pending_state` ordering bug that would have silently misrouted every real `PreferCall`/`PreferText` reply on the very first attempt, since both downstream calls unconditionally raise today; (2) the pending-triage window had no opt-out detection at all; (3) the new triage-question outbound send had no opt-out gate before sending. Two findings were reviewed and correctly refuted as false positives (a cross-repo citation-clarity nit about where the "ground truth" facts actually live, and a speculative no-TTL/retention concern that conflated two unrelated effects) — see the workflow's decision log for the full reasoning. No technical debt from this review is left open in this document.

## Open items (deferred, not blocking this sub-project)

- Real Luma Health API contract — blocks `send_secure_link`'s real implementation, not this sub-project's own code/tests.
- Sub-Project 2 (priority escalation + whisper) — blocks `escalate_priority_callback`'s real implementation.
- Sub-Project 3 (quiet hours, frequency capping, collision suppression) — none of these gates exist yet anywhere in this reply-handling path; this sub-project does not add them, by explicit scope decision.
- The quiet-hours window discrepancy (8am–8pm per the business doc vs. 8am–9pm in `vip_shared`'s existing `quiet_hours.py`) needs resolution before Sub-Project 3, not before this one.
