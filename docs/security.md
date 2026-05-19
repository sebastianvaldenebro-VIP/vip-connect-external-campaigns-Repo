# Security — VIP Connect Admin UI

HIPAA-regulated system. All controls below are live in production unless marked `[planned]`.

---

## 1. Authentication & session

| Control | Implementation |
| --- | --- |
| Identity provider | Amazon Cognito User Pool (`VipAdminUserPool`) |
| MFA | TOTP enforced (`mfa_configuration = "ON"`, no opt-out) |
| Password policy | Min 12 chars · uppercase · lowercase · digit · special char |
| Session token | Cognito access token — 1 h expiry |
| Refresh token | 24 h; revoked on sign-out or admin disable |
| Idle timeout | Frontend tracks `lastActivity`; forces re-login after 900 s inactivity |
| Token storage | In-memory (React state + Amplify session); never localStorage / sessionStorage |

---

## 2. Authorization

All API Gateway routes are protected by a **JWT Authorizer** that validates the Cognito access token against the User Pool JWKS endpoint before the request reaches any Lambda.

Lambdas extract `sub` and `email` from the JWT claims via `vip_shared.application.http.extract_caller`. Every mutating operation writes an audit row with the actor's `sub` and `email` before returning.

Current model: single admin group. Multi-role RBAC is a planned post-MVP enhancement.

---

## 3. Network

```
Internet
  │
  ▼
CloudFront (edge, TLS 1.2+, HSTS)
  │
  ├── Static SPA assets (S3 origin, Origin Access Control)
  │
  └── API calls → API Gateway (TLS 1.2+ enforced, no HTTP)
                    │
                    ▼
                  Lambda (VPC-attached for feeder; api-* Lambdas call
                  AWS APIs via VPC Interface Endpoints — no public internet)
```

- API Gateway enforces TLS 1.2 minimum via the default HTTPS endpoint.
- CORS: `AllowOrigins` is locked to the Amplify distribution hostname.
- The existing `connectcampaignRedisSub` feeder stays inside the VPC (private subnets) and communicates with ElastiCache over a private route.
- VPC Interface Endpoints for `connectcampaignsv2`, `customer-profiles`, `dynamodb` keep all backend traffic off the public internet.

---

## 4. Encryption

| Data | At rest | In transit |
| --- | --- | --- |
| `AdminAuditLog` DynamoDB | SSE-KMS, CMK `alias/prod/external-campaigns/data` | TLS 1.2+ (AWS SDK) |
| `VipAdminPlans` DynamoDB | SSE-KMS, same CMK | TLS 1.2+ |
| Lambda environment variables | KMS-encrypted (Lambda env encryption feature) | — |
| CloudWatch Logs groups | KMS-encrypted log group | — |
| S3 snapshot bucket | SSE-KMS | TLS enforced via bucket policy (`aws:SecureTransport`) |
| Customer Profiles domain | AWS-managed (Profiles service) | TLS 1.2+ |

KMS key: `alias/prod/external-campaigns/data` — customer-managed CMK, annual automatic rotation enabled. Key policy grants usage to Lambda execution roles and denies everyone else.

---

## 5. IAM — Lambda execution roles

Each Lambda has its own least-privilege role. All roles have `EngineeringPermissionBoundary` applied, which caps the maximum effective permissions regardless of inline policies.

### `vip-admin-ui-api-plans` role

```text
dynamodb:GetItem, PutItem, Query, DeleteItem  → VipAdminPlans table + index
scheduler:CreateSchedule, DeleteSchedule, GetSchedule  → EventBridge Scheduler group vip-plans-*
connectcampaignsv2:CreateCampaign, StartCampaign, StopCampaign, DeleteCampaign, DescribeCampaign
profile:CreateSegmentDefinition, CreateSegmentEstimate, DeleteSegmentDefinition
dynamodb:PutItem  → AdminAuditLog (audit writes)
kms:Decrypt, GenerateDataKey  → alias/prod/external-campaigns/data
logs:CreateLogGroup, CreateLogStream, PutLogEvents  → /aws/lambda/vip-admin-ui-api-plans
```

### `vip-admin-ui-api-segments` role

```text
profile:CreateSegmentDefinition, ListSegmentDefinitions, GetSegmentDefinition,
  DeleteSegmentDefinition, CreateSegmentEstimate, GetSegmentEstimate,
  CreateSegmentSnapshot, GetSegmentSnapshot, GetSegmentMembership
dynamodb:PutItem  → AdminAuditLog
kms:Decrypt, GenerateDataKey  → alias/prod/external-campaigns/data
s3:GetObject  → snapshot bucket
logs:*  → /aws/lambda/vip-admin-ui-api-segments
```

