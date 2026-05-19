# API Map — UI endpoints → AWS APIs

Mapping of each endpoint the admin UI exposes to the underlying AWS API. The admin UI **never** calls AWS directly — everything goes through our Lambdas, which normalize payloads, apply audit logging, and handle errors.

Base URL: `https://<api-id>.execute-api.us-east-1.amazonaws.com/prod/`
Auth: `Authorization: Bearer <cognito-access-token>` (JWT Authorizer on API Gateway)

---

## 1. Plans domain (`/plans`, `/templates`)

Lambda: `api-plans`
IAM role needs: `dynamodb:GetItem`, `dynamodb:PutItem`, `dynamodb:UpdateItem`, `dynamodb:DeleteItem`, `dynamodb:Query` on `VipAdminPlans`; `connect-campaigns:*` and `profile:*` (via plans execution); `scheduler:CreateSchedule`, `scheduler:DeleteSchedule`; `kms:GenerateDataKey`, `kms:Decrypt`

| Method | Path | Purpose | Notes |
|---|---|---|---|
| `GET` | `/plans` | List all plans (templates excluded) | Returns status, last run, trigger info |
| `POST` | `/plans` | Create plan | Pass `_duplicateFromId` to clone |
| `GET` | `/plans/{id}` | Get plan definition | Includes `pendingWarmup`, `loop`, `isLocked` |
| `PUT` | `/plans/{id}` | Update plan definition | Rejected if plan is currently locked (running) |
| `DELETE` | `/plans/{id}` | Delete plan | Rejected if plan has an active run |
| `GET` | `/templates` | List plan templates | `isTemplate: true` items |
| `POST` | `/plans/from-template/{tid}` | Clone a template into a new plan | |
| `POST` | `/plans/{id}/runs` | Trigger a run | Body: `{ startBucketIndex?: number }` |
| `GET` | `/plans/{id}/runs` | List runs for a plan | Sorted descending by start time |
| `GET` | `/plans/{id}/runs/{runId}` | Get a specific run | Full `bucketStates` + `campaignStates` |
| `POST` | `/plans/{id}/runs/{runId}/abort` | Abort a running run | Stops all Connect campaigns; deletes tick schedules |

**Internal event actions** (not HTTP — fired by EventBridge):

| Action | Source | Purpose |
|---|---|---|
| `tick` | EventBridge Scheduler `rate(1 min)` per active bucket | Poll campaigns, dispatch ready ones, advance buckets |
| `scheduled_run` | EventBridge Scheduler (daily) | Start a time-triggered plan run |
| `chain_trigger` | Direct Lambda invoke | Start a downstream `on_plan_complete` plan |
| `prestart_check` | EventBridge rule `vip-plans-prestart-check` `rate(1 min)` | Warm plans whose `trigger.time` is 4–6 min away (COT) |

**Request body example — POST /plans:**

```json
{
  "name": "NY Morning Wave",
  "description": "Daily morning calls for NY leads",
  "trigger": { "type": "time", "time": "07:55" },
  "loop": { "endTime": "19:00" },
  "buckets": [
    {
      "id": "b1",
      "name": "NY Bucket 1",
      "run_mode": "time_based",
      "duration_minutes": 45,
      "cleanup": true,
      "prestart_next": true,
      "parallel": false,
      "campaignConfig": {
        "queueId": "fc5e3102-44f1-4986-baaa-055ee92e0a98",
        "sourcePhoneNumber": "+19734949660",
        "dialerType": "progressive",
        "contactFlowId": "3d24320b-c1e3-40f3-90a2-b6867ef70c85",
        "campaignFlowArn": "arn:aws:connect:us-east-1:165505826690:instance/.../contact-flow/..."
      },
      "campaigns": [
        { "id": "c1", "name": "NY New Lead", "states": ["NY"], "group": "New lead",
          "attempts": [1], "run_type": "full", "dependsOn": [] },
        { "id": "c2", "name": "NY Follow-up", "states": ["NY"], "group": "Follow-up",
          "attempts": [1], "run_type": "full", "dependsOn": ["c1"] }
      ]
    }
  ]
}
```

---

## 2. Segments domain (`/segments`)

Lambda: `api-segments`
IAM role needs: `profile:CreateSegmentDefinition`, `profile:ListSegmentDefinitions`, `profile:GetSegmentDefinition`, `profile:DeleteSegmentDefinition`, `profile:CreateSegmentEstimate`, `profile:GetSegmentEstimate`, `profile:CreateSegmentSnapshot`, `profile:GetSegmentSnapshot`, `profile:GetSegmentMembership`, `dynamodb:PutItem` (audit), `s3:GetObject` (snapshot reads)

