<!--
TEMPLATE — Runbook (Diátaxis: how-to). Copy to runbook-<symptom>.md, fill <...>, delete this comment.
A runbook is for an already-competent engineer under pressure: checks in order, fixes, rollback, escalation.
-->

# Runbook: <symptom, e.g. "calls not connecting to an agent">

| | |
|---|---|
| **When to use** | `<the observable trigger — what someone reports/sees>` |
| **Severity** | `<low / medium / high>` |
| **Owner** | `<team / role>` |

## Checks (in order)

1. <First thing to look at — e.g. CloudWatch flow logs for the contact ID / the flow's Set logging output>
2. <Queue status / Check staffing — are agents logged in? within hours of operation?>
3. <The external call — did the Lambda / data-table lookup fail? which branch fired?>
4. <...>

## Likely cause → fix

| Symptom / signal | Likely cause | Action |
|------------------|--------------|--------|
| `<...>` | `<...>` | `<...>` |
| `<...>` | `<...>` | `<...>` |

## Rollback

<How to revert the flow to the previous published version using Connect Import/Export. Reference the
committed `flows/<name>.json` for the last-known-good state.>

## Escalation

<Who to contact and when (e.g. after N minutes, or if PHI exposure is suspected → notify manager
immediately, legal notification clocks apply).>
