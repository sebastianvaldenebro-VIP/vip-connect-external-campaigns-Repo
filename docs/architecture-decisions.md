# Architecture Decision Records

Canonical record of non-obvious choices made during design. Each ADR is immutable once accepted; supersessions are new ADRs.

---

## ADR-001 — Reject V2 external push for voice outbound campaigns

**Status:** Accepted — 2026-04-22
**Context:** The original goal was to push dial requests from Redis directly into AWS Outbound Campaigns so operators would get native `AWS/Connect/Campaigns` metrics plus the ML-powered predictive dialer, with lag ≤ 5 min (Redis cycle cadence).

We empirically tested 5 variants, all of which failed:

| # | Config | Result |
|---|---|---|
| 1 | V1 CreateCampaign + `PutDialRequestBatch` | `InvalidInput` — CloudTrail shows `Expiration exceeds maximum of 15 minutes` (fixed → still `Operation is not valid for this campaign` via V2 describe) |
| 2 | V2 CreateCampaign + `source.customerProfilesSegmentArn` + `PutOutboundRequestBatch` | `ValidationException: Operation is not valid for this campaign` |
| 3 | V2 with `eventTrigger` source + no schedule | `Missing required campaign parameter Schedule` |
| 4 | V2 with `eventTrigger` + schedule but with `communicationTimeConfig` | `Event Triggered Campaigns do not allow parameter: CommunicationTimeConfig` |
| 5 | V2 with `eventTrigger` + schedule without `communicationTimeConfig` | `Operation is not valid for this campaign` |

