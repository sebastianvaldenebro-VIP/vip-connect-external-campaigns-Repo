# SMS campaigns

Status, 2026-09-14: implemented and tested locally. This feature has not been
validated through an AWS campaign send. Local tests and browser checks do not
establish provider delivery or production readiness.

The admin navigation entry **SMS campaigns** opens `/sms`. It lists saved,
non-template plans whose campaigns are all SMS. Voice and mixed-delivery plans
remain in Plans. This UI reuses the existing Plans lifecycle and SMS pipeline;
it does not create a separate campaign service.

## Create, save, and start

1. Select **New SMS campaign** and enter a campaign name.
2. Choose an **Audience segment**. The form loads every page of the segment
   list. A loading error must be resolved before saving. To create an audience,
   select **Create a new segment**, complete the existing segment editor, and
   save it. The campaign draft is retained in the open screen, and the new
   segment is selected. Cancelling the segment editor returns to the draft.
   This inline flow does not enable a voice campaign.
3. Select an active **Origination number**.
4. Edit **Message** and inspect **Message preview**. The default text is:

   ```text
   Hey {{FirstName}}! It's VIP Medical Group and we noticed that booking your consult might still be on your to-do list. Let's cross it off! Book your own appointment in 30 seconds - no phone calls needed: https://luma-link.com/ly8aKJQzsjm
   ```

5. Confirm the existing no-PHI acknowledgment. Editing the message clears that
   acknowledgment, so the revised text must be confirmed again.
6. Select **Save campaign**. This saves a manual plan and returns to the list;
   it does not start a run or send a message.
7. When ready to send to the selected audience, explicitly select **Start** in
   the list. Start requests the existing Plans run. It is disabled while a
   request is pending or a run is active. The list refreshes every 15 seconds
   and keeps an accepted start blocked through a stale list response until the
   same run is observed as terminal. **View activity** opens the existing plan
   detail page at `/plans/:id`.

The editor retains a draft only while that screen remains mounted. Saving is
required before navigating away or reloading. A pinned segment selects the
audience definition; it is not a preview or a frozen list of recipients at save
time. Audience resolution occurs through the existing SMS pipeline at run time.

## Personalization and message limits

`{{FirstName}}` is the only supported recipient placeholder for this contract.
The saved template retains that marker. The sender obtains `FirstName` from
Customer Profiles for each recipient and renders the outbound body. It
normalizes and trims the name, applies the shared name cleaner, and limits it
to 20 characters. Missing, blank, or invalid names use `there`. The preview uses
the fictional name Alex; Alex is never substituted into the saved template.

The editable copy is preserved without truncation. The only permitted link is
the exact supplied Luma URL, with no additional parameters. Removing the link
is allowed; replacing it with another destination is not. The server rejects
unsupported or malformed placeholders, disallowed content, and messages that
exceed the contract's limits. The UI acknowledgment does not replace those
server checks.

The character and SMS-part display is an estimate using a 20-character name.
GSM-7 extension characters count as two units; Unicode uses UTF-16 units and
different multipart limits. Actual encoding and part count depend on the
recipient's name. Server validation checks both GSM and Unicode name cases,
with a 1,600-character API ceiling and limits of 1,530 GSM units or 630 Unicode
units after personalization.

## Saved contract and compatibility

The simple editor creates one bucket and one campaign:

- Plan: `trigger.type: "manual"`, `isTemplate: false`.
- Bucket: `run_mode: "status_based"`, `cleanup: false`,
  `prestart_next: false`, and an empty bucket-level `campaignConfig`.
- Campaign: `deliveryType: "sms"`, `run_type: "full"`, the selected
  `pinnedSegmentArn`, and empty `states`, `groups`, and `dependsOn` arrays.
- Campaign configuration: `smsTemplateVersion: "campaign-v1"`,
  `smsMessageTemplate`, `smsOriginationNumberArn`, and
  `phiAcknowledged: true`.

No voice connection configuration or new schedule is required. The campaign
reuses Plans create/update/start endpoints, SMS run records, the existing queue,
sender/retry functions, and processor. The version is carried through the
durable run, queue row, and message payload. The sender renders per recipient;
the processor validates the version, rendered body, and current run before
calling the provider with `MessageType: "PROMOTIONAL"`. A provider acceptance
counter is not proof of handset delivery.

`campaign-v1` is an explicit opt-in. SMS configurations without that version
retain their legacy validation and send path. Manual and profile pre-call SMS
retain their existing contracts and copy. The new campaign contract cannot be
combined with the profile pre-call policy. Opt-out, cancellation, per-run
claims, and deduplication remain part of the pipeline. An uncertain provider
outcome is not automatically resent to repair its counters.

**Edit** is offered only for an idle, single-bucket, single-campaign plan in the
simple `campaign-v1` shape described above. Default plans, automatic schedules,
recurrences, dependencies, custom durations, legacy templates, and other
advanced configuration are excluded from this editor. They remain accessible
through **View activity** and the existing Plans interface; opening them here
does not silently convert their settings.

Plans owns the schedule for these sends. Its internal invocation wrappers stamp
`scheduleSource: "plans"`, and retries use the policy saved on the SMS run.
The sender does not add a per-recipient phone-hours gate to Plans-origin SMS.
Standalone invocations keep their prior scheduling behavior. This is an
internal caller contract, not authentication inferred from a plan ID. See
[SMS scheduling owned by Plans](sms-plan-scheduling.md) for compatibility and
in-flight invocation limits.

## Validation and rollout requirements

Local coverage includes template/renderer checks, backend lifecycle and
processor tests, and frontend interaction tests for pagination, inline audience
creation, saving without starting, errors, pending-request guards, and edit
restrictions. Desktop and mobile browser checks passed with local fixtures;
the composition field and preview grow to expose the full default text and
link on mobile. These checks used no production campaign send.

Before publication:

1. Build and verify fresh artifacts from the reviewed source. The new shared
   module is
   `services/shared/python/vip_shared/domain/services/sms_campaign.py`.
   Package it at `python/vip_shared/domain/services/sms_campaign.py` in every
   shared layer used by the updated SMS sender/retry, SMS processor, and Plans
   validator. Matching handler changes are required as well. Earlier optional
   clinic assemblies and the two code-only scheduling ZIPs do not contain this
   new module or the complete `campaign-v1` feature and are not candidates for
   this publication.
2. Verify archive file manifests and hashes, module importability, handler/layer
   pairing, and the intended deployment diff. A frontend build alone cannot
   establish backend compatibility. Preserve a reviewed compatible rollback
   baseline, including existing profile pre-call cancellation recovery.
3. Publish the compatible SMS backend and shared layer first, including the
   sender/retry and processor, then Plans with its matching shared layer, then
   the frontend. Allow earlier in-flight backend invocations to finish before
   enabling the new contract. Keep the new UI unpublished until the backend
   version pairing and required integration checks are complete. Publishing
   Plans or the frontend first can expose `campaign-v1` to a handler that does
   not understand it.
4. Complete an explicitly authorized AWS integration test with synthetic
   Customer Profiles and a controlled recipient before enabling operator use.
   Verify audience resolution, saved placeholder versus rendered body, fallback
   name behavior, queue/version propagation, promotional provider submission,
   run activity, and the applicable cancellation/opt-out behavior. Track
   provider acceptance separately from delivery evidence. This integration and
   application rollback validation remain pending; they are not implied by the
   local test results.

Once `campaign-v1` work exists in runs or queued messages, do not roll back its
processor or shared layer blindly to a version that lacks the contract.
Reconcile that work and retain compatible consumers as part of the reviewed
rollback plan.
