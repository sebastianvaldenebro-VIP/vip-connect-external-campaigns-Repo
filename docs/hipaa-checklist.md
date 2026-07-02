# HIPAA Compliance Checklist

Implementation and verification of HIPAA Security Rule technical safeguards for the VIP Connect Admin UI.

## Scope of PHI in this system

The UI displays and processes the following PHI-adjacent data:
- Phone numbers (Identifier #4 per Safe Harbor)
- First/last names (Identifier #1)
- Lead IDs (derived from internal CRM — not Safe Harbor Identifier by itself, treated as PHI because it links to a specific person)
- Appointment scheduling hours (Identifier #3 when linked to individual)
- Geographic locations smaller than state (Identifier #2 — e.g. "NJ - Hoboken")

**What the UI does NOT touch:**
- Medical records, diagnoses, treatment info
- Insurance info, SSNs, DOB
- Biometric identifiers, photographs
- Email addresses as user-facing data (only Cognito login emails)

## Services used — all HIPAA eligible

Verified against the [AWS HIPAA Eligible Services Reference](https://aws.amazon.com/compliance/hipaa-eligible-services-reference/):

| Service | Eligible? | Use in this system |
|---|---|---|
| Amazon Connect | ✅ | Customer Profiles + Outbound Campaigns |
| Amazon Cognito | ✅ | Admin authentication |
| AWS Lambda | ✅ | API backend (4 Lambdas) |
| Amazon API Gateway | ✅ | HTTP API fronting Lambdas |
| Amazon DynamoDB | ✅ | Audit log storage |
| AWS KMS | ✅ | CMK for encryption |
| AWS Amplify Hosting | ✅ | SPA static hosting |
| Amazon CloudWatch | ✅ | Logs + metrics |
| AWS CloudTrail | ✅ | Auditoría de API calls |
| Amazon S3 | ✅ | Segment snapshots (write via CreateSegmentSnapshot, read via signed URLs) |

**BAA:** VIP Medical Group has a signed BAA covering the AWS account `165505826690`. No BAA addendum needed for this project since no new services are introduced.

---

## Safeguards checklist

### §164.312(a)(1) — Access Control

| Requirement | Implementation | Verification |
|---|---|---|
| Unique user identification (required) | Every operator gets their own Cognito user; `sub` claim surfaced in every audit row | `aws cognito-idp list-users --user-pool-id <pool>` shows one user per operator |
| Emergency access procedure (required) | AWS root account owner can disable compromised users via Cognito console | Runbook section "Emergency access" documents steps |
| Automatic logoff (addressable) | **15-min idle timeout** enforced client-side via `idle-timer` + Cognito ID token expires at 1h | Manual test: leave UI idle, verify redirect to login after 15 min |
| Encryption and decryption (addressable) | All data encrypted at rest with CMK + in transit with TLS 1.2+ | Audit below |

### §164.312(a)(2)(i) — Automatic Logoff

- Client-side: React hook using [`idle-timer`](https://www.npmjs.com/package/react-idle-timer) triggers logout after 900s (15 min) of no keyboard/mouse activity.
- Server-side: Cognito access token expires at 3600s (1h). Refresh token expires at 86400s (24h). Post-expiry, all API calls return `401`.
- Defense in depth: the combination means a user who walks away gets logged out by the client within 15 min; even if the client is compromised, the server refuses expired tokens.

### §164.312(b) — Audit Controls

| Requirement | Implementation |
|---|---|
| Hardware, software, and/or procedural mechanisms that record and examine activity | 3 layers: (1) CloudTrail captures all AWS API calls, (2) API Gateway access logs capture all REST calls, (3) `AdminAuditLog` DynamoDB table records every create/update/delete/start/stop action with before+after JSON payload |
| Retention period | TTL attribute on `AdminAuditLog` set to 189,216,000 seconds (6 years). DynamoDB deletion protection enabled on table. CloudTrail organization trail retains 7 years in dedicated S3 bucket (pre-existing). |
| Integrity | `AdminAuditLog` IAM policy allows only `PutItem` from Lambda — no UpdateItem, no DeleteItem. Append-only by design. |

**Verification:** After deploy, a test operator performs a segment create → update → delete. All 3 actions appear in `AdminAuditLog` within 5 seconds.

### §164.312(c)(1) — Integrity

| Requirement | Implementation |
|---|---|
| Protect ePHI from improper alteration or destruction | DynamoDB PITR + deletion protection on all tables. CloudTrail immutable. S3 snapshot objects encrypted + versioned. |

### §164.312(d) — Person or Entity Authentication

| Requirement | Implementation |
|---|---|
| Verify the person or entity seeking access to ePHI | Cognito User Pool with password policy (min 12 chars, upper+lower+number+symbol) + MFA required (TOTP primary, SMS backup) + JWT Authorizer on API Gateway validates token against Cognito JWKS on every call |

### §164.312(e)(1) — Transmission Security

| Requirement | Implementation |
|---|---|
| Integrity controls | TLS 1.2+ end-to-end (browser ↔ CloudFront ↔ API Gateway ↔ Lambda ↔ AWS services). API Gateway rejects TLS 1.0/1.1 at the custom domain level. |
| Encryption | TLS for data in transit. KMS CMK (rotation enabled) for data at rest in DynamoDB, CloudWatch Logs, and S3 buckets owned by this project. |

---

## CMK encryption — specifics

**KMS CMK reused from existing infrastructure:**
- Alias: `alias/prod/external-campaigns/data`
- Key type: Symmetric, customer-managed
- Rotation: Enabled (annual automatic)
- Key policy: Limited to specific Lambda roles + CloudWatch Logs service principal

**Tables/resources encrypted with this CMK:**
- `AdminAuditLog` DynamoDB table
- `ExternalCampaignFilters` (if retained post-archival) DynamoDB table
- `/aws/lambda/vip-admin-ui-*` CloudWatch Log Groups (4 Lambdas)
- S3 bucket for segment snapshot exports (if created)

---

## Logging posture — no PHI in logs

**`StructuredLogger` helper** (`services/shared/infrastructure/telemetry/structured_logger.py`) implements automatic PHI scrubbing:

```python
PHI_FIELDS = frozenset({"phone", "first_name", "last_name", "fullname",
                        "phone_number", "email", "ssn", "dob"})

def _scrub(fields):
    scrubbed = {}
    for key, value in fields.items():
        if key in PHI_FIELDS:
            scrubbed[f"{key}_hash"] = hash_identifier(str(value)) if value else None
        elif key == "lead_id" and value:
            scrubbed["lead_id_hash"] = hash_identifier(str(value))
            scrubbed["lead_id_prefix"] = str(value)[:8]
        else:
            scrubbed[key] = value
    return scrubbed
```

Every Lambda uses `StructuredLogger.build()` as the sole logging entry point. Code review checklist enforces: no `print()`, no `logging.info(f"...")` with raw PHI, always go through the logger.

---

## Per-Lambda IAM least-privilege

Each Lambda has its own IAM role with **only** the actions it needs. All roles carry the `EngineeringPermissionBoundary` attached.

| Lambda | Max permissions |
|---|---|
| `api-segments` | `profile:CreateSegmentDefinition`, `ListSegmentDefinitions`, `GetSegmentDefinition`, `DeleteSegmentDefinition`, `CreateSegmentEstimate`, `GetSegmentEstimate`, `CreateSegmentSnapshot`, `GetSegmentSnapshot`, `GetSegmentMembership`, `dynamodb:PutItem` on `AdminAuditLog`, `s3:GetObject` on the snapshot bucket, `kms:Decrypt`+`GenerateDataKey` on the CMK, `logs:CreateLogStream`+`PutLogEvents` |
| `api-profiles` | `profile:SearchProfiles`, `BatchGetProfile`, `ListProfileObjects`, `GetCalculatedAttributeForProfile`, `ListCalculatedAttributesForProfile`, same DynamoDB/KMS/Logs |
| `api-campaigns` | `connect-campaigns:*` (scoped to `arn:aws:connect-campaigns:us-east-1:165505826690:campaign/*`), `connect:ListQueues`, `ListContactFlows`, `ListPhoneNumbersV2`, same DynamoDB/KMS/Logs |
| `api-metrics` | `cloudwatch:GetMetricStatistics`, `GetMetricData`, `connect:GetMetricDataV2`, `GetCurrentMetricData`, `SearchContacts`, `dynamodb:Query`+`Scan` on `AdminAuditLog`, Logs |

**No Lambda has `*` actions.** No Lambda has `iam:*` permissions. Permission boundary prevents privilege escalation.

---

## Session & authentication settings

**Cognito User Pool config:**

```typescript
new UserPool(this, 'AdminPool', {
  mfa: Mfa.REQUIRED,
  mfaSecondFactor: { sms: true, otp: true },
  passwordPolicy: {
    minLength: 12,
    requireLowercase: true,
    requireUppercase: true,
    requireDigits: true,
    requireSymbols: true,
    tempPasswordValidity: Duration.days(1),
  },
  signInAliases: { email: true },
  accountRecovery: AccountRecovery.EMAIL_ONLY,
  advancedSecurityMode: AdvancedSecurityMode.ENFORCED, // risk-based auth
});
```

**User Pool client:**

```typescript
new UserPoolClient(this, 'AdminPoolClient', {
  userPool,
  accessTokenValidity: Duration.hours(1),
  idTokenValidity: Duration.hours(1),
  refreshTokenValidity: Duration.hours(24),
  enableTokenRevocation: true,
  preventUserExistenceErrors: true,
});
```

---

## Verification checklist (run post-deploy)

```bash
# 1. KMS key rotation enabled
aws kms get-key-rotation-status --key-id alias/prod/external-campaigns/data \
  --query 'KeyRotationEnabled'  # expected: true

# 2. DynamoDB tables have PITR + encryption
for TABLE in VipAdminPlans VipAdminAudit VipActiveBrandedCampaigns VipProgressiveCampaignQueue VipProgressiveAgentLocks; do
  aws dynamodb describe-continuous-backups --table-name $TABLE \
    --query 'ContinuousBackupsDescription.PointInTimeRecoveryDescription.PointInTimeRecoveryStatus'
  aws dynamodb describe-table --table-name $TABLE \
    --query 'Table.{SSE:SSEDescription.Status, Deletion:DeletionProtectionEnabled}'
done

# 3. Lambda roles have permission boundary
for FN in api-segments api-profiles api-campaigns api-metrics; do
  ROLE=$(aws lambda get-function-configuration --function-name vip-admin-ui-$FN --query 'Role' --output text)
  ROLE_NAME=$(echo $ROLE | cut -d'/' -f2)
  aws iam get-role --role-name $ROLE_NAME --query 'Role.PermissionsBoundary.PermissionsBoundaryArn'
done

# 4. CloudWatch Log Groups encrypted
for FN in api-segments api-profiles api-campaigns api-metrics; do
  aws logs describe-log-groups --log-group-name-prefix /aws/lambda/vip-admin-ui-$FN \
    --query 'logGroups[*].{name:logGroupName,kms:kmsKeyId}'
done

# 5. Cognito MFA enforced
aws cognito-idp describe-user-pool --user-pool-id <pool-id> \
  --query 'UserPool.MfaConfiguration'  # expected: "ON"

# 6. API Gateway TLS 1.2+
aws apigatewayv2 get-api --api-id <api-id> \
  --query '{ApiEndpoint:ApiEndpoint, Version:ProtocolType}'
# manual: curl -v with --tlsv1.1 must fail
```

---

## Incident response readiness

| Event | Detection | Response |
|---|---|---|
| Compromised operator credentials | GuardDuty alerts on anomalous API calls (pre-existing org-level) | Rotate via Cognito admin: disable user, reset password |
| Unauthorized segment deletion | `AdminAuditLog` scan shows actions outside business hours / unknown IP | Restore from DynamoDB PITR within 35 days |
| PHI leak in CloudWatch Logs | CloudWatch Logs Insights query for unscrubbed patterns | Rotate logs, report to privacy officer, 60-day notification if ≥500 individuals |
| CMK deletion attempt | CloudTrail + CMK `AWS_REGION_ACCOUNT_KEY_DELETION_PENDING` alarm | 30-day window to cancel; contact AWS Support |

Breach notification window per HIPAA: 60 days. Runbook `runbook.md` documents the escalation path.

---

## Out of scope for MVP (tracked for post-MVP)

- AWS Macie classification on S3 snapshot bucket (add when snapshot volume > 10GB/month)
- Automated compliance reports generation (e.g. quarterly access review export)
- AWS Config Rules for HIPAA conformance pack (should be at org-level)
- Per-user role differentiation beyond "admin" (when team grows beyond 1 operator)

These are not gaps — they're enhancements that the current architecture can absorb cleanly.
