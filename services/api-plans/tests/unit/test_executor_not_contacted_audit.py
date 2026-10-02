"""Tests for _audit_not_contacted_leads: a read-only bookkeeping step that,
right before a campaign's Customer Profiles segment is deleted at cleanup
time, diffs the segment's targeted phone numbers against who Connect
actually dialed for that specific connectCampaignId (via describe-contact's
Campaign.CampaignId field, which disambiguates shared source numbers) and
writes one audit row per never-contacted number. No action is taken on the
leads themselves.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

sys.modules.setdefault("vip_shared", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure.persistence", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure.persistence.audit", MagicMock())
sys.modules.setdefault("vip_shared.infrastructure.persistence.customer_profiles_client", MagicMock())

import executor  # noqa: E402


def _segment_definition(numbers):
    return {
        "SegmentGroups": {
            "Groups": [
                {
                    "Dimensions": [
                        {"ProfileAttributes": {"PhoneNumber": {"Values": numbers}}}
                    ]
                }
            ]
        }
    }


def _run(plan_id="p1", run_id="r1"):
    return {"planId": plan_id, "runId": run_id}


def _cs(**overrides):
    cs = {
        "name": "NY-NL_1",
        "campaignId": "campaign-def-1",
        "connectCampaignId": "cc-1",
        "segmentName": "seg-1",
        "startedAt": "2026-09-29T14:00:00+00:00",
    }
    cs.update(overrides)
    return cs


def _bucket(queue_id="queue-1"):
    return {
        "campaignConfig": {"queueId": queue_id} if queue_id else {},
        "campaigns": [{"id": "campaign-def-1", "campaignConfig": {}}],
    }


def test_noop_when_table_env_var_not_set():
    with patch.object(executor, "_NOT_CONTACTED_AUDIT_TABLE", ""):
        with patch("vip_shared.infrastructure.persistence.customer_profiles_client.build_from_env") as build_cp:
            executor._audit_not_contacted_leads(_run(), _cs(), _bucket())
            build_cp.assert_not_called()


def test_noop_when_missing_connect_campaign_id_or_segment_name():
    with patch.object(executor, "_NOT_CONTACTED_AUDIT_TABLE", "AuditTable"):
        with patch("vip_shared.infrastructure.persistence.customer_profiles_client.build_from_env") as build_cp:
            executor._audit_not_contacted_leads(_run(), _cs(connectCampaignId=None), _bucket())
            executor._audit_not_contacted_leads(_run(), _cs(segmentName=None), _bucket())
            build_cp.assert_not_called()


def test_writes_one_row_per_never_contacted_number():
    targeted = ["+12025550111", "+12025550122", "+12025550133"]
    dialed_contact_ids = ["contact-a", "contact-b"]

    mock_cp_client = MagicMock()
    mock_cp_client.get_segment_definition.return_value = _segment_definition(targeted)

    mock_connect = MagicMock()
    mock_connect.get_paginator.return_value.paginate.return_value = [
        {"Contacts": [{"Id": cid} for cid in dialed_contact_ids]}
    ]
    # contact-a belongs to this campaign and dialed +12025550111;
    # contact-b belongs to a DIFFERENT campaign sharing the same source
    # number and must not count as "dialed" for this one.
    mock_connect.describe_contact.side_effect = lambda InstanceId, ContactId: {
        "contact-a": {
            "Contact": {
                "Campaign": {"CampaignId": "cc-1"},
                "CustomerEndpoint": {"Address": "+12025550111"},
            }
        },
        "contact-b": {
            "Contact": {
                "Campaign": {"CampaignId": "cc-OTHER"},
                "CustomerEndpoint": {"Address": "+12025550122"},
            }
        },
    }[ContactId]

    with patch.object(executor, "_NOT_CONTACTED_AUDIT_TABLE", "AuditTable"), \
         patch.object(executor, "CONNECT_INSTANCE_ID", "instance-1"), \
         patch("vip_shared.infrastructure.persistence.customer_profiles_client.build_from_env", return_value=mock_cp_client), \
         patch.object(executor, "_get_connect_client", return_value=mock_connect), \
         patch.object(executor, "_get_ddb_client") as get_ddb:
        mock_ddb = MagicMock()
        get_ddb.return_value = mock_ddb

        executor._audit_not_contacted_leads(_run(), _cs(), _bucket(queue_id="queue-1"))

        search_kwargs = mock_connect.get_paginator.return_value.paginate.call_args.kwargs
        assert search_kwargs["SearchCriteria"] == {"QueueIds": ["queue-1"]}

        written_numbers = {
            call.kwargs["Item"]["phoneNumber"]["S"] for call in mock_ddb.put_item.call_args_list
        }
        assert written_numbers == {"+12025550122", "+12025550133"}
        for call in mock_ddb.put_item.call_args_list:
            assert call.kwargs["TableName"] == "AuditTable"
            assert call.kwargs["Item"]["campaignId"]["S"] == "cc-1"
            assert call.kwargs["Item"]["runId"]["S"] == "r1"


def test_noop_when_queue_id_cannot_be_resolved():
    """No bucket, or a bucket with no queueId anywhere, must short-circuit
    before ever calling Connect — an unfiltered search_contacts scan of the
    whole instance is never an acceptable fallback."""
    mock_cp_client = MagicMock()
    mock_cp_client.get_segment_definition.return_value = _segment_definition(["+12025550111"])
    mock_connect = MagicMock()

    with patch.object(executor, "_NOT_CONTACTED_AUDIT_TABLE", "AuditTable"), \
         patch.object(executor, "CONNECT_INSTANCE_ID", "instance-1"), \
         patch("vip_shared.infrastructure.persistence.customer_profiles_client.build_from_env", return_value=mock_cp_client), \
         patch.object(executor, "_get_connect_client", return_value=mock_connect), \
         patch.object(executor, "_get_ddb_client") as get_ddb:
        mock_ddb = MagicMock()
        get_ddb.return_value = mock_ddb

        executor._audit_not_contacted_leads(_run(), _cs(), bucket=None)
        executor._audit_not_contacted_leads(_run(), _cs(), _bucket(queue_id=""))

        mock_connect.get_paginator.assert_not_called()
        mock_ddb.put_item.assert_not_called()


def test_empty_segment_writes_nothing():
    mock_cp_client = MagicMock()
    mock_cp_client.get_segment_definition.return_value = _segment_definition([])
    mock_connect = MagicMock()
    mock_connect.get_paginator.return_value.paginate.return_value = [{"Contacts": []}]

    with patch.object(executor, "_NOT_CONTACTED_AUDIT_TABLE", "AuditTable"), \
         patch.object(executor, "CONNECT_INSTANCE_ID", "instance-1"), \
         patch("vip_shared.infrastructure.persistence.customer_profiles_client.build_from_env", return_value=mock_cp_client), \
         patch.object(executor, "_get_connect_client", return_value=mock_connect), \
         patch.object(executor, "_get_ddb_client") as get_ddb:
        mock_ddb = MagicMock()
        get_ddb.return_value = mock_ddb

        executor._audit_not_contacted_leads(_run(), _cs(), _bucket())

        mock_ddb.put_item.assert_not_called()


def test_all_contacted_writes_nothing():
    targeted = ["+12025550111"]
    mock_cp_client = MagicMock()
    mock_cp_client.get_segment_definition.return_value = _segment_definition(targeted)
    mock_connect = MagicMock()
    mock_connect.get_paginator.return_value.paginate.return_value = [{"Contacts": [{"Id": "contact-a"}]}]
    mock_connect.describe_contact.return_value = {
        "Contact": {
            "Campaign": {"CampaignId": "cc-1"},
            "CustomerEndpoint": {"Address": "+12025550111"},
        }
    }

    with patch.object(executor, "_NOT_CONTACTED_AUDIT_TABLE", "AuditTable"), \
         patch.object(executor, "CONNECT_INSTANCE_ID", "instance-1"), \
         patch("vip_shared.infrastructure.persistence.customer_profiles_client.build_from_env", return_value=mock_cp_client), \
         patch.object(executor, "_get_connect_client", return_value=mock_connect), \
         patch.object(executor, "_get_ddb_client") as get_ddb:
        mock_ddb = MagicMock()
        get_ddb.return_value = mock_ddb

        executor._audit_not_contacted_leads(_run(), _cs(), _bucket())

        mock_ddb.put_item.assert_not_called()


def test_never_raises_on_segment_lookup_failure():
    with patch.object(executor, "_NOT_CONTACTED_AUDIT_TABLE", "AuditTable"), \
         patch.object(executor, "CONNECT_INSTANCE_ID", "instance-1"), \
         patch(
             "vip_shared.infrastructure.persistence.customer_profiles_client.build_from_env",
             side_effect=RuntimeError("boom"),
         ):
        executor._audit_not_contacted_leads(_run(), _cs(), _bucket())  # must not raise


def test_never_raises_on_dynamodb_write_failure():
    from botocore.exceptions import ClientError

    mock_cp_client = MagicMock()
    mock_cp_client.get_segment_definition.return_value = _segment_definition(["+12025550111"])
    mock_connect = MagicMock()
    mock_connect.get_paginator.return_value.paginate.return_value = [{"Contacts": []}]

    with patch.object(executor, "_NOT_CONTACTED_AUDIT_TABLE", "AuditTable"), \
         patch.object(executor, "CONNECT_INSTANCE_ID", "instance-1"), \
         patch("vip_shared.infrastructure.persistence.customer_profiles_client.build_from_env", return_value=mock_cp_client), \
         patch.object(executor, "_get_connect_client", return_value=mock_connect), \
         patch.object(executor, "_get_ddb_client") as get_ddb:
        mock_ddb = MagicMock()
        mock_ddb.put_item.side_effect = ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "PutItem")
        get_ddb.return_value = mock_ddb

        executor._audit_not_contacted_leads(_run(), _cs(), _bucket())  # must not raise
