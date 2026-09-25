# Phase II: Post-Call SMS Follow-Up + Lex Reply Triage — Design

Status: reviewed by an adversarial fleet (31 agents, 13 findings — 1 architecture blocker, 4 real design gaps, 2 real non-blocking gaps, 5 false positives after verification); all fix_now/fix_later items resolved below. Continuation of `docs/superpowers/plans/2026-09-09-precall-sms-phase1.md`, gated behind that plan's Task 7 (deferred by explicit user decision — this design proceeds without waiting for it).

## Problem

When an outbound campaign call to a lead goes unanswered or reaches voicemail, nothing follows up with the lead. The business has an approved copy catalog (`Amazon Connect _ Journey Step Texts - Connect Texts_ Journey Steps Proposal.csv`) for exactly this case, keyed by specialty and attempt number, but it is not wired to anything. Separately, when a lead replies to that follow-up text, nothing classifies the reply or gets it to an agent — every existing inbound-SMS path either does not apply to this traffic or is non-functional.

## Scope

**In scope** (Phase II):
- Detect a call ending in no-answer or voicemail (event-driven, off the existing CTR stream).
- Send one SMS from the approved catalog, selected by the lead's specialty + attempt number.
- Classify the lead's reply into one of four intents via a dedicated Amazon Lex V2 bot (en_US).
- Route non-opt-out replies needing a human into Connect's native chat queue.
- Persist message history (both directions) in a dedicated DynamoDB table, in addition to Connect's native chat transcript.

