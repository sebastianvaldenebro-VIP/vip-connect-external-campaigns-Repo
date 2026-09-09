from unittest.mock import MagicMock
from campaign_queue import CampaignQueue

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


def test_dequeue_retries_next_item_when_race_lost():
    """Two Lambdas racing for the same PENDING item: the loser's conditional
    update fails and dequeue must move on to try the next item instead of
    raising or returning None."""
    items = [
        {"campaignId": "campaign-1", "sk": "ts1#uuid1", "contactUUID": "uuid1",
         "phone": "+15551111111", "status": "PENDING"},
        {"campaignId": "campaign-1", "sk": "ts2#uuid2", "contactUUID": "uuid2",
         "phone": "+15552222222", "status": "PENDING"},
    ]
    q, table = _make_queue(items=items)
    table.meta.client.exceptions.ConditionalCheckFailedException = Exception
    table.update_item.side_effect = [Exception("lost the race"), {}]

    contact = q.dequeue("campaign-1")

    assert contact is not None
    assert contact.contact_uuid == "uuid2"
    assert table.update_item.call_count == 2


def test_dequeue_paginates_across_query_pages():
    """A first page with no PENDING items but a LastEvaluatedKey must be
    followed to a second page before giving up."""
    q, table = _make_queue()
    table.query.side_effect = [
        {
            "Items": [
                {"campaignId": "campaign-1", "sk": "ts0#uuid0", "contactUUID": "uuid0",
                 "phone": "+15550000000", "status": "DISPATCHING"}
            ],
            "LastEvaluatedKey": {"campaignId": "campaign-1", "sk": "ts0#uuid0"},
        },
        {
            "Items": [
                {"campaignId": "campaign-1", "sk": "ts1#uuid1", "contactUUID": "uuid1",
                 "phone": "+15551111111", "status": "PENDING"}
            ]
        },
    ]
    table.update_item.return_value = {}

    contact = q.dequeue("campaign-1")

    assert contact is not None
    assert contact.contact_uuid == "uuid1"
    assert table.query.call_count == 2
    second_call_kwargs = table.query.call_args_list[1][1]
    assert second_call_kwargs["ExclusiveStartKey"] == {
        "campaignId": "campaign-1",
        "sk": "ts0#uuid0",
    }


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


def test_reset_to_pending_is_idempotent_on_conditional_check_failed():
    """Another invocation already transitioned the item away from DISPATCHING —
    reset_to_pending must swallow the conditional failure, not raise."""
    q, table = _make_queue()
    table.meta.client.exceptions.ConditionalCheckFailedException = Exception
    table.update_item.side_effect = Exception("ConditionalCheckFailed")
    q.reset_to_pending("campaign-1", "ts1#uuid1")  # must not raise


def test_mark_outcome_writes_outcome_when_unset():
    q, table = _make_queue()
    q.mark_outcome("campaign-1", "ts1#uuid1", "voicemail")
    call_kwargs = table.update_item.call_args[1]
    assert call_kwargs["Key"] == {"campaignId": "campaign-1", "sk": "ts1#uuid1"}
    assert call_kwargs["ExpressionAttributeValues"] == {":o": "voicemail"}
    assert call_kwargs["ConditionExpression"] == "attribute_not_exists(#o)"


def test_mark_outcome_is_idempotent_when_already_set():
    """A concurrent invocation already recorded the outcome — must not raise."""
    q, table = _make_queue()
    table.meta.client.exceptions.ConditionalCheckFailedException = Exception
    table.update_item.side_effect = Exception("ConditionalCheckFailed")
    q.mark_outcome("campaign-1", "ts1#uuid1", "answered")  # must not raise


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
