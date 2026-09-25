# Phase I: pre-call SMS from recipient profiles

Status: the optional-clinic Phase I backend was deployed on 2026-09-14; see
the [rollout record](/home/devaju/projects/_audit-reports/precall-sms-rollout-2026-09-14/ESTADO.md).
The subsequent [Plans scheduling change](sms-plan-scheduling.md) is local and
pending deployment. Frontend publication and the integrated send remain pending.

Baseline: `1317f728ab7f4db3589c033935f8690d03bcc8b1`, including the deployed
timezone hotfix. Work branch: `feature/precall-sms-auto-phase1`.

## Scope and compatibility

This change personalizes the pre-call SMS for recipients of an existing plan
segment. It does not add a Medwork NPL-entry event integration, post-call SMS,
Lex/Luma routing or new contact flows. Plan schedules and working-hour rules are
not changed. SMS initiated by Plans delegates scheduling to Plans instead of
applying a second per-phone time-window check; see the scheduling change above.

This patch also does not add a global two-SMS-per-lead/24-hour cap, Medwork
cross-channel consent synchronization or manual-dial/bot collision arbitration.
The existing per-SMS-run phone deduplication is not a global frequency cap.
These requirements must not be marked complete based on this personalization
change.

An absent `precallSms.mode` retains the existing manual behavior. Existing plans
and running snapshots are not migrated automatically. The automatic mode is an
explicit opt-in:

```json
{
  "precallSms": {
    "enabled": true,
    "mode": "profile",
    "catalogVersion": "phase1-v1",
    "clinicName": "Example Clinic",
    "originationNumberArn": "<configured SMS origination ARN>"
  }
}
```

Profile mode rejects nonempty `messageTemplate` overrides. The application uses
the incorporated Vein/Pain copies and resolves the name and specialty for each
recipient. `clinicName` is an optional campaign setting: enter the clinic to
mention, or leave it blank to omit the complete clinic clause. The frontend
previews the selected value for both variants. Manual pre-call and bulk SMS retain their existing validation,
including the 160-character ceiling. Profile mode permits multipart SMS and
preserves the approved copy and punctuation. On 2026-09-14 the user removed
the Vein copy's same-number promise; its final sentence is "Look out for a call!".
The user also approved optional campaign clinic names on that date. These edits
update the unpublished v1 candidate: the production inventory contained no
profile-mode plan or run at the time of the change. Once published, this catalog
and its behavior must remain stable for retained runs.

Dependencies remain part of the plan. Profile mode supports dependent voice
campaigns through their own preparation lifecycle; adding this mode is not a
reason to remove a dependency. The manual mode retains its existing dependency
restriction.

## Data contract

- Name: Customer Profiles `FirstName`, normalized as Unicode and limited to
  20 characters; invalid or missing names use the neutral `there` fallback.
- Specialty: explicit `Attributes.specialty` or an unambiguous Vein/Pain value
  in `Attributes.campaign`. Conflicting specialties are rejected for that
  recipient.
- Clinic: optional `precallSms.clinicName`, normalized to NFC, trimmed and
  validated with an 80-character limit. Missing, empty or whitespace-only
  strings omit the complete clinic clause. A non-string or invalid nonempty
  name is rejected before sending. Profile clinic attributes do not fill in a
  name when the operator leaves this setting blank.
- Invalid or conflicting recipient data suppresses that recipient's SMS with a
  technical reason. Names, phone values and rendered messages must not appear
  in diagnostic logs.

The catalog version covers copy and optional clinic behavior. Do not change a
published catalog's meaning in place: retained runs must continue using their
original version. The normalized `precallPolicy` is persisted with the SMS run
and reused on recovery; a replay cannot replace the original clinic choice.
No clinic brand is invented as a fallback.

When the field is blank, the incorporated copies are:

- Vein: "Hi {{FirstName}}! We’re about to give you a quick call regarding your vein consultation request. Look out for a call!"
- Pain: "Hi {{FirstName}}! We’re calling you in just a moment to discuss your pain management request. Talk soon!"

The clause is selected structurally from the catalog, not by substituting an
empty string into `This is {{ClinicName}}.` or `{{ClinicName}} here.`.

## Sending and voice continuity

Profile mode distinguishes audience preparation, queueing, API acceptance and
carrier delivery. The new Connect path prepares the campaign without starting
it, then starts voice after the SMS work has resolved; it does not rely on a
StartCampaign/PauseCampaign race. Branded voice similarly waits before seeding.

The wait is bounded by the existing initialization deadline. Failure or timeout
records the SMS outcome and lets voice proceed. Cancellation observed before
Start prevents it, and the new post-Start check compensates a concurrent stop
when the worker and its dependencies remain available. Consequently, this is a best-effort pre-call notification
with an acceptance gate, **not a guarantee that every patient receives a text
before a call**. The provider's `MessageId` confirms API acceptance, not delivery.

The queue processor must not send an automatic message for an aborted SMS run.
The new mode bounds provider TTL and preserves the existing `messageTemplate`
SQS key, which carries the already rendered body.

The adversarial fleet reproduced a cancellation case: Connect can
accept Start while an abort wins in DynamoDB, followed by a worker termination
or a transient failure of the compensating read/Stop. Terminal runs no longer
poll. The new `profile_voice_cleanup` module records each admitted Start's
Connect ID and immutable run/generation owner before the external call. A fixed
EventBridge rule processes these records independently of run pollers, so abort,
restart, deleted runs and the janitor do not remove the recovery obligation.
It retains unresolved records after service errors, verifies ownership before
Stop/Delete, and never reuses retired Connect IDs for another generation.

