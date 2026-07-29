<!-- Diátaxis: reference. Grow this as you document. Format: **Term** — plain-English meaning · where it appears. -->

# Glossary — Connect application

**Contact flow** — the drag-and-drop logic in Amazon Connect that runs a call (routing, prompts, whisper,
voicemail). Built by CloudHesive; the thing we're documenting.

**Flow module** — a small, reusable flow composed into larger flows via "Transfer to flow".

**Queue** — where a contact waits for an agent with the right skill. · Reached from a flow via "Transfer to queue".

**Routing profile** — a set of queues (with priority/delay) assigned to a group of agents; defines which
contacts an agent can receive.

**Hours of operation** — the schedule a flow checks (Check hours of operation) before routing to a queue.

**Quick connect** — a saved transfer target (agent, queue, or external number in E.164).

**Contact attribute** — a key/value set/read within a flow (camelCase). May be sensitive → logging disabled.

**AMD** — Answer Machine Detection; a dialer setting on a campaign.

**Campaign (V2)** — an Amazon Connect Outbound Campaign our webapp creates; its `source` is a Customer
Profiles segment; it enters a contact flow to place the call.

**Customer Profiles** — Amazon Connect's profile/segment store; our external `connectcampaignRedisSub`
writes leads into it; segments are built from it.

**LocationBasedVoicemail-v2** — the Connect data table mapping `(campaignType, location, groups) → greeting`.

**Import/Export** — Connect's mechanism to back up / version a flow as JSON (our docs-as-code ground truth).

**CloudHesive** — the external partner that built the Connect application (contact flows, queues, voicemail).

<!-- add: any acronym or term a newcomer wouldn't know, as you encounter it -->
