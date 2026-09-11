# Pre-call SMS: release and recovery

This guide describes the 2026-09-11 continuation of the `feature/precall-sms-phase1` worktree. The user explicitly approved the reviewed IAM permissions, retry log group, five changed backend stacks and frontend publication. **Deployment completed:** all participating stacks are `UPDATE_COMPLETE`, the ten Lambdas passed configuration checks, the harmless runtime probes passed, and the frontend's public hashes match the production build after CloudFront invalidation. See the [deployment record](/home/devaju/projects/_audit-reports/connect-precall-sms-review-2026-09-11/deployment/DEPLOYMENT.md). Application commit: `a9f8f9c`; IAM deployment correction: `08eef0e`. [Task 7](superpowers/plans/2026-09-09-precall-sms-phase1.md#task-7-end-to-end-verification--the-gate-for-all-phase-iiiii-work), including approved live test recipients and the real Sunday check, remains pending as the E2E gate for Phase II/III.

## What changed

- The postdeployment audit found `CreateCampaign` rejected the combination of `defaultTimeZone` and `localTimeZoneDetection`. Both builders now select only `AREA_CODE` detection, preserving the configured Monday–Saturday local contact hours. Legacy `communicationTime.timezone` does not override that policy. AWS can exclude recipients whose timezone cannot be determined; the fixed timezone field is not a fallback. The [audit and affected runs](/home/devaju/projects/_audit-reports/connect-precall-sms-review-2026-09-11/deployment/postdeploy-logs-2026-09-11/REVISION-LOGS.md) record the incident; the hotfix deployment and service validation are recorded separately.

- Force Start persists a new `precallSmsGeneration` before external work. Retries within that generation reuse the same SMS identity; optimistic-concurrency recovery retains the completed/pending initialization evidence and does not overwrite a newer restart.
- SMS retries remain active even after the current quiet-hours count reaches zero. Fresh claims, SQS partial failures and incomplete profile reads remain recoverable; initial sender invocations reuse their persisted configuration and do not recreate stopped runs.
- Segment enumeration uses supported Customer Profiles operations. Exact small canonical phone/customer-ID lists use `SearchProfiles` and authoritative `GetSegmentMembership(ProfileIds=...)`; all other definitions, including larger lists, use an asynchronous encrypted snapshot. The previous membership API call could not enumerate a segment.
- Both bulk and pre-call validation screen the rendered clinic boundary for PHI, reject unresolved double-brace expressions, and require a usable clinic value when its token is referenced. Approved Vein/Pain copy, the two-field allowlist, opt-out handling and configured contact windows stay as recorded in the plan.
- The main branch merge retains Cognito-group authorization, Blocked Numbers, DLQ fixes and shared bundling. Only the SMS layer includes `phonenumbers`; CI installs that dependency for SMS and shared-module tests.

## Pending initialization and timeout

| Situation | Pre-call voice / branded | Bulk SMS |
| --- | --- | --- |
| Sender returns `pending: true` | `precallSmsState=pending`; Connect retains its pause; branded keeps its segment and has not registered/seeded | `smsInitializationState=pending`; campaign remains running and is not classified as complete from an empty queue |
| Subsequent tick | Reuses generation, SMS ID, segment and published snapshot metadata | Reuses SMS ID, segment and published snapshot metadata |
| Initialization completes | Records `precallSmsSentAt`; resumes Connect or seeds branded once | Initialization becomes complete; normal queue processing/polling proceeds |
| Pending age reaches 300 seconds | Records `precallSmsState=failed`; aborts the SMS row with `sms_initialization_timeout`; voice is allowed to proceed | Records `smsInitializationState=failed`, campaign `status=error`, `exitReason=sms_initialization_timeout`; aborts its SMS row |
| Definitive initialization failure | Records failure and permits voice; no subsequent SMS retry for that failed initialization | Records error and stops the SMS attempt |
| Explicit stop/cancel, terminal cancellation response, or campaign/bucket/run cutoff | Cancels preparation; does not resume or seed a cancelled campaign | Cancels preparation; does not recreate the run |

