# Dependency Manifest

Complete dependency inventory across backend, shared layer, infrastructure, and frontend.

---

## 1. `vip_shared` Lambda Layer

Located at `services/shared/python/vip_shared/`. Imported as `from vip_shared.* import ...` by every api-* Lambda.

| Path | Purpose |
|---|---|
| `application/http.py` | HTTP response helpers: `json_response`, `parse_body`, `extract_caller` (decodes Cognito JWT claims from API Gateway event) |
| `domain/entities/filter_rule.py` | `FilterRule` value object — boolean operators, leaf comparisons, used by reconcile filters |
| `domain/entities/comm_limits.py` | `CommLimits` value object — communication frequency caps per patient |
| `domain/entities/schedule_spec.py` | `ScheduleSpec` — operator-defined schedule windows |
| `domain/services/filter_evaluator.py` | Evaluates `FilterRule` against a lead dict; pure logic |
| `domain/services/comm_limits_evaluator.py` | Applies `CommLimits` to decide if a patient can be contacted today |
| `domain/services/schedule_evaluator.py` | Decides if `ScheduleSpec` permits a contact at a given timestamp |
| `domain/services/segment_groups_translator.py` | Maps operator-friendly group labels (`"Cancellation / 1st attempt"`) to Customer Profiles filter primitives |
| `infrastructure/persistence/outbound_campaigns_client.py` | Thin wrapper over `boto3.client("connectcampaignsv2")` with retries + structured errors |
| `infrastructure/persistence/customer_profiles_client.py` | Wrapper over `boto3.client("customer-profiles")` |
| `infrastructure/persistence/connect_client.py` | Wrapper over `boto3.client("connect")` (queues, contact flows) |
| `infrastructure/persistence/redis_lead_source.py` | Reads `wait_list:{team}:list`, applies server-side filters |
| `infrastructure/persistence/audit.py` | Writes audit records to `VipAdminAudit` |
| `infrastructure/persistence/cloudwatch_client.py` | Wrapper for `PutMetricData` |
| `infrastructure/persistence/segment_filter_config.py` | Loads filter templates for segment construction |
| `infrastructure/persistence/snapshot_reader.py` | Reads run snapshots for the exporter |
| `infrastructure/telemetry/structured_logger.py` | JSON logger with PHI scrubbing (`PHI_FIELDS = {phone, first_name, last_name, fullname, phone_number}`) |
| `infrastructure/telemetry/metrics_publisher.py` | High-level metric publishing helpers |

**Layer build:** `infra/lib/utils/shared-layer.ts` builds the layer asset from `services/shared/python/`. Bundled with every api-* function during `cdk deploy`.

---

## 2. Per-Component Dependency Table

### Backend (Python 3.12)

| Component | boto3 | redis | pydantic | StructuredLogger | Notes |
|---|---|---|---|---|---|
| api-plans | ≥1.34 | ≥5.0 | n/a | yes | Core orchestrator |
| api-campaigns | ≥1.34 | n/a | n/a | yes | CRUD only |
| api-metrics | ≥1.34 | n/a | n/a | yes | DDB + CW reads |
| api-profiles | ≥1.34 | n/a | n/a | yes | Customer Profiles |
| api-segments | ≥1.34 | ≥5.0 | n/a | yes | Reads Redis for estimates |
| shared (`vip_shared`) | ≥1.34 | ≥5.0 | n/a | self | Layer root |

All api-* services declare their own `requirements.txt` for dev/test (`pytest`, `moto`); production dependencies are pulled in via the layer.

### Infrastructure (`infra/`)

TypeScript / Node 20 + AWS CDK v2.

| Package | Version | Purpose |
|---|---|---|
| `aws-cdk-lib` | ^2.x | CDK constructs |
| `constructs` | ^10.x | CDK base |
| `typescript` | ^5.x | Build |
| `@types/node` | ^20.x | Types |

### Frontend (`frontend/`)

| Package | Version | Purpose |
|---|---|---|
| `react` | ^18.3 | UI |
| `react-dom` | ^18.3 | DOM renderer |
| `vite` | ^5.4 | Dev server + bundler |
| `typescript` | ^5 | Types |
| `@tanstack/react-query` | ^5 | Server state |
| `react-router-dom` | ^6 | Routing |
| `tailwindcss` | ^3 | CSS |
| `@radix-ui/react-*` | latest | shadcn/ui primitives (dialog, dropdown, popover, tooltip, etc.) |
| `lucide-react` | latest | Icons |
| `clsx` + `tailwind-merge` | latest | Class composition |
| `zod` | ^3 | API response validation |

