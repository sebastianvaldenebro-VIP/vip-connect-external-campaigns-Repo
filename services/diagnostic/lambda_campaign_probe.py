"""
Probe diagnostic for Outbound Campaigns PutDialRequestBatch/PutOutboundRequestBatch
failures. Tests variations to isolate which field triggers InvalidInput.

Deploy as Lambda in same account as Connect. Test event:
{
  "campaign_id_v1": "<v1-campaign-id>",
  "campaign_id_v2": "<v2-campaign-id>",
  "test_phones": {
    "us_mainland": "+19734949660",
    "us_territory": "+17878086669",
    "known_profile_phone": "+12017801027"
  }
}
"""

import uuid
from datetime import datetime, timedelta, timezone

import boto3

v1 = boto3.client("connectcampaigns")
v2 = boto3.client("connectcampaignsv2")


def iso_future(minutes: int = 30) -> str:
    return (datetime.now(tz=timezone.utc) + timedelta(minutes=minutes)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def test_v1(campaign_id: str, phone: str, label: str) -> dict:
    """Test V1 PutDialRequestBatch."""
    try:
        resp = v1.put_dial_request_batch(
            id=campaign_id,
            dialRequests=[
                {
                    "clientToken": str(uuid.uuid4()),
                    "phoneNumber": phone,
                    "expirationTime": iso_future(30),
                    "attributes": {"probe": label, "lead_id": f"probe-{label}"},
                }
            ],
        )
        return {
            "test": f"v1_{label}",
            "phone": phone,
            "successful": len(resp.get("successfulRequests", [])),
            "failed": len(resp.get("failedRequests", [])),
            "failure_codes": [
                f.get("failureCode") for f in resp.get("failedRequests", [])
            ],
        }
    except Exception as e:
        return {"test": f"v1_{label}", "phone": phone, "exception": str(e)}


def test_v2(campaign_id: str, phone: str, label: str) -> dict:
    """Test V2 PutOutboundRequestBatch."""
    try:
        resp = v2.put_outbound_request_batch(
            id=campaign_id,
            outboundRequests=[
                {
                    "clientToken": str(uuid.uuid4()),
                    "expirationTime": iso_future(30),
                    "channelSubtypeParameters": {
                        "telephony": {
                            "destinationPhoneNumber": phone,
                            "attributes": {"probe": label, "lead_id": f"probe-{label}"},
                        }
                    },
                }
            ],
        )
        return {
            "test": f"v2_{label}",
            "phone": phone,
            "successful": len(resp.get("successfulRequests", [])),
            "failed": len(resp.get("failedRequests", [])),
            "failure_codes": [
                f.get("failureCode") for f in resp.get("failedRequests", [])
            ],
        }
    except Exception as e:
        return {"test": f"v2_{label}", "phone": phone, "exception": str(e)}


def describe_campaign(campaign_id: str) -> dict:
    try:
        return v2.describe_campaign(id=campaign_id).get("campaign", {})
    except Exception as e:
        return {"error": str(e)}


def lambda_handler(event, context):
    v1_id = event.get("campaign_id_v1")
    v2_id = event.get("campaign_id_v2")
    phones = event.get("test_phones", {})

    results = {
        "campaigns": {},
        "tests": [],
    }

    if v2_id:
        results["campaigns"]["v2"] = describe_campaign(v2_id)

    for label, phone in phones.items():
        if v1_id:
            results["tests"].append(test_v1(v1_id, phone, label))
        if v2_id:
            results["tests"].append(test_v2(v2_id, phone, label))

    # Matrix: which combinations worked?
    summary = {
        "v1_successes": [
            t["phone"]
            for t in results["tests"]
            if t["test"].startswith("v1_") and t.get("successful", 0) > 0
        ],
        "v2_successes": [
            t["phone"]
            for t in results["tests"]
            if t["test"].startswith("v2_") and t.get("successful", 0) > 0
        ],
        "v1_failures": [
            t
            for t in results["tests"]
            if t["test"].startswith("v1_") and t.get("failed", 0) > 0
        ],
        "v2_failures": [
            t
            for t in results["tests"]
            if t["test"].startswith("v2_") and t.get("failed", 0) > 0
        ],
    }
    results["summary"] = summary

    return results
