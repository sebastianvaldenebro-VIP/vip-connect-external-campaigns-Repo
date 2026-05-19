from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from boto3.dynamodb.conditions import Key
from boto3.resources.base import ServiceResource

from ...domain.repositories.tracking_repository import TrackingRepository

TRACKING_TTL_DAYS = 30
IN_FLIGHT_WINDOW_MINUTES = 35


class DynamoDbTrackingRepository(TrackingRepository):
    def __init__(self, table_resource: ServiceResource) -> None:
        self._table = table_resource

    def list_push_timestamps(self, lead_id: str, campaign_id: str) -> list[str]:
        response = self._table.query(
            KeyConditionExpression=Key("lead_id").eq(lead_id)
            & Key("campaign_id_pushed_at").begins_with(f"{campaign_id}#"),
            ProjectionExpression="pushed_at",
        )
        return [item["pushed_at"] for item in response.get("Items", []) if "pushed_at" in item]

    def is_in_flight(self, lead_id: str, campaign_id: str, now_iso: str) -> bool:
        now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        cutoff = (now - timedelta(minutes=IN_FLIGHT_WINDOW_MINUTES)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        response = self._table.query(
            KeyConditionExpression=Key("lead_id").eq(lead_id)
            & Key("campaign_id_pushed_at").gte(f"{campaign_id}#{cutoff}"),
            FilterExpression="#s IN (:pushed, :dialed)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":pushed": "pushed", ":dialed": "dialed"},
            Limit=1,
        )
        return bool(response.get("Items"))

    def record_pushes(self, items: Iterable[dict]) -> None:
        ttl = int((datetime.now(tz=timezone.utc) + timedelta(days=TRACKING_TTL_DAYS)).timestamp())
        with self._table.batch_writer() as batch:
            for record in items:
                batch.put_item(
                    Item={
                        "lead_id": record["lead_id"],
                        "campaign_id_pushed_at": f"{record['campaign_id']}#{record['pushed_at']}",
                        "campaign_id": record["campaign_id"],
                        "client_token": record["client_token"],
                        "phone": record["phone"],
                        "status": record["status"],
                        "pushed_at": record["pushed_at"],
                        "ttl": ttl,
                        **({"failure_code": record["failure_code"]} if record.get("failure_code") else {}),
                    }
                )