| Method | Path | Purpose | AWS API called | Notes |
|---|---|---|---|---|
| `GET` | `/segments` | List all segments with latest count | `ListSegmentDefinitions` + parallel `GetSegmentEstimate` | Cacheable 60s |
| `POST` | `/segments` | Create segment with filters | `CreateSegmentDefinition` | Auto-triggers `CreateSegmentEstimate` |
| `GET` | `/segments/{id}` | Segment detail | `GetSegmentDefinition` | |
| `DELETE` | `/segments/{id}` | Delete segment | `DeleteSegmentDefinition` | Validates not in use by active campaign |
| `PUT` | `/segments/{id}` | Edit segment (delete + recreate) | `DeleteSegmentDefinition` + `CreateSegmentDefinition` | Segments are immutable; UI hides the delete+recreate |
| `POST` | `/segments/{id}/estimate` | Force recompute | `CreateSegmentEstimate` | Async — returns `estimateId` |
| `GET` | `/segments/{id}/estimate/{estimateId}` | Poll recompute | `GetSegmentEstimate` | Returns `status`, `estimate.totalCount` |
| `POST` | `/segments/{id}/snapshot` | Export members to S3 | `CreateSegmentSnapshot` | Async — returns `snapshotId` |
| `GET` | `/segments/{id}/snapshot/{snapshotId}` | Poll export | `GetSegmentSnapshot` | Returns S3 key; Lambda reads first 20 rows |
| `POST` | `/segments/{id}/members` | Batch membership check | `GetSegmentMembership` | For "is this profile in the segment?" |

**Request body example — POST /segments:**

```json
{
  "name": "NJ-New-Leads-1st-Attempt",
  "displayName": "NJ New Leads – 1st Attempt",
  "description": "Active New Leads in New Jersey, 1st attempt",
  "segmentGroups": {
    "Groups": [{
      "Dimensions": [{
        "ProfileAttributes": {
          "Attributes": {
            "available": {"DimensionType": "INCLUSIVE", "Values": ["True"]},
            "groups": {"DimensionType": "INCLUSIVE", "Values": ["New Lead / 1st Attempt"]},
            "location": {"DimensionType": "CONTAINS", "Values": ["NJ -"]}
          }
        }
      }],
      "SourceType": "ANY",
      "Type": "ANY"
    }],
    "Include": "ALL"
  }
}
```

Response includes `segmentDefinitionArn` — required for manual campaign creation.

---

## 3. Profiles domain (`/profiles`)

Lambda: `api-profiles`
IAM role needs: `profile:SearchProfiles`, `profile:BatchGetProfile`, `profile:ListProfileObjects`, `profile:GetCalculatedAttributeForProfile`, `profile:ListCalculatedAttributesForProfile`, `dynamodb:PutItem` (audit)

| Method | Path | Purpose | AWS API called |
|---|---|---|---|
| `GET` | `/profiles/search?key=phone&value=%2B12017801027` | Search by key+value | `SearchProfiles` |
| `POST` | `/profiles/batch` | Get multiple profiles | `BatchGetProfile` |
| `GET` | `/profiles/{profileId}` | Profile detail | `SearchProfiles` with `_profileId` key, or `BatchGetProfile([id])` |
| `GET` | `/profiles/{profileId}/objects` | Raw profile objects (history) | `ListProfileObjects` |
| `GET` | `/profiles/{profileId}/calculated-attributes` | Calculated attribute list | `ListCalculatedAttributesForProfile` |
| `GET` | `/profiles/{profileId}/calculated-attributes/{attrName}` | Single attribute value | `GetCalculatedAttributeForProfile` |

**Search keys supported** (keys of object type `leads-data-mapping`):

- `customerid` → `Attributes.ID`
- `_phone` → PhoneNumber (normalized)
- `_email` → EmailAddress
- `_fullName` → FirstName + LastName

---

## 4. Campaigns domain (`/campaigns`)

Lambda: `api-campaigns`
IAM role needs: `connect-campaigns:CreateCampaign`, `connect-campaigns:DescribeCampaign`, `connect-campaigns:ListCampaigns`, `connect-campaigns:DeleteCampaign`, `connect-campaigns:UpdateCampaignName`, `connect-campaigns:UpdateCampaignSource`, `connect-campaigns:UpdateCampaignSchedule`, `connect-campaigns:StartCampaign`, `connect-campaigns:StopCampaign`, `connect-campaigns:PauseCampaign`, `connect-campaigns:ResumeCampaign`, `connect-campaigns:GetCampaignState`, `connect:ListQueues`, `connect:ListContactFlows`, `connect:ListPhoneNumbersV2`, `dynamodb:PutItem` (audit)

All calls to Outbound Campaigns use the **V2 API** (`connectcampaignsv2` service endpoint).