The five-minute deadline is evaluated on ticks from `precallSmsPendingAt` or `smsInitializationPendingAt`; it is not a promise of an action at exactly 300 wall-clock seconds. Logs identify timeouts with `precall_sms_initialization_timeout` or `sms_initialization_timeout`. A failed pre-call initialization does not enter the later quiet-hours retry path, which requires `precallSmsSentAt`.

`VipSmsCampaignRuns.recipientSnapshot` stores only `snapshotId`, `segmentName`, `destinationUri` and `requestedAtEpoch`. Conditional publication selects one snapshot for the lifecycle. Concurrent losing exports have distinct prefixes and are never read by the winner. The requested prefix is `precall-sms/<uuid>/` in the existing encrypted bucket. AWS's observed trailing-slash normalization is accepted when checking the returned destination; reads use the persisted requested prefix. An incomplete export, malformed CSV or partial CP result never becomes a successful partial recipient list.

The reader accepts case-insensitive `ProfileId` CSV headers, then hydrates IDs with `BatchGetProfile` batches of 20. Membership batches contain at most 100 IDs and only `PRESENT` members are eligible. A complete profile without a phone cannot receive SMS; this differs from a missing/failed profile response, which fails the scan.

## Limits to include in release acceptance

There is no end-to-end guarantee that every SMS is delivered before the first call. SMS initialization/enqueue, provider delivery and voice initiation are different events. Connect can start before the pause takes effect, the voice fail-open paths allow calls after SMS failure, and the minute-based SMS retry is not synchronized with Connect's per-recipient quiet-hours reopening. The earlier 1–6 minute estimate must not be treated as a verified lead-time guarantee.

There is also no exactly-once SQS/DynamoDB transaction in the inherited bulk pipeline. Publishing to SQS and persisting its queue ledger occur separately; a fast processor or a subsequent ledger write failure can lose a message. Per-phone claims and generation IDs reduce duplicate work but do not prove exactly-once delivery. Record provider and call evidence in the canary instead of interpreting counters or `precallSmsSentAt` as delivery receipts.

## Production prerequisites and dependency review

Verified account/region: `165505826690` / `us-east-1`, AWS profile `production`.

| Resource | Value |
| --- | --- |
| Sender and retry execution role | `vip-sms-sender-role` |
| Snapshot bucket | `vip-admin-segment-snapshots-165505826690` |
| Snapshot export role | `arn:aws:iam::165505826690:role/VipAdminSnapshotRole-us-east-1` |
| Snapshot CMK | `arn:aws:kms:us-east-1:165505826690:key/df585888-2f49-4de0-9cba-14803fda63f0` |
| Retry function/log group | `vip-admin-sms-retry-quiet-hours` / `/aws/lambda/vip-admin-sms-retry-quiet-hours` |

The SMS sender and retry functions share an execution role imported with `mutable:false`. The additive [sender policy](../infra/config/precall-sms-sender-policy.json) must be applied separately: retry-log writes; queue `Query`/`DeleteItem`; domain/segment CP reads and snapshot operations; exact snapshot-role `PassRole` limited to `profile.amazonaws.com`; and S3 read/list limited to `precall-sms/*`. Existing role policies already grant the recorded snapshot CMK access, runs/queue writes and opt-out reads. Preserve those existing policies.

The static permission review compared saved role policies and actual snapshot resources. An IAM simulation attempt was denied by AWS for `iam:SimulatePrincipalPolicy`; it did not establish effective permissions. This was a service authorization denial, not an automatic approval-review rejection.

