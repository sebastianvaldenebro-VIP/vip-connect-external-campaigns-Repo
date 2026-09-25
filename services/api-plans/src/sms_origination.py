"""Plans adapter for the new campaign SMS origin contract only."""
from __future__ import annotations

import boto3

from vip_shared.domain.services.sms_campaign import validate_version
from vip_shared.domain.services.sms_origination import SmsOriginationError, validate_promotional_origin

_origin_client = None


def validate_campaign_origin(config: dict, *, bucket_config: dict | None = None) -> None:
    """Legacy SMS and precall have no campaign version and perform no lookup."""
    if "smsTemplateVersion" in (bucket_config or {}):
        # SMS dispatch has always used campaignConfig from the campaign only;
        # unlike the voice path it does not inherit bucket configuration.
        raise SmsOriginationError("campaign_sms_version_must_be_configured_per_campaign")
    if "smsTemplateVersion" not in config:
        return
    validate_version(config["smsTemplateVersion"])
    global _origin_client
    if _origin_client is None:
        try:
            _origin_client = boto3.client("pinpoint-sms-voice-v2", region_name="us-east-1")
        except Exception:
            raise SmsOriginationError("campaign_sms_origin_lookup_failed") from None
    validate_promotional_origin(config.get("smsOriginationNumberArn"), client=_origin_client)


def validate_plan_origins(plan: dict) -> list[str]:
    """Check each distinct campaign origin once per operation, without caching metadata."""
    errors = []
    checked = {}
    for bi, bucket in enumerate(plan.get("buckets", [])):
        for ci, campaign in enumerate(bucket.get("campaigns", [])):
            config = campaign.get("campaignConfig") or {}
            bucket_config = bucket.get("campaignConfig") or {}
            if campaign.get("deliveryType") != "sms":
                continue
            if "smsTemplateVersion" not in config and "smsTemplateVersion" not in bucket_config:
                continue
            # Malformed values cannot be dict keys and must still fail closed.
            key = (repr(config.get("smsTemplateVersion")), repr(config.get("smsOriginationNumberArn")),
                   "smsTemplateVersion" in bucket_config)
            if key not in checked:
                try:
                    validate_campaign_origin(config, bucket_config=bucket_config)
                    checked[key] = None
                except ValueError as exc:
                    checked[key] = str(exc)
            if checked[key]:
                errors.append(f"bucket[{bi}] campaign[{ci}]: {checked[key]}")
    return errors
