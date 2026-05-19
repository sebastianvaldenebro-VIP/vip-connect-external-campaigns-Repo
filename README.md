# vip-connect-external-campaigns

External Outbound Campaigns for Amazon Connect — Redis-driven real-time dialer feeder that bypasses Customer Profiles segments to achieve ~5 min lag from CRM update to dial.

## Architecture

```
Redis wait_list (source of truth, 5-min CRM rebuild)
      │
      ▼
EventBridge cron (5 min)
      │
      ▼
Lambda feeder ───▶ reads ExternalCampaignFilters (DynamoDB)
      │           filters per campaign config
      │           checks comm limits + schedule + timezone
      │           tracks each push in ExternalCampaignDialTracking
      ▼
Connect Campaigns API ── PutDialRequestBatch ──▶ Predictive dialer
                                                  │
                                                  ▼
                                        Call + Agent Workspace
```

Campaigns are pre-created in Connect (via the React admin app, Day 3-4). This
repo only feeds them.

## Prerequisites

- Node.js 20.x
- Python 3.12
- AWS CLI with credentials for account `165505826690`
- AWS CDK v2 (installed via `npm install`)

## Setup

```bash
npm install
```

Then discover and fill in the infra context values in [cdk.json](cdk.json):

```bash
# Get VPC config from the existing Redis Lambda
aws lambda get-function-configuration \
  --function-name connectcampaignRedisSub \
  --region us-east-1 \
  --query 'VpcConfig'
```

Copy `SubnetIds`, `VpcId`, and `SecurityGroupIds` into `cdk.json`.

For Redis credentials, either:
- Use the same Secrets Manager secret the existing Lambda uses, OR
- Create a new one: `aws secretsmanager create-secret --name redis-creds-feeder ...`

## Deploy

```bash
# First time: bootstrap CDK in the account
npx cdk bootstrap aws://165505826690/us-east-1

# Deploy data stack (DynamoDB + KMS)
npm run deploy:data

# Deploy feeder stack (Lambda + EventBridge)
npm run deploy:feeder
```

## Project layout

| Path | Purpose |
|---|---|
| `infra/bin/app.ts` | CDK entry point |
| `infra/lib/stacks/data-stack.ts` | DynamoDB tables + KMS CMK |
| `infra/lib/stacks/feeder-stack.ts` | Lambda feeder + EventBridge rule |
| `services/feeder/src/` | Python Lambda — Clean Architecture |
| `services/feeder/src/domain/` | Entities + domain services (no external deps) |
| `services/feeder/src/application/` | Orchestrator (use case) |
| `services/feeder/src/infrastructure/` | DDB + Redis + Connect SDK adapters |
| `services/feeder/tests/` | Unit + integration tests |

## Commands

```bash
npm run build           # Compile TypeScript
npm run synth           # cdk synth (check infra compiles)
npm run diff            # cdk diff (preview changes)
npm run deploy          # Deploy all stacks
npm run test:infra      # Jest infra tests
npm run test:feeder     # pytest Lambda tests
```

## DynamoDB tables

### ExternalCampaignFilters
Campaign configuration equivalent to a Customer Profiles segment + schedule.
- PK: `campaign_id`
- Operator-editable via the React admin app (Day 3-4)

### ExternalCampaignDialTracking
Tracks every dial request lifecycle — replaces Connect's native comm limits.
- PK: `lead_id`
- SK: `campaign_id#pushed_at`
- GSI1 for reattempt scheduling: `campaign_id` / `retry_scheduled_at`
- TTL: 30 days (rolling window for comm-limit evaluation)

### ExternalCampaignAudit
HIPAA audit trail of campaign/filter changes.
- PK: `entity_id`
- SK: `timestamp`
- TTL: 6 years

All tables use KMS CMK encryption with annual rotation and PITR enabled.

## HIPAA posture

- KMS CMK (customer-managed) for all data-at-rest
- TLS 1.2+ for all data-in-transit (enforced by AWS SDKs)
- VPC-attached Lambda (private subnets, no public IP)
- No PHI in CloudWatch Logs (structured logger scrubs PII; uses lead_id hash)
- Audit log retention ≥ 6 years via TTL
- IAM least-privilege policies with resource scoping
- MFA enforced via Cognito (Day 3 admin app)

## Roadmap

| Day | Milestone |
|---|---|
| 1 | CLI validation of External Campaign pattern ✅ |
| 2 | **This repo** — Feeder + DynamoDB + EventBridge |
| 3 | Disposition listener (Kinesis consumer for reattempts) |
| 4-5 | React admin app (Amplify + API Gateway + Cognito) |
| 6 | Rollout — gradual traffic migration from segment-based campaigns |