New profile starts require a recent recovery heartbeat. The worker lease lasts
360 seconds (the Lambda's five-minute hard timeout plus a margin); an obsolete
record cannot retire while its Start worker could still be alive. A pass that
cannot process even one pending item does not refresh readiness. These are
recovery execution budgets, not changes to patient contact hours. Recovery is
eventual while its scheduler and dependencies are available; it does not
promise an instantaneous stop or delivery-before-call guarantee. See
[publication and rollback prerequisites](profile-voice-cleanup-release.md).

Profile SQS initialization now tracks active batches and an enqueue revision
so a concurrent rejection cannot masquerade as an empty completed audience.
When a profile processor reclaims a stale SENDING item, its previous provider
outcome is uncertain: it records PROVIDER_OUTCOME_UNKNOWN without resending.
Manual/bulk recovery remains unchanged.

## Required local validation

1. Mixed Vein/Pain audience, optional campaign clinic selection, Unicode,
   missing/blank clinic settings, missing names, ambiguous specialties and
   shared phones. Profile clinic attributes must not override the selection.
2. Exact template text and multipart accounting; old manual and bulk rules.
3. Disabled/absent/manual mode does not enter the new voice lifecycle.
4. Pending SMS, completion, partial failures, timeout, cancellation, retry and
   repeated/concurrent lifecycle observations.
5. Warm and cold Connect campaign paths, dependent campaigns and branded voice.
6. A fast SQS consumer cannot miss a queue row or have its terminal state
   overwritten by sender bookkeeping.
7. Frontend editing/serialization and API contract agreement; typecheck/build.
8. Existing Plans, Campaigns, Progressive Dialer, SMS and shared-module suites;
   infrastructure synthesis verifies package contents, the new fixed cleanup
   rule/permission, and unchanged existing IAM resources without deploying.
9. A scheduled recovery pass resolves hard crashes and transient read/Stop
   failures after abort, without the old run poller or a second user action;
   retries retain ownership evidence and preserve a newer active generation.

Results and limitations belong in the accompanying validation report. Passing
mocked/unit tests does not establish production delivery behavior.

## Before any deployment

The user requires evidence that current production behavior is preserved. There
is no deployment as part of this implementation turn. In particular:

- Verify the operator's clinic setting in the preview, including leaving it
  blank. A profile clinic mapping is no longer an activation prerequisite;
  omission is an explicit behavior approved by the user.
- The adversarial review found that the live Plans execution role lacks
  `connect-campaigns:UpdateCampaignSchedule`, which profile mode calls before
  StartCampaign. The scoped addition is prepared in
  `infra/config/precall-sms-plans-policy.json`; it has **not been applied**.
  Keep this in the existing operator-managed policy, because altering the
  CloudFormation-managed role policy previously blocked update and rollback.
  Reading all inline/attached policies confirmed the missing Allow. The current
  operator cannot run `iam:SimulatePrincipalPolicy`, so no effective-policy
  simulation result is claimed.
- Verify the separate, unapplied CloudFormation deployment-policy delta in
  `infra/config/profile-voice-cleanup-cfn-policy.json`, then the fixed recovery
  rule, its Lambda permission and a successful persisted heartbeat. Follow
  [the release order](profile-voice-cleanup-release.md); keep recovery deployed
  while any owned intents remain.
- The same-number wording was resolved on 2026-09-14: the user approved removing
  "from this number". The candidate makes no such promise and retains the
  existing caller identities.
- Verify the accepted build in an isolated environment using explicitly
  designated test recipients. Record SMS acceptance and the subsequent call
  event; no patient outreach is needed for local regression tests.
- Review the exact changed assets for the SMS and Plans stacks. Shared layers
  are per-stack, so unrelated stacks do not need deployment merely because a
  shared source module was added. This is not a command to deploy all stacks.
- Rebuild publication artifacts with the intended environment configuration.
  The local frontend build checks compilation and has no production `.env`.
  The synthesis comparison is against the source baseline, not a live
  CloudFormation drift check. Preexisting bytecode in local build directories
  can change asset hashes even when source is unchanged; review a clean release
  build before publishing.

Keep profile activation disabled while backend versions are mixed. Update and
verify all SMS consumers/senders before Plans, and publish the frontend last.
An old SMS processor ignores the new policy and cancellation checks. Old
in-flight invocations must finish before enabling profile runs; stack completion
alone is not evidence that an invocation already running has changed code.

Local evidence, package comparison and the existing processor IAM permission
check are recorded in the workspace audit folder
`_audit-reports/precall-sms-auto-phase1-2026-09-11/VALIDACION.md`.
The original adversarial findings are preserved in
`_audit-reports/precall-sms-adversarial-fleet-2026-09-11/VALIDACION.md`.
The durable correction and its current verification are recorded separately in
`_audit-reports/precall-sms-durable-fix-2026-09-11/VALIDACION.md`; historical
passing suites do not substitute for the new recovery evidence.

## Reversal preparation

Keep the baseline source revision and deployed Lambda/layer versions and
frontend asset manifest. The old manual contract remains supported by the new
code, providing a way to disable future automatic-mode use without reverting
the entire application.

Do not roll the SMS consumer back to a version that ignores `precallPolicy`
while profile messages remain in the queue, DLQ or in-flight invocations.
Disabling new profile configuration does not erase existing queue messages.

A rollback must account for profile-mode runs and messages already in flight.
Do not replace the executor with an older version while profile-mode snapshots
still require its start gate: the old executor does not understand that state.
First establish that those runs/queues have settled, or retain the compatible
executor until they do. Similarly, disabling a plan does not retract an SMS
already accepted by the provider. No reversal or production stop was executed
for this work.

Keep the fixed cleanup rule, handler and ownership records until all pending
cleanup intents are resolved. The writer flag can disable new registration
without disabling recovery. Reverting or removing the recovery worker early
would reopen the cancellation failure even though its records remain durable.
