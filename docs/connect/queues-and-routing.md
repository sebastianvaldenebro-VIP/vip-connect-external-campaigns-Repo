<!-- Diátaxis: reference. One entry per queue / routing profile / hours-of-operation the documented
     flows depend on. Get the facts from the Connect console (read-only) + the campaign create params
     (queueId / contactFlowId in services/api-campaigns). No PHI, no real phone numbers. -->

# Queues, routing profiles & hours of operation

> Scope: only the queues/profiles/hours that the documented contact flows and campaigns actually use.

## Queues

### Queue: `<name>`
- **Purpose:** `<what contacts land here>`
- **Hours of operation:** `<name / reference below>`
- **Reached by:** `<which flows / campaigns route here (Transfer to queue)>`
- **Callback config:** `<queued callback offered? capacity threshold?>`
- **Notes:** `<...>`

<!-- repeat per queue -->

## Routing profiles

### Routing profile: `<name>`
- **Queues (with priority / delay):** `<queue A (p1, d0), queue B (p2, d15), ...>`
- **Skillset / who's assigned:** `<agent group>`
- **Overflow behavior:** `<...>`

<!-- repeat per routing profile -->

## Hours of operation

### Schedule: `<name>`
- **Timezone:** `<COT / UTC-5>` *(verify)*
- **Open hours:** `<Mon–Fri HH:MM–HH:MM, etc.>`
- **Holiday handling:** `<how holidays are treated — Lambda/DynamoDB lookup? custom message?>`
- **Emergency flag:** `<is there an emergency True/False mechanism? where is it set/read?>`

## How a campaign enters a flow

<Note the wiring from a Campaign V2 (our webapp) into Connect: `connectQueueId`, `connectContactFlowId`,
`connectSourcePhoneNumber` come per-request from `services/api-campaigns/src/builders.py`. Cross-reference
the Connect deep-dive (doc 07 §6).>

## Open questions

- <Which routing profiles map to which agent teams today?>
- <Is there an emergency-flag flow, and who can toggle it?>