| Method | Path | Purpose | AWS API called |
|---|---|---|---|
| `GET` | `/campaigns` | List campaigns | `ListCampaigns` (V2) |
| `POST` | `/campaigns` | Create campaign | `CreateCampaign` (V2) |
| `GET` | `/campaigns/{id}` | Detail | `DescribeCampaign` + `GetCampaignState` |
| `DELETE` | `/campaigns/{id}` | Delete (stops first if needed) | `StopCampaign` + `DeleteCampaign` |
| `PATCH` | `/campaigns/{id}` | Edit name/source/schedule | `UpdateCampaignName` / `UpdateCampaignSource` / `UpdateCampaignSchedule` selectively |
| `POST` | `/campaigns/{id}/start` | Start | `StartCampaign` |
| `POST` | `/campaigns/{id}/stop` | Stop | `StopCampaign` |
| `POST` | `/campaigns/{id}/pause` | Pause | `PauseCampaign` |
| `POST` | `/campaigns/{id}/resume` | Resume | `ResumeCampaign` |
| `GET` | `/campaigns/resources/queues` | Queues dropdown | `connect:ListQueues` filtered `STANDARD` |
| `GET` | `/campaigns/resources/contact-flows` | Outbound flows | `connect:ListContactFlows` filtered `*Campaign*` |
| `GET` | `/campaigns/resources/phone-numbers` | Claimable phone numbers | `connect:ListPhoneNumbersV2` |
| `GET` | `/campaigns/resources/campaign-flows` | Connect Campaign Flows (type CAMPAIGN) | `connect:ListContactFlows` filtered type CAMPAIGN |

---

## 5. Metrics domain (`/metrics`)

Lambda: `api-metrics`
IAM role needs: `cloudwatch:GetMetricStatistics`, `cloudwatch:GetMetricData`, `connect:GetMetricDataV2`, `connect:GetCurrentMetricData`, `dynamodb:PutItem` (audit)

| Method | Path | Purpose | AWS API called |
|---|---|---|---|
| `GET` | `/metrics/campaigns/{id}?period=24h` | Metrics for one campaign | `cloudwatch:GetMetricStatistics` on `AWS/Connect/Campaigns` |
| `GET` | `/metrics/campaigns?period=24h` | Dashboard summary all campaigns | Loop over campaigns + aggregate in Lambda |
| `GET` | `/metrics/queues/{queueId}?period=24h` | Queue metrics | `connect:GetMetricDataV2` |
| `GET` | `/metrics/current` | Real-time snapshot (agents, contacts in queue) | `connect:GetCurrentMetricData` |
| `GET` | `/metrics/dispositions?campaignId={id}&period=24h` | Disposition breakdown | `connect:SearchContacts` + group by `DisconnectReason` |

---

## 6. Audit domain (read-only)

Served by `api-metrics` under a sub-path.

| Method | Path | Purpose | Source |
|---|---|---|---|
| `GET` | `/audit?limit=50&cursor=xxx` | Recent actions | DynamoDB `AdminAuditLog` scan/query |
| `GET` | `/audit?entity=segment&id=xxx` | History of a specific entity | DynamoDB query by PK=entity_id |

**DynamoDB schema `AdminAuditLog`:**

```
PK: entity_id (string)       — e.g. "segment/<uuid>", "campaign/<uuid>", "plan/<uuid>"
SK: timestamp (ISO 8601)
attributes:
  actor_sub         — Cognito user sub
  actor_email       — convenience display
  action            — create | update | delete | start | stop | pause | resume | abort | estimate | …
  before            — JSON of entity before (null on create)
  after             — JSON of entity after (null on delete)
  ip_address        — from API GW context
  user_agent
  ttl               — Unix timestamp 6 years ahead
```

---

## 7. Error response envelope

All endpoints return errors in a consistent shape:

```json
{
  "error": {
    "code": "SEGMENT_IN_USE",
    "message": "Cannot delete segment: in use by 2 active campaigns",
    "details": {
      "campaigns": ["abc-123", "def-456"]
    },
    "requestId": "xyz-789"
  }
}
```

Status codes:

- `400` — validation errors (malformed JSON, missing fields, cycle detected)
- `401` — missing or invalid JWT
- `403` — user lacks permission (future multi-role)
- `404` — resource not found
- `409` — conflict (e.g. delete while plan is locked/running)
- `429` — throttled by AWS
- `5xx` — unexpected; logged with full stack trace (no PHI)

---

## 8. Rate limiting & caching

- API Gateway usage plan: 100 req/s burst, 50 req/s sustained per IP
- Lambda `reservedConcurrentExecutions` = 10 per Lambda (5 reserved for api-plans, 1 for feeder)
- TanStack Query stale-while-revalidate: 60s for list endpoints, 30s for detail endpoints
- `CreateSegmentEstimate` / `CreateSegmentSnapshot` responses cached by `(segmentId, estimateId)` — immutable once SUCCEEDED