Plans also requires an operator-managed addition: [precall-sms-plans-policy.json](../infra/config/precall-sms-plans-policy.json), inline policy `PrecallSmsPlansAdditionalPerms` on `VipAdminApiPlansStack-FunctionRole111A5701-mSfFlCntjbO0`. It contains only campaign Pause/Resume and invocation of the retry Lambda. `EngineeringPermissionBoundary` prohibits the CloudFormation execution role from modifying IAM policies; the first deployment failed on this pre-existing restriction. Commit `08eef0e` keeps all CloudFormation IAM resources identical to their deployed baseline and provisions the already-approved delta through the authorized operator session. Existing inline policies, the boundary and the CloudFormation service role are preserved.

The failed rollback was recovered by skipping only `FunctionRoleDefaultPolicy41A10F9C`, after comparing its actual IAM document with the fully resolved original template and proving them equal. The dependent onboarding guard was allowed to roll back normally. Before repeating that operation in another incident, establish the actual resource state and consistency; the recorded proof is specific to this release. Evidence: `deployment/rollback-policy-consistency.json`, `rollback-complete.json`, `plans-additive-policy-applied.json` and `recovery-template-check.json`. This follows the existing `events-list-rules-cli` procedure documented in `api-plans-stack.ts` and AWS's [rollback recovery procedure](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/using-cfn-updating-stacks-continueupdaterollback.html).

The read-only production check in `live-prerequisites.json` confirmed that `vip-sms-campaign-queue` uses the exact CMK above and has a 180-second visibility timeout. `SqsManagedSseEnabled=false` accompanies `KmsMasterKeyId`: the queue uses KMS encryption. The retry log group was absent and must be created with encryption and retention before the new retry function runs.

`VipAdminApiSmsStack` now references snapshot resources from `VipAdminApiSegmentsStack` and `VipAdminDataStack`. `VipAdminApiPlansStack` references SMS and progressive-dialer resources. A targeted deploy can therefore include Data, Segments, SMS and Progressive Dialer dependency stacks. Review every included dependency diff. Shared-layer changes can also change Lambda bundle hashes outside the primary code target. Authorizer rollout retains the separate user-group backfill procedure in the [runbook](runbook.md); this feature does not authorize an unrelated auth rollout.

The live comparison of the synthesized assembly reported five stacks with functional changes: `VipAdminApiSegmentsStack`, `VipAdminApiCampaignsStack`, `ApiProgressiveDialerStack`, `VipAdminApiSmsStack` and `VipAdminApiPlansStack`. `VipAdminDataStack` had only CDK analytics metadata differences, with no functional changes. Reviewed changes include shared-layer replacement, the new retry Lambda and exports, snapshot configuration, and Pause/Resume plus retry-invocation permissions; no storage deletion was found. A separate deployed-template comparison confirmed that CDK's omitted differences concern descriptions/non-ASCII Checkov rationales and metadata; the skipped Checkov IDs are unchanged. `--strict` aborts on inherited subnet `routeTableId` warnings, so it is not used in the command below.

## Approved release commands and execution record

Run CDK from the repository/worktree root containing `cdk.json`, using Node 20 and Python 3.12. First capture the revision and review source/tests/synth evidence for that exact revision:

```bash
cd /home/devaju/projects/vip-connect-external-campaigns/.claude/worktrees/precall-sms-phase1
git status --short --branch
git rev-parse HEAD
aws sts get-caller-identity --profile production --region us-east-1
npx cdk diff VipAdminDataStack VipAdminApiSegmentsStack ApiProgressiveDialerStack VipAdminApiSmsStack VipAdminApiCampaignsStack VipAdminApiPlansStack --app cdk.out --no-change-set --profile production
```

`--app cdk.out` uses the exact reviewed assembly for both diff and deployment. Preserve that assembly and its source revision; if it is rebuilt or either changes, repeat review before deployment. `--no-change-set` keeps the diff comparison read-only.

Inspect the current inline policies and log group before applying the approved additive policy. `PrecallSmsSenderAdditionalPerms` below is the proposed new inline policy name:

