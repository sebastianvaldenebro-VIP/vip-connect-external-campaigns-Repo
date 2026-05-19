from __future__ import annotations

import json
import os
from functools import lru_cache

import boto3
import redis

from application.feeder_orchestrator import FeederOrchestrator
from domain.services.comm_limits_evaluator import CommLimitsEvaluator
from domain.services.filter_evaluator import FilterEvaluator
from domain.services.schedule_evaluator import ScheduleEvaluator
from infrastructure.connect.campaign_dialer import ConnectCampaignDialer
from infrastructure.persistence.ddb_filter_repo import DynamoDbFilterRepository
from infrastructure.persistence.ddb_tracking_repo import DynamoDbTrackingRepository
from infrastructure.persistence.redis_lead_source import RedisLeadSource
from infrastructure.telemetry import metrics_publisher, structured_logger

_logger = structured_logger.build()


@lru_cache(maxsize=1)
def _redis_password() -> str | None:
    arn = os.environ.get("REDIS_PASSWORD_SECRET_ARN")
    if not arn:
        return None
    secrets = boto3.client("secretsmanager")
    response = secrets.get_secret_value(SecretId=arn)
    secret_str = response.get("SecretString", "")
    try:
        parsed = json.loads(secret_str)
        return str(parsed.get("password") or parsed.get("REDIS_PASS") or secret_str)
    except json.JSONDecodeError:
        return secret_str


@lru_cache(maxsize=1)
def _redis_client():
    return redis.Redis(
        host=os.environ["REDIS_HOST"],
        port=int(os.environ.get("REDIS_PORT", "6379")),
        password=_redis_password(),
        decode_responses=True,
        socket_timeout=10,
        socket_connect_timeout=10,
    )


def _build_orchestrator() -> FeederOrchestrator:
    ddb = boto3.resource("dynamodb")
    campaigns_client = boto3.client("connectcampaigns")
    cloudwatch = boto3.client("cloudwatch")

    filter_repo = DynamoDbFilterRepository(ddb.Table(os.environ["FILTERS_TABLE"]))
    tracking_repo = DynamoDbTrackingRepository(ddb.Table(os.environ["TRACKING_TABLE"]))
    lead_source = RedisLeadSource(_redis_client(), team=os.environ["TEAM"])
    dialer = ConnectCampaignDialer(campaigns_client)
    metrics = metrics_publisher.MetricsPublisher(
        cloudwatch, namespace=os.environ["METRICS_NAMESPACE"]
    )

    return FeederOrchestrator(
        filter_repo=filter_repo,
        tracking_repo=tracking_repo,
        lead_source=lead_source,
        dialer=dialer,
        filter_evaluator=FilterEvaluator(),
        schedule_evaluator=ScheduleEvaluator(),
        comm_limits_evaluator=CommLimitsEvaluator(),
        logger=_logger,
        metrics=metrics,
    )


def lambda_handler(event, context):
    try:
        result = _build_orchestrator().execute()
        _logger.info("feeder_completed", **result)
        return result
    except Exception as exc:
        _logger.error("feeder_failed", error=str(exc))
        raise
