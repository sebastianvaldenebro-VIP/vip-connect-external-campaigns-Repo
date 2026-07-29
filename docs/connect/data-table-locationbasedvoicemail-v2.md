<!-- Diátaxis: reference. Fill the <...>; verify against the flow(s) that read this table and the
     scripts in services/data-table-tools/. No PHI. -->

# Data table: `LocationBasedVoicemail-v2`

**Purpose:** maps *campaign type* + *clinic location* + *outcome group* → the **voicemail greeting** a
contact flow plays. Lets one flow serve different greetings per campaign / location / outcome without
branching logic in the flow itself.

| | |
|---|---|
| **Key** | `(campaignType, location, groups)` |
| **Value** | `greeting` |
| **Rows** | ~210 |
| **Read by** | `<which contact flow(s) / block(s)>` *(verify)* |
| **Maintained by** | ops scripts in `services/data-table-tools/` (`dt_create_v2.py`, `dt_export_import.py`, `dt_upsert_noshow.py`) |

## Columns

| Column | Type | Primary key? | Example | Notes |
|--------|------|:------------:|---------|-------|
| `campaignType` | text | yes | `<...>` | part of the lookup key |
| `location` | text | yes | `<...>` | clinic; part of the lookup key |
| `groups` | text | yes | `No Show` | outcome group; part of the lookup key |
| `greeting` | text | — | `<prompt ref>` | the value the flow uses |

## Lookup semantics

- **Match:** `<exact match on all three key parts? case sensitivity?>` *(verify)*
- **No match / fallback:** `<what does the flow do when no row matches? default greeting? which block?>` *(verify)*
- **Empty / invalid rows:** rows with empty `groups` (or the `"(empty text) (Default)"` UI artifact) are
  skipped by the loader — note if any flow still depends on them. *(verify)*

## How it's populated

<Which script creates/updates rows, on what trigger (manual? none — API-only?), and how a new
campaign/location/group gets a greeting added.>

## Open questions

- <Which exact flows read this table, and at which block?>
- <Is there a documented default when the key isn't found?>