```bash
aws iam get-role-policy --role-name vip-sms-sender-role --policy-name SmsSenderPerms --profile production
aws iam get-role-policy --role-name vip-sms-sender-role --policy-name SmsSenderOptOutRead --profile production
aws logs describe-log-groups --log-group-name-prefix /aws/lambda/vip-admin-sms-retry-quiet-hours --profile production --region us-east-1
aws iam put-role-policy --role-name vip-sms-sender-role --policy-name PrecallSmsSenderAdditionalPerms --policy-document file://infra/config/precall-sms-sender-policy.json --profile production
aws iam put-role-policy --role-name VipAdminApiPlansStack-FunctionRole111A5701-mSfFlCntjbO0 --policy-name PrecallSmsPlansAdditionalPerms --policy-document file://infra/config/precall-sms-plans-policy.json --profile production
```

The retry log group was absent at review time. Recheck immediately before creating it with the recorded CMK; if another deployment has created it, verify its encryption instead. Apply the retained 365-day policy:

```bash
aws logs create-log-group --log-group-name /aws/lambda/vip-admin-sms-retry-quiet-hours --kms-key-id arn:aws:kms:us-east-1:165505826690:key/df585888-2f49-4de0-9cba-14803fda63f0 --profile production --region us-east-1
aws logs put-retention-policy --log-group-name /aws/lambda/vip-admin-sms-retry-quiet-hours --retention-in-days 365 --profile production --region us-east-1
npx cdk deploy VipAdminApiCampaignsStack VipAdminApiPlansStack --app cdk.out --require-approval broadening --profile production
```

The deployment must include the reviewed dependency updates. Record stack events, resulting Lambda versions/configuration and the exact source revision. Do not mark deployment complete from a local synth alone.

Publish the frontend after the backend deployment succeeds. A Vite production-mode build also requires the actual production `VITE_*` values: Cognito pool/client/domain, signin/signout URLs, API URL, region and `VITE_PREVIEW_MODE=false`. Verify them against the deployed Auth/API/Hosting outputs and Cognito callbacks. A build can succeed with empty IDs and localhost redirects; the earlier offline verification build did exactly that and must not be published.

For a new build using a reviewed `frontend/.env.production`, the file-presence guard must succeed before building:

```bash
test -f frontend/.env.production && npm --prefix frontend run build
```

Alternatively inject the verified public variables into the build process. Never put credentials, tokens or client secrets in `VITE_*`: these values become public JavaScript. For this release, commit `a9f8f9cfd614434bd715faf7ee1c5ee39415e61e` has already been rebuilt with the verified values injected, without copying an env file into the worktree. Reuse that `frontend/dist` and check the [production manifest](/home/devaju/projects/_audit-reports/connect-precall-sms-review-2026-09-11/deployment/frontend-production-manifest.json) and [preparation record](/home/devaju/projects/_audit-reports/connect-precall-sms-review-2026-09-11/deployment/frontend-production-preparation.md). Do not run another build without those values.

The following sequence is tied to that manifest. It verifies the destination, checks local hashes, uploads fingerprinted assets with immutable caching, confirms the JS/CSS lengths, and only then replaces the HTML with no-cache headers. Existing hashed assets are retained. Update the filenames and expected lengths from the new manifest if a later release is built.

