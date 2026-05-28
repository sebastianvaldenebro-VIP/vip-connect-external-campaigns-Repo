# VIP Connect External Campaigns — Non-Technical Guide

A plain-English overview of what the system does, how it works, and why it is safe.

---

## What is this system?

This system automatically schedules and manages outbound phone calls to VIP Medical Group patients for appointment reminders, follow-ups, and notifications. Instead of a human dialing every number, it tells **Amazon Connect** (a call-centre platform) who to call, when, and from which queue — and it watches each call campaign in real time, advancing to the next group of patients as soon as the previous one finishes.

---

## How does it work?

1. **Call-centre operators** open the Admin UI (a website) and log in with their company email + a one-time code from their phone (multi-factor authentication).
2. They build a **Plan** — a list of phone-call campaigns, grouped in ordered **buckets**, with rules for which patients to call (by region) and what stage of contact (1st attempt, 2nd attempt, cancellation follow-up, etc.).
3. They pick **when** the plan should run — a specific time of day, after another plan finishes, or manually on demand.
4. The system takes over:
   - Looks up the matching patient phone numbers (kept in a secure database).
   - Creates each campaign inside Amazon Connect.
   - Starts dialing.
   - Watches every minute, advancing buckets, restarting campaigns that finish early, and moving from one stage to the next.
5. Every plan **stops automatically at 7:00 PM Colombia time (COT)**. No call is ever placed after that.

The operator monitors progress through a dashboard showing each bucket and campaign in real time.

---

## Key business rules

| Rule | Description |
|---|---|
| **7 PM hard stop** | No outbound call is initiated after 19:00 COT. Campaigns still running at that time are stopped. |
| **Geographic routing** | Campaigns target specific regions (NY, Long Island, NJ, CT, MD, TX, Northern California, Southern California). Each region uses its own contact flow and queue. |
| **Lead list refresh** | Phone numbers come from an external pipeline that refreshes throughout the day. The system retries when a freshly created segment is still empty. |
| **One run per plan at a time** | The same plan cannot run twice simultaneously. The operator must abort the active run before triggering a new one. |
| **Pre-warming** | Amazon Connect requires campaigns to be scheduled at least 6 minutes in advance. The system pre-creates the next bucket 5 minutes early so dialing starts on time. |
| **Working hours window** | Plans can specify allowed days and hours (e.g. weekdays 9 AM – 5 PM only). The system enforces these in addition to the 7 PM cutoff. |

---

## Privacy & HIPAA safety

This is a HIPAA-regulated platform. Patient health information (PHI) is treated as the most sensitive data in the system.

- **Phone numbers are never written to logs.** Every log record about a patient uses a one-way hash so the original number cannot be recovered.
- **Names are never written to logs or to the plan/run database.** Names live only inside Amazon Customer Profiles, which is a HIPAA-eligible AWS service covered by Medwork's Business Associate Agreement (BAA).
- **Encryption everywhere.** Patient data is encrypted in transit (TLS 1.2+) and at rest (AWS KMS customer-managed keys).
- **Multi-factor authentication** is required for every operator login.
- **Sessions time out after 15 minutes of inactivity.**
- **Every action is audited.** Who triggered a run, who aborted it, when, from which IP — all recorded in an immutable audit table with 6+ year retention.

---

## Frequently Asked Questions

### 1. Can a campaign accidentally call a patient at midnight?

No. The system enforces a hard 7 PM Colombia time cutoff. Any campaign still running at that time is forcibly stopped.

### 2. What if Amazon Connect is down?

The Admin UI will keep working for plan editing, but new campaigns won't be created until Connect recovers. Existing campaigns will be polled and updated when service resumes.

### 3. Can two operators trigger the same plan at the same time?

No. The system uses a database lock to ensure only one run per plan exists at any moment. The second operator gets a clear error message.

### 4. What if a patient's phone number was entered incorrectly?

The phone number lives in the patient profile system (Amazon Customer Profiles). If it is wrong, the call will fail at the carrier level and Connect will mark it accordingly. Correcting the number requires updating the patient record upstream — not in this system.

### 5. Do operators see patient phone numbers in the UI?

Yes, but only inside the regulated parts of the UI tied to the user's role. The plan and run records themselves never store phone numbers — they store references and counts.

### 6. How do we know the system is healthy?

The platform emits operational alerts (email/SMS via SNS) when:
- A run has been "running" for more than 4 hours without progress.
- Pre-warming failed.
- A campaign couldn't be created at all.

Engineers also have CloudWatch dashboards.

### 7. Why does the platform sometimes show a "skipped_empty" campaign?

It means the system tried to dial a particular state + group combination but the lead list was empty at that moment. The system retries automatically up to five times, then records `skipped_empty` and moves on so the rest of the plan continues.

### 8. Can a plan be edited while it's running?

Yes. The Admin UI lets operators edit a live plan; the changes apply to **future buckets** of the active run (buckets already running or completed are not retroactively changed). This is why each run stores a `planSnapshot` — the version of the plan that was active when the run started.

---

## Service Level Expectations

| Metric | Target |
|---|---|
| Availability | 99.5% during operating hours (08:00–19:00 COT) |
| Plan trigger latency | < 60 seconds from scheduled time to first dial attempt |
| Tick frequency | 1 minute (state polled every minute per running bucket) |
| Run start delay (with pre-warm) | < 1 minute |
| Run start delay (without pre-warm, cold start) | ~6 minutes |
| Stuck-run alert | Within 4 hours |
| Time to acknowledge an operational alert | < 30 minutes during business hours |

---

## Who to contact

- **Operational issues during business hours:** Call-centre supervisor → on-call platform engineer.
- **Off-hours / after 7 PM COT:** Email `ops@medwork.io` and escalate the next business day.
- **HIPAA / compliance concerns:** Compliance officer at Medwork.
- **Code / infrastructure changes:** Platform engineering team (see internal handbook).