### `vip-admin-ui-api-campaigns` role

```text
connectcampaignsv2:CreateCampaign, DescribeCampaign, ListCampaigns, DeleteCampaign,
  UpdateCampaignName, UpdateCampaignSource, UpdateCampaignSchedule,
  StartCampaign, StopCampaign, PauseCampaign, ResumeCampaign, GetCampaignState
connect:ListQueues, ListContactFlows, ListPhoneNumbersV2
dynamodb:PutItem  → AdminAuditLog
kms:Decrypt, GenerateDataKey  → alias/prod/external-campaigns/data
logs:*  → /aws/lambda/vip-admin-ui-api-campaigns
```

### `vip-admin-ui-api-profiles` role

```text
profile:SearchProfiles, BatchGetProfile, ListProfileObjects,
  GetCalculatedAttributeForProfile, ListCalculatedAttributesForProfile
dynamodb:PutItem  → AdminAuditLog
kms:Decrypt, GenerateDataKey  → alias/prod/external-campaigns/data
logs:*  → /aws/lambda/vip-admin-ui-api-profiles
```

---

## 6. PHI handling

No PHI flows through this system's application layer. The system manages:

- **Segment definitions** — filter criteria (state abbreviations, group labels, attempt numbers). Not PHI.
- **Campaign configurations** — queue IDs, flow IDs, phone numbers (operational). Caller phone numbers are stored in Connect Customer Profiles, owned by the upstream CRM → Connect pipeline.
- **Audit records** — `actor_sub`, `actor_email` (operator identity), entity IDs, action verbs, timestamps. No patient names, dates of service, or clinical data.

The `StructuredLogger` in `vip_shared.infrastructure.telemetry` hashes any value that looks like a phone number or email before emitting to CloudWatch Logs, as a belt-and-suspenders safeguard.

If PHI ever reaches a Lambda (e.g., a profile search result passes through `api-profiles`), the Lambda must not log the raw values — only log hashed references or opaque IDs.

---

## 7. Audit trail

**Table:** `AdminAuditLog` (DynamoDB)

Every API mutation records a row before returning the response. The `build_audit()` call in each handler writes synchronously — if it fails, the handler raises and the HTTP response is 500 (the action is not silently committed without an audit row).

| Field | Value |
| --- | --- |
| `entity_type` | `plan`, `run`, `segment`, `campaign` |
| `entity_id` | UUID of the affected entity |
| `action` | `create`, `update`, `delete`, `start`, `abort`, `clone_template`, … |
| `actor_sub` | Cognito `sub` claim (immutable user identifier) |
| `actor_email` | Cognito `email` claim (display convenience) |
| `ip_address` | From API Gateway `requestContext.identity.sourceIp` |
| `user_agent` | From request headers |
| `before` / `after` | Minimal JSON diff (non-PHI fields only) |
| `ttl` | Unix epoch 6 years from write time (DynamoDB TTL column) |

The table has **deletion protection enabled** and **PITR enabled**. TTL handles legal expiry; before TTL fires, records can be restored from PITR.

---

## 8. Secrets management

No secrets are hardcoded. Lambda environment variables that contain IDs (Connect instance ID, DynamoDB table name) are not secrets but are injected at deploy time by CDK and encrypted at rest by Lambda's KMS feature.

Any credential that would be a secret (API keys, tokens) must go into **Secrets Manager** with automatic rotation. Current system has none — all auth is IAM role-based.

---

## 9. Dependency & container scanning `[planned]`

- Trivy image scan on Lambda zip artifacts in CI pipeline
- `pip-audit` / `safety` for Python dependency CVEs
- These gates are not yet wired into the GitHub Actions workflow; tracked as a post-MVP hardening task.

---

## 10. Incident response — quick reference

| Severity | Trigger | First action |
| --- | --- | --- |
| PHI in logs | Macie finding or manual discovery | Disable affected Lambda, rotate KMS key, preserve logs |
| Credential leak | GitLeaks / manual report | `aws iam delete-access-key`, revoke Cognito sessions |
| Service down | API 5xx > 50% for 5 min | Check Lambda errors, last deploy, AWS Health |
| Brute-force | > 20 Cognito failures in 5 min | CloudWatch Alarm → lock account temporarily |

Full response checklist in `runbook.md` § 5.