**Evidence sources:**
- [aws-samples/voice-channel-for-outbound-campaigns](https://github.com/aws-samples/voice-channel-for-outbound-campaigns) — official AWS sample uses Step Functions + Lambda + `StartOutboundVoiceContact` for voice custom-list dialing, explicitly NOT `PutDialRequestBatch`
- [aws-samples/amazon-connect-agentless-outbound-campaign](https://github.com/aws-samples/amazon-connect-agentless-outbound-campaign) — same pattern for agentless
- AWS doc states `PutProfileOutboundRequestBatch` *"cannot be directly invoked"* — only customer-profile event triggers invoke it
- No re:Post / StackOverflow results for "Operation is not valid for this campaign" with voice + external push, indicating the pattern is not a customer path AWS supports

**Decision:** Abandon external push into Outbound Campaigns V2. Voice + external ad-hoc push is not a supported pattern in the current API shape.

**Consequences:**
- ✅ We stop burning time on an unsupported API combination
- ✅ We align with AWS's own reference architectures
- ❌ We lose native `AWS/Connect/Campaigns` metrics for ad-hoc flows (mitigated by ADR-002)
- ❌ No predictive ML dialer for real-time dials (acceptable — pacing is simple enough for operator-managed campaigns)

**Related:** Test CloudTrail event IDs documented: `2beeb772-1885-4c0c-af2f-fe17b59c9842`, `8ab3c6a4-2dfa-4aec-b86f-37bfa5529f6e`.

---

## ADR-002 — Segment-driven campaigns with UI-triggered recompute

**Status:** Accepted — 2026-04-22
**Context:** With ADR-001 closing off external push, we must choose how operators get fresh data into campaigns. Options evaluated:

1. **Segment-driven campaigns + UI-triggered refresh** (chosen)
2. Pure `StartOutboundVoiceContact` via Lambda feeder (no campaigns at all)
3. Event-triggered campaigns (only fires when profile events match)

**Decision drivers:**
- Operators **require** native campaign metrics (Delivery, ContactsPlaced, AMD, abandonment) for reporting → rules out Option 2 on its own
- Operators already understand the segment-builder workflow → lowest training cost
- `CreateSegmentEstimate` forces recomputation on demand (doc-confirmed) → mitigates the 24h lag that made segment-driven painful in the past
- Event triggers require `PutProfileObject` to be configured as a trigger source — high-risk change to production `connectcampaignRedisSub`

**Decision:** Use native segment-driven Outbound Campaigns. The admin UI issues `CreateSegmentEstimate` whenever the operator creates, edits, or previews a segment, forcing a recompute against live Profile Attributes. Campaigns launched from the UI benefit from fresh snapshots.

**Consequences:**
- ✅ Full native metrics for free
- ✅ Fresh data without waiting for Connect's ~24h automatic refresh
- ✅ No changes to `connectcampaignRedisSub` (production stays stable)
- ⚠️ Per-segment `CreateSegmentEstimate` is async — UI has to poll. Latency p95 ≈ 60–90s. Acceptable for operator workflow.
- ⚠️ Segments in Customer Profiles are **immutable after creation**. Editing narrows → delete + recreate pattern. UI hides this, but worth noting for support.

**Superseded plans:**
- External push via Lambda feeder (ADR-001)
- Hybrid (segments + StartOutboundVoiceContact) — discarded for MVP simplicity; may revisit post-MVP if real-time < 5-min lag becomes a hard requirement.

---

## ADR-003 — React + CloudFront/S3 + API Gateway + Lambda stack

**Status:** Accepted — 2026-04-22 (updated 2026-05-13)
**Context:** Choice of frontend hosting, auth, and backend API layer.

**Alternatives considered:**

| Stack | Pros | Cons | Verdict |
|---|---|---|---|
| **React SPA + S3/CloudFront + API GW + Lambda + Cognito** ⭐ | Managed CDN, zero-config TLS, Cognito MFA out of box, pay-per-request backend, CDK-friendly | Manual deploy (S3 sync + CloudFront invalidation) vs. Amplify CI/CD | **Chosen** |
| React SPA + Amplify | Auto CI/CD from GitHub | CFN exec role lacks required IAM permissions; Amplify stack was removed after deploy failures | Rejected |
| Streamlit on Fargate + ALB + Cognito | Python everywhere, zero frontend code | Slower iterations, VPC+ALB overhead, less flexible UI | Rejected |
| Retool / internal tool builder | Fastest MVP | External SaaS, additional BAA, vendor lock-in | Rejected (HIPAA sensitive) |
| Next.js on Lambda | Full-stack in one repo | More complex than SPA for this volume | Rejected |

**Why Amplify was removed:** The CDK CFN execution role (`VipAdminCdkCfnExecPolicy`) is a custom least-privilege policy that does not include Amplify IAM actions. Adding those permissions would require coordinating with the account owner to update the policy. The same restriction affects CloudWatch and SNS. Decision: keep hosting minimal (S3 + CloudFront, both already permitted) and manage Amplify-equivalent CI/CD via the deploy script.

**Decision:** React 18 + Vite + TypeScript + Shadcn/ui + Tailwind + TanStack Query. Hosted on S3 + CloudFront. Auth via Cognito User Pool with Hosted UI. API Gateway HTTP API (not REST — simpler, cheaper) fronting 5 Python 3.12 Lambdas plus the Plans Orchestrator Lambda. Frontend deploy: `npm run build` → `aws s3 sync` → `aws cloudfront create-invalidation`.

**Consequences:**
- ✅ Zero additional IAM policy changes needed; S3 + CloudFront are already in the permitted action set
- ✅ Cognito Hosted UI = zero custom auth code
- ✅ HTTP API is ~70% cheaper than REST API and supports JWT authorizer natively
- ✅ CDK manages all infrastructure except monitoring (SNS + CloudWatch)
- ⚠️ No automatic CI/CD from GitHub — deploys are manual via the commands in `docs/runbook.md`

---

## ADR-004 — HIPAA posture: KMS CMK, VPC posture, permission boundary, operational alerting

**Status:** Accepted — 2026-04-22 (updated 2026-05-13)
**Context:** The UI handles phone numbers, names, lead IDs, and audit records — all fall under HIPAA per the [18 PHI identifiers](https://www.hhs.gov/hipaa/for-professionals/privacy/special-topics/de-identification/index.html). Design must meet HIPAA Security Rule technical safeguards.

**Decision:**

| Control | Implementation |
|---|---|
| **Encryption at rest — CMK** | Reuse existing `alias/prod/external-campaigns/data` KMS CMK (annual rotation enabled). All new DynamoDB tables + CloudWatch Log Groups encrypted with it. |
| **Encryption in transit** | API Gateway enforces TLS 1.2+; Cognito tokens over HTTPS; internal AWS service-to-service already TLS. |
| **Unique user identification** | Cognito User Pool. Each operator has their own sub claim surfaced in every audit row. |
| **MFA** | `mfa_configuration = "ON"` on Cognito User Pool. Software TOTP is required; SMS backup. |
| **Automatic session timeout** | React app uses idle timer (15 min idle → force re-auth). Cognito ID tokens expire 1h server-side regardless. |
| **Audit trail ≥ 6 years** | `AdminAuditLog` DynamoDB table with `ttl` ~189M seconds ahead. Deletion protection enabled. Every mutation goes through `record_audit()` in `services/shared/audit.py`. |
| **Access controls** | IAM least-privilege per Lambda. All roles have the mandatory `EngineeringPermissionBoundary` attached. API Gateway JWT Authorizer validates Cognito tokens server-side. |
| **Logging without PHI** | `StructuredLogger` in shared layer SHA-256-hashes any field matching a PHI regex (phone, first_name, last_name, email, SSN, address). |
| **VPC posture** | `api-plans` and `api-segments` are VPC-attached (require Redis/ElastiCache access). `api-campaigns`, `api-profiles`, `api-metrics` call only AWS public-endpoint APIs — no VPC needed. (`feeder` is gone — decommissioned; `aws lambda get-function` returns ResourceNotFoundException, verified live 2026-08-21.) |
| **Dedicated account for PHI** | Account `165505826690` already holds the Customer Profiles domain. No cross-account data movement. |
| **Breach readiness** | CloudTrail captures all API calls. GuardDuty assumed enabled at org level. 58 CloudWatch Alarms (verified live 2026-08-21, up from the original 14 created 2026-05-13) → SNS topic `vip-admin-alerts` provide automated operational alerting. |
| **Operational alerting** | SNS topic `arn:aws:sns:us-east-1:165505826690:vip-admin-alerts` with email subscription. 58 alarms as of 2026-08-21 — Lambda errors/throttles/duration across every function in this repo, DynamoDB system errors and throttles across all 11 in-repo VIP tables (see below), EventBridge failed invocations, API Gateway 5xx (backstop for handlers that convert application errors into well-formed HTTP responses, which AWS/Lambda's own Errors metric can't see), and app-level custom metrics (`VIPPlans` namespace). Created via CLI (CFN exec role lacks SNS + cloudwatch:PutDashboard permissions). |

**DynamoDB alarm coverage (verified live 2026-08-21):** every AWS/DynamoDB
`SystemErrors`/`ThrottledRequests` alarm in this account was, until this date,
dimensioned by `TableName` alone — a dimension combination DynamoDB never
actually publishes data under (it's always `{TableName, Operation}`), so the
two alarms that existed (both on `VipAdminPlans`) had never once received a
datapoint and had already missed a real error on 2026-08-07. Fixed for all 11
VIP tables owned by this repo (`VipAdminPlans`, `VipActiveBrandedCampaigns`,
`VipAdminSegmentFilterConfig`, `VipAgentSnapshot`, `VipBrandedCampaignMetrics`,
`VipBrandedRunSummary`, `VipLocationMapping`, `VipProgressiveAgentLocks`,
`VipProgressiveCampaignQueue`, `VipSmsCampaignQueue`, `VipSmsCampaignRuns`) via
a Metric Math sum across all 8 possible DynamoDB operations per table (CLI
alarms cannot use `SEARCH()` — confirmed live, "SEARCH is not supported on
Metric Alarms" — so each operation is an explicit `MetricStat` summed with
`FILL(...,0)`). `vip-connect-deny-list` (12th `Vip*`-tagged table) is
excluded: manually managed by a different team's Lambdas, out of this repo's
scope.

**Monitoring architecture note:** The MonitoringStack CDK construct (`infra/lib/stacks/monitoring-stack.ts`) exists as a reference implementation but is NOT instantiated in `app.ts`. The CFN execution role `VipAdminCdkCfnExecPolicy` lacks SNS and CloudWatch alarm/dashboard permissions. All monitoring resources (SNS topic, alarms, dashboard) were created via AWS CLI and persist independently of CDK — see `infra/scripts/create-alarms.sh` (58 alarms) and `infra/scripts/create-progressive-dialer-alarms.sh` (10 alarms, separate `vip-progressive-dialer-alerts` topic) for the exact current set. Do not attempt to add them back to CDK without first updating the CFN exec role policy.

**Consequences:**
- ✅ All HIPAA technical safeguards are met — reproducible deploys
- ✅ Permission boundary enforcement means Lambdas cannot escalate even if compromised
- ✅ Audit trail is append-only by design (no update/delete IAM actions on that table)
- ✅ Operational alerting is live — SNS emails are sent on every alarm state change
- ⚠️ Session timeout relies on client-side enforcement + server token expiry. Defense-in-depth is acceptable per AWS HIPAA whitepaper.
- ⚠️ Monitoring is outside CDK — must be managed via CLI scripts if ever rebuilt

**Supersedes:** None (first ADR on compliance for this system).

---

## ADR-005 — Plans Orchestrator: EventBridge Scheduler per bucket, not Step Functions

**Status:** Accepted — 2026-04-30
**Context:** The Plans Orchestrator needs to poll running Connect campaigns every minute, sequence buckets, and chain downstream plans. Options:

1. **EventBridge Scheduler `rate(1 min)` per active bucket** (chosen)
2. AWS Step Functions with `.waitForTaskToken` + `Wait` states
3. Single long-running Lambda with `time.sleep` loops

**Decision drivers:**
- Step Functions Express Workflows have a 5-minute limit; Standard Workflows charge per state transition and are complex to instrument for our DAG semantics
- Long-running Lambda with sleep would consume the 15-min timeout and is un-restartable
- EventBridge Scheduler is serverless, costs ~$0.000001/invocation, and creates an auditable schedule per bucket run — easy to inspect and clean up

**Decision:** Create one EventBridge Scheduler schedule per active bucket run (group `vip-plans`, name `vip-plan-{planId}-run-{runId}-b{index}`). The schedule fires `rate(1 min)` into api-plans with `{"action":"tick",...}`. When a bucket completes, the schedule is deleted. If a run is aborted, all its schedules are deleted.

**Consequences:**
- ✅ Zero Lambda timeout risk
- ✅ Schedules are inspectable in the AWS console / CLI
- ✅ Clean separation: one schedule = one active bucket = easy operational reasoning
- ⚠️ There is a brief window at plan startup where the tick hasn't fired yet (~0–60s lag from `start_run` to first dispatch)
- ⚠️ EventBridge Scheduler is not in CDK (same CFN exec role restriction as monitoring); schedules are created/deleted by the Lambda itself at runtime via `scheduler_manager.py`
