# Features Needed — Backlog

Funcionalidades solicitadas pero no implementadas. Cada entrada describe QUÉ se pidió, POR QUÉ se pausó, y qué se necesitaría para construirla. Orden cronológico. **Append al final.**

---

## 2026-08-27 — Dialer type seleccionable (predictive) para campañas branded

**Pedido original:** exponer el selector de `dialerType` (Progressive/Predictive/Agentless) también para campañas branded en `PlanNew.tsx` (hoy oculto vía `campaign.deliveryType !== 'branded'`), con Predictive como default.

**Por qué se pausó:** las campañas branded no usan el dialer nativo de Connect Campaigns V2 — nunca llaman `CreateCampaign` (confirmado: `connectCampaignId` siempre `None` para branded). Usan un sistema propio (`services/api-progressive-dialer/`): un lock atómico por agente (`agent_lock.py`) + una cola DynamoDB (`campaign_queue.py`) + un Lambda que despacha `StartOutboundVoiceContact` cuando llega un evento Kinesis de "agente pasó a Available" (`handler_consumer.py`/`handler_caller.py`). Ese diseño es intrínsecamente 1-agente-libre → 1-llamada; no existe ningún concepto de "predictive" (marcar más líneas que agentes libres) en ese pipeline. El campo `dialerType`/`bandwidthAllocation` que se guarda hoy en el `campaignConfig` de un bucket branded es config muerta — se persiste pero ningún código branded la lee.

**Qué se necesitaría para construirla de verdad:** rediseñar el dispatcher custom para soportar dial-ahead real (marcar N líneas por agente libre anticipando no-contesta/voicemail, con lógica de abandono cuando sobra una línea que sí contesta). Es trabajo de arquitectura, no un toggle de UI — tocaría `agent_lock.py`, `handler_caller.py`, `campaign_queue.py` y probablemente el modelo de datos de la cola.

**Estado actual:** branded se queda en su comportamiento de siempre (1:1 vía lock por agente). No es un blocker funcional — el problema real detectado el mismo día (ver BUGLOG.md) fue de *throughput* (falta de re-arm por timer + contención de eventos de disponibilidad entre campañas concurrentes), no del modo de dialing.
