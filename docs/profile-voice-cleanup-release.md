# Profile voice cleanup: publication prerequisites

This change prepares recovery for durable profile voice start intents. It has
not been deployed, and no IAM changes have been applied.

`VipAdminApiPlansStack` adds one enabled EventBridge rule named
`vip-profile-voice-cleanup`, running every minute against the existing Plans
Lambda with `{"action":"profile_voice_cleanup"}`. Its explicit Lambda permission
allows only `events.amazonaws.com` from that rule ARN and this account. The
fixed name is outside `vip-plan-*` and `vip-sched-*`, so the existing orphan-rule
janitor does not delete it when a run becomes terminal or is no longer latest.

The Lambda environment sets `PROFILE_VOICE_CLEANUP_ENABLED=true` to permit new
intent registration. Registration additionally requires a recent successful
reaper heartbeat in DynamoDB. The environment flag alone cannot permit a Start
while the rule, target, permission or cleanup handler is still unavailable.
The cleanup action remains callable when the flag is false so existing intents
can drain. Action failures propagate with a sanitized error, producing Lambda
Errors and allowing the existing asynchronous retry path to run; they are not
returned as a successful invocation containing `ok:false`.

Failures for individual intents preserve the cursor and pending work while
the handler reports failure. A persisted failure marks recovery degraded and
immediately rejects subsequent admission checks. When scheduled invocations
are absent, admission closes once its last healthy heartbeat is more than
180 seconds old. A failed pass does not refresh that heartbeat. Health remains degraded across
subsequent pages until a complete cursor cycle finishes without another failure.
At most 25 intents are considered per invocation, so recovery and restoration
of readiness can take several scheduled invocations for a larger backlog.

## Infrastructure and permission boundary

The existing execution role already declares Query/GetItem/PutItem/UpdateItem/
DeleteItem for `VipAdminPlans`, and GetCampaignState/StopCampaign/DeleteCampaign
for Connect campaigns in this account. DeleteCampaign is needed to retire an
obsolete campaign that remained Initialized after its worker expired. The
dedicated intent partition uses that table without a new table or index.
No execution-role IAM resources are changed by
the rule or permission. This statement is supported by source and the cached
Plans-role inventory from 2026-09-11; it is not a new effective-permissions
simulation or a live recovery test.

Creating the new CloudFormation rule requires additional deployment capability.
The repository's `infra/iam/cfn-exec-scoped-policy.json` does not declare
EventBridge actions, and `api-metrics-stack.ts` documents an earlier failure
caused by missing `events:DescribeRule`. Review
`infra/config/profile-voice-cleanup-cfn-policy.json` as an **unapplied deployment
policy delta** for the CloudFormation execution principal. It scopes rule
lifecycle/target/tag operations to the fixed rule, and resource-policy
operations to the existing Plans Lambda. It does not belong on the runtime role.

The cached EngineeringPermissionBoundary v4 contains an explicit denial of
`iam:PutRolePolicy` and related role-management actions. An authorized principal
must establish deployment permissions through the approved IAM process before
publication; CloudFormation must not attempt to expand its own execution role.
The boundary's non-IAM allow does not independently grant EventBridge access.
A successful local synth proves template construction, not deployment rights.

The separate existing `connect-campaigns:UpdateCampaignSchedule` prerequisite
remains in `infra/config/precall-sms-plans-policy.json`; preparing the reaper
does not apply that permission.

## Release and rollback order

1. Review the exact Rule, Lambda Permission, handler/module package and
   environment diff. Confirm existing IAM resources remain unchanged. Verify
   the deployment principal can create, read, update and remove both new
   resources before attempting publication.
2. Publish the compatible cleanup handler/module and the static rule/permission.
   Wait for the stack update to complete and for the rule to invoke the handler
   successfully. Registration must reject a missing or stale heartbeat; do not
   replace that requirement with the environment flag.
3. Enable profile-mode campaigns only after the infrastructure update completes
   and the independent recovery tests pass. Verify the optional clinic setting
   and complete the designated-recipient test. The user approved omitting the
   clinic clause when that field is blank and resolved the same-number copy
   issue on 2026-09-14 by removing that phrase.
4. To stop new profile starts, disable their registration/configuration while
   retaining the compatible cleanup handler and enabled rule. Drain and verify
   pending intents before deleting the rule/permission or reverting to code
   that cannot process `profile_voice_cleanup`. A rollback of the Lambda and
   rule during pending work would remove recovery even if the intents survived.

The rule shares the Plans Lambda's five-minute timeout and reserved concurrency
of five. Its one-minute interval is a dispatch cadence, not a one-minute
recovery guarantee: service outages, throttling, worker expiry and backlog can
delay retirement. Recovery must keep its bounded-work and pagination behavior; an
execution budget does not authorize dropping an unresolved intent. Monitoring
and release evidence must distinguish successful recovery from merely queued
or accepted cleanup invocations.
