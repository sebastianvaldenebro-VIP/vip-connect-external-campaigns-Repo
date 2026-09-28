# SMS scheduling owned by Plans

User requirement, 2026-09-14: when SMS belongs to a plan, the plan determines
when it runs. The sender must not impose an additional per-phone time window.
Status: implemented locally, pending deployment.

Both internal Plans invocation wrappers stamp `scheduleSource: "plans"`,
overriding any supplied value. This covers manual pre-call, profile pre-call,
SMS-only campaigns, snapshot resumption and active-campaign retries. No UI
option or additional schedule configuration is required.

The sender persists that source on the SMS run before reading the audience.
Every enqueue path uses the persisted source to omit the recipient-hours
check. A replay cannot change the saved source, audience or message policy.
Standalone calls without the marker retain their existing hours behavior;
having `planId` and `runId` alone does not establish the Plans contract.

An existing RUNNING SMS record without a source can adopt Plans scheduling
once, when invoked by the updated Plans wrapper. A conditional write requires
an absent source and unfinished initialization; a consistent read uses the
winner after either success or a conflict. Terminal runs and profile runs whose
initialization is already complete are not reopened to send late messages.
There is no bulk migration or automatic replay of completed work.

Profile statistics and the initialization seal are conditional on the source
used for that pass. If a concurrent Plans invocation adopts the legacy record,
an older pass that evaluated phone hours cannot seal its stale suppression;
initialization remains pending for a pass using the saved Plans source.

This field is an internal caller contract, not authentication of a Lambda
principal. It relies on the existing IAM restriction on invoking the sender
and retry functions. It is not exposed as a plan setting or a public send API.
Unknown values do not disable the phone-hours gate.

Opt-out checks, per-run claims and deduplication, profile rendering, cancellation
checks and the provider-acceptance gate remain in their existing paths. The
processor, shared scheduling code, campaign builders, plan schedules, IAM and
infrastructure configuration are unchanged.

Deployment order remains SMS sender/retry first, then Plans. During that
transition an old Plans invocation without the marker retains the old behavior;
do not claim the change active before both backends are updated. Existing
in-flight invocations may finish with their original code, which lacks the new
conditional seal. Before updating Plans, allow invocations of the previous
sender/retry version to finish within their configured execution timeout while
production continues running. Rollback to the
previous sender restores its hours gate, even on records carrying the new field.
Retain the compatible Phase I backend and cancellation recovery in a rollback.
