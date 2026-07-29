# Connect application documentation

Source-of-truth docs for the **Amazon Connect / CloudHesive layer** — the contact flows, queues,
routing profiles, hours of operation, and the `LocationBasedVoicemail-v2` data table that actually run
each call. This is the layer that had no documentation; these files fill that gap.

> **Scope:** our webapp *decides who to call* (already documented in the repo `docs/` suite + the Connect
> deep-dive). This folder documents the Connect application that *runs the call*.

## How to use this folder

1. **Read first:** the Connect deep-dive (onboarding doc 07) and the Connect Documentation Kit (doc 15).
2. **Export the flow's JSON** from the Connect flow designer (Import/Export) — that JSON is your ground
   truth. Commit it next to the `.md` (e.g. `flows/<name>.json`). Do **not** rely on screenshots.
3. **Copy a template** and fill it in:
   - `_TEMPLATE-flow.md` → one `flow-<name>.md` per contact flow
   - `_TEMPLATE-runbook.md` → one `runbook-<symptom>.md` per common failure
4. Fill the standing references directly: `data-table-locationbasedvoicemail-v2.md`,
   `queues-and-routing.md`, `connect-architecture.md`, `glossary.md`.
5. Open a PR; docs change in the **same PR** as any flow change.

## Standards (what "good" looks like)

- **Diátaxis** — keep the four doc types separate: *reference* (facts), *explanation* (why),
  *how-to* (runbooks), *tutorial* (onboarding). Don't mix them in one page.
- **Modular** — one doc per flow / flow-module; compose, don't write a monolith.
- **Every error branch** — capture where each error/no-match branch goes. No infinite loops; always a
  documented path to an agent, bot, or transfer.
- **Attributes** — list the contact attributes each flow sets/reads (camelCase); flag sensitive ones and
  note where `Set logging behavior` disables logging.
- **Path-to-service** — document Check hours-of-operation / Check staffing / Check queue-status logic
  (holidays, business hours, emergency flag, callbacks).
- **Docs-as-code** — Markdown + diagrams-as-code (Mermaid / draw.io) committed here; version with the flow JSON.
- **No PHI, no real phone numbers, no secrets** — ever, including in diagrams.

## Definition of Done (any doc)

- [ ] Correct Diátaxis type (reference / explanation / how-to — not mixed)
- [ ] All error / no-match branches captured; no infinite loop; path-to-service confirmed
- [ ] Attributes listed (camelCase); sensitive ones flagged
- [ ] External calls (Lambda / data table) documented
- [ ] Diagram included and committed as code
- [ ] No PHI, no real phone numbers, no secrets
- [ ] Open questions logged; reviewed by a human

## Milestones (Objective C)

> **Sequencing decision:** start with the **Architecture HLD (discovery)** — map the whole Connect
> application first. Its output (the unknowns register + prioritized flow backlog) scopes everything after.
> *Then* drill into individual flows.

| # | Deliverable | File(s) |
|---|-------------|---------|
| **M1** | **Architecture HLD (discovery)** — inventory + C4 L1/L2 + unknowns register + prioritized flow backlog | `connect-architecture.md` |
| M2 | Document the FIRST contact flow (the top of the M1 backlog) | `flow-<name>.md` + diagram |
| M3 | Data-table reference | `data-table-locationbasedvoicemail-v2.md` |
| M4 | Queues / routing profiles / hours | `queues-and-routing.md` |
| M5 | Remaining core contact flows | `flow-*.md` |
| M6 | One runbook for a common failure | `runbook-<symptom>.md` |

## Files in this folder

```
docs/connect/
├── README.md                                  ← you are here
├── _TEMPLATE-flow.md                           ← copy per contact flow
├── _TEMPLATE-runbook.md                        ← copy per failure symptom
├── data-table-locationbasedvoicemail-v2.md     ← fill directly
├── queues-and-routing.md                       ← fill directly
├── connect-architecture.md                     ← fill directly
├── glossary.md                                 ← grow as you go
└── flows/                                       ← exported flow JSON (ground truth)
```