No frontend tests today (TD-003); no ESLint config today (TD-004).

---

## 3. Build & Packaging

### api-plans `deploy.sh`

The script uses **Python's `zipfile` module** to build the deployment artifact rather than the system `zip` binary (the latter is broken on the dev host — see TD-014). Conceptually:

```
1. Run unit tests       (python -m pytest -q)
2. Build zip in /tmp    (python -c "import zipfile; …")
3. Update Lambda code   (aws lambda update-function-code --function-name vip-admin-ui-api-plans --zip-file fileb://… --profile production)
4. Wait Active          (aws lambda wait function-updated --function-name vip-admin-ui-api-plans --profile production)
```

The shared layer is **separately** published. To roll a layer update:

```bash
cd services/shared
zip -r layer.zip python/
aws lambda publish-layer-version \
  --layer-name vip-shared \
  --zip-file fileb://layer.zip \
  --compatible-runtimes python3.12 \
  --profile production
# Then update each api-* function:
aws lambda update-function-configuration \
  --function-name vip-admin-ui-api-plans \
  --layers <new-layer-arn> \
  --profile production
```

### Frontend

```bash
cd frontend
npm ci
npm run build          # vite build → dist/
aws s3 sync dist/ s3://vip-admin-ui-assets-165505826690/ --delete --profile production
aws cloudfront create-invalidation --distribution-id E3QCDJPG0LCO7E --paths "/*" --profile production
```

### Infrastructure

```bash
cd infra
npm ci
npx tsc -p .            # produces .js/.d.ts (TD-010 — keep these out of git)
AWS_PROFILE=production npx cdk synth
AWS_PROFILE=production npx cdk diff
AWS_PROFILE=production npx cdk deploy ApiPlansStack
```

---

## 4. Local Development Setup

Reproducible from a clean WSL Ubuntu / macOS host:

```bash
# 1. Clone
git clone <repo> vip-connect-external-campaigns
cd vip-connect-external-campaigns

# 2. Python tooling
python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install boto3 redis pytest moto

# 3. Per-service deps
for svc in api-plans api-campaigns api-metrics api-profiles api-segments shared; do
  [ -f services/$svc/requirements.txt ] && pip install -r services/$svc/requirements.txt
  [ -f services/$svc/requirements-dev.txt ] && pip install -r services/$svc/requirements-dev.txt
done

# 4. Run tests
cd services/api-plans && python -m pytest -q && cd ../..
cd services/shared && python -m pytest -q && cd ../..

# 5. Frontend
cd frontend
npm ci
npm run dev              # localhost:5173 — points at production API via .env.local

# 6. Infra
cd ../infra
npm ci
npx tsc -p . --noEmit    # type-check only
```

### Recommended .env.local for frontend

```
VITE_API_BASE_URL=https://<api-gateway-or-cloudfront>/api
VITE_COGNITO_USER_POOL_ID=us-east-1_MeEkWO4P4
VITE_COGNITO_CLIENT_ID=<app-client-id>
```

### AWS profile

```
[profile production]
sso_session = vip
sso_account_id = 165505826690
sso_role_name = DeveloperAccess
region = us-east-1
```

Always log in before deploys:
```bash
aws sso login --profile production
```

---

## 5. Test Inventory (262 total)

| Suite | Test count | Notes |
|---|---|---|
| `services/api-plans` | 150 | Executor + store + builders + handlers |
| `services/api-campaigns` | 16 | CRUD layer |
| `services/api-metrics` | 10 | Aggregation paths |
| `services/api-segments` | 23 | Segment estimate + reconcile |
| `services/api-profiles` | 9 | Customer Profiles wrappers |
| `services/shared` | 54 | Filter evaluator, schedule evaluator, audit, logger |
| **Total** | **262** | All boto3 stubbed; no AWS calls |

Run everything:

```bash
for d in services/api-plans services/api-campaigns services/api-metrics services/api-profiles services/api-segments services/shared; do
  (cd "$d" && python -m pytest -q) || exit 1
done
```
