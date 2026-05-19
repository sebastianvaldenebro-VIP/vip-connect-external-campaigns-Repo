from __future__ import annotations

from collections.abc import Iterable

from boto3.resources.base import ServiceResource

from ...domain.entities.campaign_config import CampaignConfig
from ...domain.repositories.filter_repository import FilterRepository


class DynamoDbFilterRepository(FilterRepository):
    def __init__(self, table_resource: ServiceResource) -> None:
        self._table = table_resource

    def list_enabled(self) -> Iterable[CampaignConfig]:
        response = self._table.scan(
            FilterExpression="enabled = :t",
            ExpressionAttributeValues={":t": True},
        )
        items = response.get("Items", [])
        while "LastEvaluatedKey" in response:
            response = self._table.scan(
                FilterExpression="enabled = :t",
                ExpressionAttributeValues={":t": True},
                ExclusiveStartKey=response["LastEvaluatedKey"],
            )
            items.extend(response.get("Items", []))

        return [CampaignConfig.from_ddb_item(item) for item in items]