**Out of scope for this design:**
- The 27 `COMPLIANCE PASS=FALSE` and 47 blank rows in the CSV (insurance status, cancellations, procedure outcomes) — legally flagged or undecided; excluded until compliance signs off. Only the 12 `TRUE` rows (all under `STAGE=New Leads`, `FUNNEL TOUCHPOINT=New Leads`, attempts 1st/3rd/5th/7th, specialties Fibroid/Pain/Vein) are in scope.
- Phase III (Luma Health auto-booking handoff). The intent taxonomy below reserves a slot for it but implements nothing beyond routing to a human.
- Fixing the per-campaign static `clinicName` field in Phase I's precall SMS to auto-fill from Customer Profiles `Attributes.location` — deferred at the user's request pending an approved location→clinic-name catalog (compliance concern: `location` is unvalidated Redis data, not pre-approved copy).
- Any change to `connectcampaign_AMD_CTR_handler.py`, `*PostCallSMSFlow`, `*OutboundFollowupSMSFlow`, or `inbound-sms-handler`. The last of these was investigated and found orphaned (never invoked, no SNS subscription, no phone number's `TwoWayChannelArn` points to it) and has been decommissioned (resource policy removed, reserved concurrency set to 0, tagged `Status=DEPRECATED`) rather than reused or deleted.

## Verified ground truth this design relies on

- **CTR stream**: `connectcampaign_AMD_CTR_handler` (repo `Connect-batch-redis-refactor`) consumes Connect's CTR Kinesis stream today. Each record carries `DisconnectReason`, `AnsweringMachineDetectionStatus`, `Campaign.CampaignId` or `Attributes.brandedCampaignId`, and the customer phone. Amazon Connect supports multiple independent consumers on the same stream — confirmed no special wiring is needed to add a second Lambda.
- **No-answer / voicemail signal**: **three** distinct shapes in the same stream (a fleet review caught that the design's original two-shape framing silently dropped the third):
  - True no-answer (call never reached AMD): `DisconnectReason` in `{RING_NO_ANSWER, TELECOM_UNANSWERED}`.
  - Voicemail via the `*Agent-staffed Campaign AMD` flow, wrapped: `DisconnectReason=CONTACT_FLOW_DISCONNECT` with `AnsweringMachineDetectionStatus` in `{VOICEMAIL_BEEP, VOICEMAIL_NO_BEEP, AMD_UNRESOLVED_SILENCE}`. (`connectcampaign_AMD_CTR_handler` deliberately routes this combination to `SKIP` for its own purpose — lead-subgroup advancement already happened in-flow — which is exactly why this needs a separate consumer rather than a branch added to that handler.)
  - Voicemail/AMD as a **standalone top-level `DisconnectReason`**, not wrapped in `CONTACT_FLOW_DISCONNECT`: `VOICEMAIL_BEEP`, `VOICEMAIL_NO_BEEP`, `AMD_UNANSWERED`, `AMD_UNRESOLVED`, `AMD_UNRESOLVED_SILENCE` all appear directly as `DisconnectReason` values in `connectcampaign_AMD_CTR_handler`'s own `DISCONNECT_REASON_ROUTING` table (all routed to `NEXT_ATTEMPT` there), confirming Connect emits them this way via call paths outside the wrapped shape above. Module A's trigger condition below checks both the wrapped and standalone shapes.
- **Specialty and attempt number** are both already present on the Customer Profiles record used for the call, sourced from Redis via the `leads-data-mapping` object type: `Attributes.campaign` (specialty, e.g. `Vein`) and `Attributes.attempt` (e.g. `1st Attempt`). Confirmed via `customer-profiles get-profile-object-type`.
- **Message classification**: `TRANSACTIONAL` (continuation of an in-progress contact attempt, not solicited marketing) — confirmed with the user, no dependency on the pending AWS Support case for the blocked MARKETING 10DLC campaign.
- **Opt-out (STOP/QUIT/UNSUBSCRIBE/CANCEL/END)** is already handled platform-side: every phone number has `SelfManagedOptOutsEnabled=false`, so AWS End User Messaging intercepts these keywords before any Lambda sees them. The Lex `OPT_OUT` intent below is a defense-in-depth safety net, not the primary mechanism.
- **Connect routing profiles** support `VOICE`, `CHAT`, `TASK`, `EMAIL` channels — there is no distinct native "SMS" channel. Connect's SMS support routes inbound SMS as `CHAT`-channel contacts. Several routing profiles already have `CHAT` concurrency configured, so no new queue *type* is needed, only a queue/flow purposed for this traffic.
- **`sms_sender_handler.py` (the Phase I bulk SMS sender) has the wrong contract for Module A.** It requires `campaignId`/`planId`/`runId`/`segmentArn`/`segmentName` and resolves an entire Customer Profiles *segment* — it has no single-recipient entry point. Module A cannot invoke it as originally drafted; see the redesigned Send step below.
- **Lex V2 conversation logging is opt-in at bot-creation time** and, if left on, writes full conversation text (the lead's raw SMS reply — PHI-adjacent) to CloudWatch Logs outside this design's own message-history table and its access controls. Module C must explicitly disable it (or the deployment must confirm BAA coverage for that log group) — see Module C below.
- **The sibling repo's `connectcampaign_leadidupdate.py` has subgroup transitions literally named `"2nd attempt (send text)"`/`"4th attempt (send text)"`/`"6th attempt (send text)"`** in two of its three `SUBGROUP_FLOW` lineages. Confirmed by reading the file: `next_name` is a dict value that is never read anywhere in this codebase — `call_update_api` sends only `subgroupId` (a GUID) to the external Lead API, never `next_name`. So nothing *in this repo* acts on that label. Whether the **external** Lead API/CRM independently sends its own SMS when a lead lands in one of those subgroups is genuinely unverified from here — see the pre-launch verification gate in Module A.

## Architecture

```
[Connect campaign call ends]
        |
        v
   CTR Kinesis stream  ---------------------------+
        |                                          |
        v                                          v
connectcampaign_AMD_CTR_handler          post-call-sms-trigger (NEW, Module A)
(unchanged — lead subgroup advance)      - filters no-answer/voicemail shape above
                                          - own idempotency table (ContactId-keyed)
                                          - reads specialty+attempt from CP
                                          - selects message (Module B catalog)
                                          - sends via new single-recipient send path
                                            (NOT the segment-based bulk SMS sender)
                                          - writes to message-history table (Module E)
                                                    |
                                                    v
                                          [Patient may reply by SMS]
                                                    |
                                                    v
                                     (End User Messaging inbound, SNS)
                                                    |
                                                    v
                                     post-call-sms-reply-handler (NEW, Module C)
                                          - Lex V2 bot classifies intent
                                          - OPT_OUT -> log only (platform already handled it)
                                          - NOT_INTERESTED -> close out, log
                                          - WANTS_CONTACT / UNCLEAR -> Module D
                                          - writes to message-history table (Module E)
                                                    |
                                                    v
                                     Module D: native Connect CHAT queue
                                     (new contact flow + queue, agent picks up)
```

## Module A — No-answer/voicemail detection and SMS trigger

New Lambda, e.g. `post-call-sms-trigger`, in `Connect-batch-redis-refactor`, subscribed to the same CTR Kinesis stream as `connectcampaign_AMD_CTR_handler` via its own event source mapping.

**Shared classification constants**: the wrapped/standalone voicemail and no-answer value sets above are extracted into one shared module (e.g. `no_answer_voicemail_reasons.py`) imported by both this Lambda and, as a follow-up housekeeping change (not blocking this feature), `connectcampaign_AMD_CTR_handler`'s own `DISCONNECT_REASON_ROUTING` construction — so the two Lambdas' classification logic cannot drift apart silently the way this review's own P1 finding warned about.

**Trigger condition** (per CTR record):
```python
from no_answer_voicemail_reasons import (
    TRUE_NO_ANSWER_REASONS,       # {"RING_NO_ANSWER", "TELECOM_UNANSWERED"}
    VOICEMAIL_AMD_STATUSES,       # {"VOICEMAIL_BEEP", "VOICEMAIL_NO_BEEP", "AMD_UNRESOLVED_SILENCE"}
    STANDALONE_VOICEMAIL_REASONS, # {"VOICEMAIL_BEEP", "VOICEMAIL_NO_BEEP", "AMD_UNANSWERED",
                                   #  "AMD_UNRESOLVED", "AMD_UNRESOLVED_SILENCE"}
)

is_true_no_answer = disconnect_reason in TRUE_NO_ANSWER_REASONS
is_wrapped_voicemail = (
    disconnect_reason == "CONTACT_FLOW_DISCONNECT" and amd_status in VOICEMAIL_AMD_STATUSES
)
is_standalone_voicemail = disconnect_reason in STANDALONE_VOICEMAIL_REASONS
should_fire = is_true_no_answer or is_wrapped_voicemail or is_standalone_voicemail
```
Records without `Campaign.CampaignId`/`Attributes.brandedCampaignId` are skipped (not a campaign call — mirrors the existing handler's own guard).

**Idempotency**: own DynamoDB table (e.g. `PostCallSmsCtrIdempotency`), same claim/complete pattern as `ctr_idempotency.py`, but *not* that module or table — sharing would let this Lambda's claim collide with `connectcampaign_AMD_CTR_handler`'s unrelated claim on the same `ContactId`.

**Message selection**: read `Attributes.campaign` (specialty) and `Attributes.attempt` (attempt label) from the Customer Profile associated with the CTR's phone number. Normalize both before lookup — `.strip()` then case-fold (e.g. `"7th attempt".casefold()`) — because the CSV's own approved text already contains inconsistent casing across rows (`"1st Attempt"` vs `"7th attempt"`), so an exact-match lookup against raw Customer Profiles strings would silently skip real leads on a casing mismatch alone. The Module B catalog's keys are normalized the same way at load time. Look up the row by `(specialty.casefold(), attempt.casefold())`. If no row matches (e.g., an attempt number outside 1st/3rd/5th/7th, or a specialty outside Fibroid/Pain/Vein), skip and log — never guess or fall back to unapproved copy.

**Send — redesigned**: `sms_sender_handler.py` (Phase I's bulk sender) requires `campaignId`/`planId`/`runId`/`segmentArn`/`segmentName` and resolves a whole Customer Profiles segment; it has no single-recipient path and must not be reused here. Module A instead calls a new, minimal single-recipient send function — either a small new Lambda or a shared library function — that: validates the origination number the same way Phase I already does (`MessageType=TRANSACTIONAL`, `Status=ACTIVE`, `SMS` in `NumberCapabilities`) via the exact same check Phase I's frontend/backend already implements (`isPromotionalSmsNumber`'s transactional counterpart), then calls End User Messaging's `SendTextMessage` directly for the one resolved phone number and rendered body. No segment, no campaign run, no `VipSmsCampaignRuns` row — this is a single fire-and-record send, not a campaign.

**Failure handling**: matches Phase I's principle — a failure here degrades to "no follow-up text," never to a retried call or a blocked campaign. Distinguish a transient Lambda-invoke throttle (retry-safe, do not tombstone) from a real send failure (log and stop), the same distinction just fixed in Phase I's `_attempt_precall_sms_send`.

**Pre-launch verification gate (must close before this module ships, not optional/deferred):** confirm with whoever owns the external Lead API/CRM whether a lead entering a `"(send text)"`-named subgroup (attempts 2/4/6 in two of `connectcampaign_leadidupdate.py`'s three `SUBGROUP_FLOW` lineages) causes that external system to send its own SMS. If it does, Module A must exclude leads currently in one of those lineages/subgroups (to avoid a duplicate/conflicting message to the same patient) until the two systems are reconciled — note the attempt numbers do not even overlap (this design fires on 1st/3rd/5th/7th; the external-facing labels are on 2nd/4th/6th), which lowers but does not eliminate collision risk if the external system's actual trigger is coarser than "this exact attempt."

## Module B — Approved message catalog

A small, versioned catalog (same shape discipline as `precall_sms.py`'s `CATALOG` dict — no runtime string interpolation of unapproved text), seeded from the 12 `TRUE` rows:

| Specialty | Attempt | Text (verbatim from CSV) |
|---|---|---|
| Fibroid / Pain / Vein | 1st Attempt | *(shared text, only `[Clinic_Name]`/`[Staff_Name]` placeholders)* |
| Fibroid / Pain / Vein | 3rd Attempt | *(shared text)* |
| Fibroid / Pain / Vein | 5th Attempt | *(shared text)* |
| Fibroid / Pain / Vein | 7th Attempt | *(shared text — the CSV's own row for this attempt is inconsistently cased `"7th attempt"`; the catalog's stored key and the lookup are both normalized per Module A's casefold rule above, so this table always shows the human-readable canonical form regardless of source-CSV casing)* |

Only `[First_Name]` is a real per-lead placeholder in the approved text; `[Staff_Name]` and `[Clinic_Name]` are out of scope per the earlier decision (no auto-fill from `Attributes.location` yet) — Module A omits those placeholders' surrounding phrase rather than leaving literal bracket text in an SMS, mirroring Phase I's `CATALOG_WITHOUT_CLINIC` pattern.

## Module C — Reply intent classification

New Lex V2 bot, `en_US`, dedicated to this traffic (not `AppointmentScheduler` or either `AfterHours` bot — those are voice-oriented with different intents). Four intents:

| Intent | Trigger examples | Action |
|---|---|---|
| `WANTS_CONTACT` | "yes", "call me", "please call back" | Route to Module D |
| `NOT_INTERESTED` | "no thanks", "not interested", "no" | Log, close out — no human routing |
| `OPT_OUT` | "stop", "unsubscribe" (safety net; platform already intercepts most of these) | Log only |
| `UNCLEAR` (fallback) | anything not matching the above | Route to Module D — never guess |

New Lambda, e.g. `post-call-sms-reply-handler`, with its own SNS topic (`post-call-sms-inbound`) and its own dedicated origination number's `TwoWayChannelArn` set to that topic — **not** any of `rcm-sms-inbound`, `cloudhesive-integration-...`, or Connect-direct, all of which serve unrelated existing traffic. This Lambda calls Lex's `RecognizeText` API per inbound message, then dispatches per the table above.

**PHI/logging requirement**: the Lex V2 bot must be created with conversation logging (text and audio log destinations) **disabled**. This is a bot-level setting checked at creation time, not something this Lambda's code can compensate for after the fact — leaving it on would write the lead's raw SMS reply to a CloudWatch log group outside this design's own access controls and outside the BAA scope already established for the tables and Lambdas listed elsewhere in this doc. If a future need for Lex-side logging arises, it requires an explicit BAA-scope confirmation for that specific log group before being enabled, not a default-on setting.

**Idempotency**: SNS delivers at-least-once, and this Lambda's side effects (chat-contact creation, history append) are not naturally idempotent — a redelivered notification would create a duplicate contact and a duplicate history entry for the same reply. Own DynamoDB table (e.g. `PostCallSmsReplyIdempotency`), same claim/complete shape as Module A's table, keyed by the SNS message ID (`MessageId` on the SNS envelope) rather than `ContactId` (no CTR contact exists yet at this point in the flow — this is an inbound SMS, not a call).

## Module D — Human routing

A new Connect contact flow + queue purposed for this traffic (does not modify any existing flow). `WANTS_CONTACT` and `UNCLEAR` create a `CHAT`-channel contact into this new queue; an agent with `CHAT` concurrency on their routing profile picks it up. `NOT_INTERESTED` and `OPT_OUT` never reach a queue.

## Module E — Message history

New DynamoDB table (e.g. `PostCallSmsHistory`), partition key `customerPhone`, one item per phone with an appended list (direction, timestamp, body, intent if classified) — same shape as the orphaned `inbound-sms-handler`'s `sms-sessions` table's `lastMessages` pattern, but under this feature's own table so it has no dependency on decommissioned code. Written by both Module A (outbound) and Module C (inbound + classified intent). This is in addition to, not instead of, Connect's native chat transcript for the Module D contact itself.

## Testing strategy per module

Each module ships with its own unit test suite and is deployed+verified before the next module starts, per the requested build order:
1. **A**: unit tests for the shared classification module (every disconnect_reason/amd_status combination — wrapped, standalone, and true-no-answer shapes — plus every combination the existing handler already covers, to prove no overlap in either direction), catalog lookup with normalization (hit, specialty miss, attempt miss, casing-mismatch hit), idempotency claim/skip, the single-recipient send path's origination-number validation, and the transient-vs-permanent send-failure distinction. Verify via a real CTR-shaped test event before moving on. The external-CRM verification gate above must be explicitly answered (not just tested in code) before this module is considered done.
2. **B**: golden-file test asserting the catalog matches the CSV's 12 approved rows verbatim (dated, so a future CSV edit doesn't silently drift — same discipline as `precall_sms.py`'s `CATALOG` test), including a case specifically covering the CSV's own inconsistent casing on the 7th-attempt row to prove normalization handles it.
3. **C**: unit tests per intent with representative utterances against the actual deployed Lex bot (not a mock) before wiring it to real inbound traffic, plus the OPT_OUT/NOT_INTERESTED no-routing paths, plus a check confirming conversation logging is off for the deployed bot, plus SNS-redelivery idempotency (same `MessageId` processed twice produces one contact and one history entry).
4. **D**: manual verification that a `WANTS_CONTACT` classification actually surfaces as a claimable contact in an agent's CCP, on the new queue only (never on an existing queue).
5. **E**: unit tests for the DynamoDB append pattern (concurrent writes, item-not-exists case), then confirm both Module A and Module C writes land in the same item.

## Decisions (resolved)

- **Rate limiting**: fire on **every** qualifying attempt independently — a lead who goes unanswered on the 1st attempt gets that follow-up text, and if later unanswered again on the 3rd/5th/7th attempt gets that attempt's follow-up text too. Not a once-per-lead cap. The CTR-level idempotency table (Module A) still guards against firing twice for the *same* `ContactId`/attempt, not across different attempts.
- **Origination number**: a **new number, requested specifically for this feature**, used for nothing else — not shared with any existing TRANSACTIONAL or PROMOTIONAL traffic. Module A's outbound send and Module C's `TwoWayChannelArn` both point at this one dedicated number.

## Open items for spec review

- Exact new Connect queue/flow names and routing profile assignment (needs an operator decision, not a technical one).
