from unittest.mock import MagicMock, patch
import pytest
from campaign_queue import CampaignQueue, Contact

TABLE_NAME = "VipProgressiveCampaignQueue"


def _make_queue(items: list[dict] | None = None):
    mock_table = MagicMock()
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table
    q = CampaignQueue(TABLE_NAME, dynamodb_resource=mock_resource)
    q._table = mock_table
    if items is not None:
        mock_table.query.return_value = {"Items": items}
    return q, mock_table


def test_dequeue_returns_none_when_empty():
    q, table = _make_queue(items=[])
    assert q.dequeue("campaign-1") is None


def test_dequeue_returns_contact_and_marks_dispatching():
    item = {
        "campaignId": "campaign-1",
        "contactUUID": "uuid-abc",
        "sk": "2026-06-16T14:00:00.000Z#uuid-abc",
        "phone": "+15551234567",
        "status": "PENDING",
        "ttl": 9999999999,
    }
    q, table = _make_queue(items=[item])
    table.update_item.return_value = {}

    contact = q.dequeue("campaign-1")

    assert contact is not None
    assert contact.phone == "+15551234567"
    assert contact.campaign_id == "campaign-1"
    assert contact.contact_uuid == "uuid-abc"

    # Verify conditional write was made
    call_kwargs = table.update_item.call_args[1]
    assert call_kwargs["ConditionExpression"] is not None
    assert "DISPATCHING" in str(call_kwargs["ExpressionAttributeValues"])


def test_dequeue_skips_non_pending_items():
    items = [
        {"campaignId": "campaign-1", "sk": "ts1#uuid1", "contactUUID": "uuid1",
         "phone": "+15551111111", "status": "DISPATCHING"},
        {"campaignId": "campaign-1", "sk": "ts2#uuid2", "contactUUID": "uuid2",
         "phone": "+15552222222", "status": "PENDING"},
    ]
    q, table = _make_queue(items=items)
    table.update_item.return_value = {}

    contact = q.dequeue("campaign-1")
    assert contact.contact_uuid == "uuid2"


def test_mark_dialed_updates_status():
    q, table = _make_queue()
    q.mark_dialed("campaign-1", "ts1#uuid1", "contact-id-xyz")
    call_kwargs = table.update_item.call_args[1]
    assert "DIALED" in str(call_kwargs["ExpressionAttributeValues"])
    assert "contact-id-xyz" in str(call_kwargs["ExpressionAttributeValues"])
    # Must include a ConditionExpression to guard against SQS redelivery overwrites
    assert call_kwargs.get("ConditionExpression") is not None


def test_mark_dialed_is_idempotent_on_conditional_check_failed():
    """SQS redelivery after a successful dial must not raise — treat as idempotent success."""
    q, table = _make_queue()
    # Simulate ConditionalCheckFailedException (contact already advanced past DISPATCHING)
    table.meta.client.exceptions.ConditionalCheckFailedException = Exception
    table.update_item.side_effect = Exception("ConditionalCheckFailed")
    # Must not raise
    q.mark_dialed("campaign-1", "ts1#uuid1", "contact-id-xyz")


def test_enqueue_writes_pending_item():
    q, table = _make_queue()
    q.enqueue("campaign-1", "+15559876543")
    call_kwargs = table.put_item.call_args[1]
    item = call_kwargs["Item"]
    assert item["campaignId"] == "campaign-1"
    assert item["status"] == "PENDING"
    assert "phone" in item


def test_reset_to_pending_updates_status():
    q, table = _make_queue()
    q.reset_to_pending("campaign-1", "ts1#uuid1")
    table.update_item.assert_called_once()
    call_kwargs = table.update_item.call_args[1]
    assert "PENDING" in str(call_kwargs["ExpressionAttributeValues"])
    assert call_kwargs["Key"] == {"campaignId": "campaign-1", "sk": "ts1#uuid1"}


def test_get_phone_returns_phone():
    """Caller reads phone from DDB instead of SQS body — PHI stays out of the queue."""
    q, table = _make_queue()
    table.get_item.return_value = {
        "Item": {"campaignId": "campaign-1", "sk": "ts1#uuid1", "phone": "+15551234567"}
    }
    phone = q.get_phone("campaign-1", "ts1#uuid1")
    assert phone == "+15551234567"
    table.get_item.assert_called_once_with(Key={"campaignId": "campaign-1", "sk": "ts1#uuid1"})


def test_get_phone_returns_none_when_item_missing():
    q, table = _make_queue()
    table.get_item.return_value = {}
    assert q.get_phone("campaign-1", "ts1#uuid1") is None
