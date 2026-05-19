# Archived: `feeder/`

**Date archived:** 2026-04-22
**Reason:** See [ADR-001](../../../docs/architecture-decisions.md#adr-001--reject-v2-external-push-for-voice-outbound-campaigns) and [ADR-002](../../../docs/architecture-decisions.md#adr-002--segment-driven-campaigns-with-ui-triggered-recompute).

## What this was

This Lambda was built to push leads from the Redis `wait_list` directly into Amazon Connect Outbound Campaigns via the external-push API (`PutDialRequestBatch` / `PutOutboundRequestBatch`), attempting to bypass the 24h+ segment refresh lag.

## Why it was archived

After 5 variations tested (V1 segment source, V2 segment source, V2 eventTrigger, V2 with/without `communicationTimeConfig`, V2 updated schedule), all returned `InvalidInput` or `Operation is not valid for this campaign` for voice channel external push. AWS's own reference architecture uses `StartOutboundVoiceContact` for voice + custom-list patterns — see `aws-samples/voice-channel-for-outbound-campaigns`.

For this project, the decision was to abandon the external-push pattern entirely and build an admin UI around **segment-driven campaigns with UI-triggered recompute** (on-demand `CreateSegmentEstimate`). This delivers the same "fresh data → dial" outcome using supported AWS APIs.

## What was reused

The domain layer code was **extracted** to `services/shared/python/vip_shared/` and is now used by the new API Lambdas:

- `FilterEvaluator` → `services/shared/python/vip_shared/domain/services/filter_evaluator.py`
- `ScheduleEvaluator` → `services/shared/python/vip_shared/domain/services/schedule_evaluator.py`
- `CommLimitsEvaluator` → `services/shared/python/vip_shared/domain/services/comm_limits_evaluator.py`
- `FilterRule` → `services/shared/python/vip_shared/domain/entities/filter_rule.py`
- `StructuredLogger` → `services/shared/python/vip_shared/infrastructure/telemetry/structured_logger.py`
- `MetricsPublisher` → `services/shared/python/vip_shared/infrastructure/telemetry/metrics_publisher.py`

## When to revisit

If any of these change, consider resurrecting a real-time dialing Lambda:

- AWS adds explicit support for voice external push to Outbound Campaigns
- Business requirement shifts to sub-5-minute dial latency that segment-snapshot cannot meet
- AWS publishes a new reference architecture contradicting the current one

## Do NOT delete this directory

Kept as technical evidence of the investigation. If/when the above happens, the existing code is a starting point for a resurrected feeder.
