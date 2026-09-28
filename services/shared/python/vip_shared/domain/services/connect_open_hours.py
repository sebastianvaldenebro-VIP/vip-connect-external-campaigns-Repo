"""Connect Campaigns V2 openHours builder for TCPA-window voice delivery.

Split out of quiet_hours.py (which holds the per-recipient, phonenumbers-
dependent SMS quiet-hours gate) so that services/api-campaigns and
services/api-plans — which only need `connect_open_hours()` — never pull in
the `phonenumbers` dependency at import time. Those two services' Lambda
layers are built from the plain `requirements.txt` (no `phonenumbers`); only
api-sms's layer is built from `requirements-sms.txt`, which includes it. See
`infra/lib/utils/shared-layer.ts`'s docstring for the full rationale. This
module must import NOTHING beyond the Python standard library.
"""

from __future__ import annotations

from typing import Any

# ── Connect Campaigns V2 openHours builder ────────────────────────────────────
#
# Used by services/api-campaigns and services/api-plans builders.py to build
# communicationTimeConfig.telephony.openHours for the voice delivery types
# ("campaign"/"journey"). This is a DIFFERENT consumer shape than the
# per-recipient gate in quiet_hours.py (Connect wants "T"-prefixed ISO local
# times keyed by day NAME; is_within_quiet_hours wants "HH:MM" strings and
# weekday INTs), so these are separate public members rather than
# reusing/reshaping quiet_hours.py's constants — same TCPA policy, two shapes.
#
# TCPA quiet hours are a property of the *recipient's* local time, not of a
# timezone the operator picks once per campaign. Connect Campaigns V2 resolves
# the recipient's timezone itself; we only declare the window.
#
# AREA_CODE over ZIP_CODE: every phone number has an area code, whereas
# ZIP_CODE needs a populated Customer Profiles address we have not confirmed.
#
# The window is the FULL statutory TCPA span on the hours axis (08:00-21:00
# recipient-local) and stricter than statute on the day axis: Monday-Saturday
# only. TCPA does not exempt Sunday; excluding it is a VIP business choice.
#
# The "T" prefix is mandatory — Iso8601Time's pattern is T\d{2}:\d{2}. Note
# that botocore does NOT enforce string patterns: a bare "08:00" validates
# clean locally and is sent to the service. The unit tests are the only guard.
#
# SUNDAY is excluded by OMITTING the key from dailyHours. An empty list
# ("SUNDAY": []) is equally valid per the model but its semantics are
# undocumented; see the shape table in this task.
CONNECT_QUIET_HOURS_START = "T08:00"
CONNECT_QUIET_HOURS_END = "T21:00"
CONNECT_CONTACT_DAYS = (
    "MONDAY",
    "TUESDAY",
    "WEDNESDAY",
    "THURSDAY",
    "FRIDAY",
    "SATURDAY",
)


def connect_open_hours() -> dict[str, Any]:
    """A TimeWindow gating one channel to the recipient-local quiet-hours window.

    Used by build_create_campaign_params (api-campaigns) and
    build_campaign_params (api-plans) to populate
    communicationTimeConfig.telephony in Connect Campaigns V2's CreateCampaign
    params for the "campaign"/"journey" voice delivery types. The bulk-SMS
    delivery type ("sms") never touches Connect Campaigns and uses
    is_within_quiet_hours() (quiet_hours.py) instead.
    """
    return {
        "openHours": {
            "dailyHours": {
                day: [
                    {
                        "startTime": CONNECT_QUIET_HOURS_START,
                        "endTime": CONNECT_QUIET_HOURS_END,
                    }
                ]
                for day in CONNECT_CONTACT_DAYS
            }
        }
    }