```bash
set -euo pipefail
PRECALL_ASSET_BUCKET=$(aws cloudformation describe-stacks --stack-name VipAdminHostingStack --profile production --region us-east-1 --query "Stacks[0].Outputs[?OutputKey=='AssetBucketName'].OutputValue" --output text)
PRECALL_DISTRIBUTION_ID=$(aws cloudformation describe-stacks --stack-name VipAdminHostingStack --profile production --region us-east-1 --query "Stacks[0].Outputs[?OutputKey=='DistributionId'].OutputValue" --output text)
test "${PRECALL_ASSET_BUCKET}" = vip-admin-ui-assets-165505826690
test "${PRECALL_DISTRIBUTION_ID}" = E3QCDJPG0LCO7E
(
  cd frontend/dist
  sha256sum --check /home/devaju/projects/_audit-reports/connect-precall-sms-review-2026-09-11/deployment/frontend-production.sha256
)
aws s3 cp frontend/dist/assets/ "s3://${PRECALL_ASSET_BUCKET}/assets/" --recursive --cache-control 'public,max-age=31536000,immutable' --profile production --region us-east-1 --only-show-errors
test "$(aws s3api head-object --bucket "${PRECALL_ASSET_BUCKET}" --key assets/index-W-CPVmeK.js --profile production --region us-east-1 --query ContentLength --output text)" = 621496
test "$(aws s3api head-object --bucket "${PRECALL_ASSET_BUCKET}" --key assets/index-C9_byHmj.css --profile production --region us-east-1 --query ContentLength --output text)" = 39159
aws s3 cp frontend/dist/index.html "s3://${PRECALL_ASSET_BUCKET}/index.html" --content-type 'text/html; charset=utf-8' --cache-control 'no-cache, no-store, must-revalidate' --profile production --region us-east-1 --only-show-errors
aws cloudfront create-invalidation --distribution-id "${PRECALL_DISTRIBUTION_ID}" --paths '/*' --profile production
```

Record the invalidation ID and wait until it is completed. Verify the public `/` and `/index.html` responses reference `index-W-CPVmeK.js`, both entry assets load, and HTML cache headers match. The deployment evidence includes the previous HTML for rollback; retaining old assets allows the owner to restore that entry HTML and invalidate again if required.

## Local verification evidence

The source verification recorded 1,967 passing Python tests, including 1,201 for API Plans; 601 passing frontend tests; and a successful frontend typecheck/production build. After the deployment correction, all 263 infrastructure tests passed with 100% reported coverage, replacing the earlier 262-test result. This is 2,831 passing tests across the three suites. CDK synthesis and Checkov also passed again after the correction; see `deployment/recovery-synth.txt`, `recovery-infra-tests.txt` and `recovery-checkov.json`.

Checkov reported 353 passed, 23 skipped and zero failed checks, with no parsing errors. Its remote guidelines fetch was unavailable; the local checks completed. Semgrep reported zero findings across 295 rules and 388 files. Its eight error entries comprise six partial-parse entries affecting five files and two executor timeouts; the separate final executor scan resolved those timeouts, running 190 rules with zero findings and zero errors. The partial-parse limitations remain. These results do not establish the absence of all security or runtime defects.

Evidence is saved under `/home/devaju/projects/_audit-reports/connect-precall-sms-review-2026-09-11`: `python-results.json`, `final-api-plans.txt`, `frontend-tests.txt`, `frontend-build.txt`, `infra-offline.txt`, `final-synth.txt`, `checkov.json`, `semgrep.json`, `semgrep-final-executor.json`, `aws-diff.txt` and `live-prerequisites.json`. `REVIEW.md` records the release owner's final assessment. No production mutation or live canary result is implied by these checks.

## E2E evidence and recovery

Task 7 is still a live verification gate, including its real Sunday case. Use the approved test recipients and UI-created test plan. Record actual sender/provider/first-call timing; canonical and snapshot-based cohorts; pending→complete and timeout behavior; opt-out; local contact windows; campaign cancellation; Force Start; and watchdog outcomes. A local test suite cannot substitute for those observations. Do not silently pass Task 7's original strict-ordering requirement: attach the measured limits above and resolve acceptance explicitly before declaring that gate complete or starting Phase II/III.

For a paused campaign, inspect only the relevant run's initialization state/timestamps and snapshot status first. Stop/cancel uses the established UI operation and makes the SMS lifecycle terminal. Force Start is an explicit new lifecycle and may send another SMS; it is not a harmless snapshot-poll action. Do not clear generation/claim markers or edit run rows manually to force recovery.

Keep phones, first names and rendered bodies out of diagnostic exports. The snapshot evidence contains headers/status metadata, without recipient rows. Deployment outcomes and the synthetic probes are recorded in the linked evidence directory. No real SMS or call was initiated for verification. Live E2E/canary completion remains pending.
