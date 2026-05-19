from __future__ import annotations

from datetime import datetime, timezone

from ..domain.entities.campaign_config import CampaignConfig
from ..domain.entities.dial_request import DialRequest
from ..domain.entities.lead import Lead
from ..domain.repositories.dialer import CampaignDialer
from ..domain.repositories.filter_repository import FilterRepository
from ..domain.repositories.lead_source import LeadSource
from ..domain.repositories.tracking_repository import TrackingRepository
from ..domain.services.comm_limits_evaluator import CommLimitsEvaluator
from ..domain.services.filter_evaluator import FilterEvaluator
from ..domain.services.schedule_evaluator import ScheduleEvaluator
from ..infrastructure.telemetry.metrics_publisher import MetricsPublisher
from ..infrastructure.telemetry.structured_logger import StructuredLogger

BATCH_SIZE = 25


class FeederOrchestrator:
    def __init__(
        self,
        filter_repo: FilterRepository,
        tracking_repo: TrackingRepository,
        lead_source: LeadSource,
        dialer: CampaignDialer,
        filter_evaluator: FilterEvaluator,
        schedule_evaluator: ScheduleEvaluator,
        comm_limits_evaluator: CommLimitsEvaluator,
        logger: StructuredLogger,
        metrics: MetricsPublisher,
    ) -> None:
        self._filter_repo = filter_repo
        self._tracking_repo = tracking_repo
        self._lead_source = lead_source
        self._dialer = dialer
        self._filter_evaluator = filter_evaluator
        self._schedule_evaluator = schedule_evaluator
        self._comm_limits_evaluator = comm_limits_evaluator
        self._logger = logger
        self._metrics = metrics

    def execute(self) -> dict:
        now_utc = datetime.now(tz=timezone.utc)
        now_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

        campaigns = list(self._filter_repo.list_enabled())
        self._logger.info("campaigns_loaded", count=len(campaigns))

        if not campaigns:
            return {"campaigns": 0, "pushed": 0, "failed": 0}

        leads = list(self._lead_source.iter_leads())
        self._logger.info("leads_sourced", count=len(leads))

        total_pushed = 0
        total_failed = 0
        results_by_campaign: dict[str, dict] = {}

        for campaign in campaigns:
            stats = self._process_campaign(campaign, leads, now_utc, now_iso)
            results_by_campaign[campaign.campaign_id] = stats
            total_pushed += stats["pushed"]
            total_failed += stats["failed"]

        self._metrics.flush()

        return {
            "campaigns": len(campaigns),
            "pushed": total_pushed,
            "failed": total_failed,
            "per_campaign": results_by_campaign,
        }

    def _process_campaign(
        self,
        campaign: CampaignConfig,
        leads: list[Lead],
        now_utc: datetime,
        now_iso: str,
    ) -> dict:
        if not self._schedule_evaluator.is_within_window(campaign, now_utc):
            self._logger.info("campaign_skipped_outside_window", campaign_id=campaign.campaign_id)
            self._metrics.add("SkippedOutsideWindow", 1, campaign.campaign_id)
            return {"matching": 0, "eligible": 0, "pushed": 0, "failed": 0}

        matching = self._filter_evaluator.filter(leads, campaign.filters)
        self._metrics.add("MatchingLeads", len(matching), campaign.campaign_id)

        eligible = self._filter_eligible(campaign, matching, now_utc, now_iso)
        self._metrics.add("EligibleLeads", len(eligible), campaign.campaign_id)

        pushed = failed = 0
        for batch_start in range(0, len(eligible), BATCH_SIZE):
            batch = eligible[batch_start : batch_start + BATCH_SIZE]
            batch_pushed, batch_failed = self._push_batch(campaign, batch, now_iso)
            pushed += batch_pushed
            failed += batch_failed

        self._metrics.add("LeadsPushed", pushed, campaign.campaign_id)
        self._metrics.add("LeadsFailed", failed, campaign.campaign_id)

        self._logger.info(
            "campaign_processed",
            campaign_id=campaign.campaign_id,
            matching=len(matching),
            eligible=len(eligible),
            pushed=pushed,
            failed=failed,
        )

        return {
            "matching": len(matching),
            "eligible": len(eligible),
            "pushed": pushed,
            "failed": failed,
        }

    def _filter_eligible(
        self,
        campaign: CampaignConfig,
        matching: list[Lead],
        now_utc: datetime,
        now_iso: str,
    ) -> list[Lead]:
        eligible: list[Lead] = []
        for lead in matching:
            if self._tracking_repo.is_in_flight(lead.id, campaign.campaign_id, now_iso):
                continue
            history = self._tracking_repo.list_push_timestamps(lead.id, campaign.campaign_id)
            if self._comm_limits_evaluator.exceeded(campaign, history, now_utc):
                continue
            eligible.append(lead)
        return eligible

    def _push_batch(
        self,
        campaign: CampaignConfig,
        leads: list[Lead],
        now_iso: str,
    ) -> tuple[int, int]:
        requests: list[DialRequest] = []
        for lead in leads:
            phone = lead.normalized_phone_e164()
            if not phone:
                continue
            requests.append(
                DialRequest.build(
                    lead_id=lead.id,
                    phone_e164=phone,
                    expiration_minutes=campaign.dial_expiration_minutes,
                    attributes={
                        "lead_id": lead.id,
                        "first_name": lead.first_name,
                        "last_name": lead.last_name,
                        "groups": lead.groups,
                        "location": lead.location,
                        "campaign": lead.campaign,
                        "source": "external-feeder",
                    },
                )
            )

        if not requests:
            return 0, 0

        result = self._dialer.put_dial_batch(campaign.connect_campaign_id, requests)

        tracking_records: list[dict] = []
        success_tokens = {s["clientToken"] for s in result.successful}
        failed_tokens = {f["clientToken"]: f.get("failureCode", "Unknown") for f in result.failed}

        for request in requests:
            base = {
                "lead_id": request.lead_id,
                "campaign_id": campaign.campaign_id,
                "client_token": request.client_token,
                "phone": request.phone_number,
                "pushed_at": now_iso,
            }
            if request.client_token in success_tokens:
                tracking_records.append({**base, "status": "pushed"})
            elif request.client_token in failed_tokens:
                tracking_records.append(
                    {**base, "status": "failed", "failure_code": failed_tokens[request.client_token]}
                )

        if tracking_records:
            self._tracking_repo.record_pushes(tracking_records)

        return result.success_count, result.failure_count
