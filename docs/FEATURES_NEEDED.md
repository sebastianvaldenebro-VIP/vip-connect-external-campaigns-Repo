# Features Needed — Backlog

Funcionalidades solicitadas pero no implementadas. Cada entrada describe QUÉ se pidió, POR QUÉ se pausó, y qué se necesitaría para construirla. Orden cronológico. **Append al final.**

---

## 2026-08-27 — Dialer type seleccionable (predictive) para campañas branded

**Pedido original:** exponer el selector de `dialerType` (Progressive/Predictive/Agentless) también para campañas branded en `PlanNew.tsx` (hoy oculto vía `campaign.deliveryType !== 'branded'`), con Predictive como default.

**Por qué se pausó:** las campañas branded no usan el dialer nativo de Connect Campaigns V2 — nunca llaman `CreateCampaign` (confirmado: `connectCampaignId` siempre `None` para branded). Usan un sistema propio (`services/api-progressive-dialer/`): un lock atómico por agente (`agent_lock.py`) + una cola DynamoDB (`campaign_queue.py`) + un Lambda que despacha `StartOutboundVoiceContact` cuando llega un evento Kinesis de "agente pasó a Available" (`handler_consumer.py`/`handler_caller.py`). Ese diseño es intrínsecamente 1-agente-libre → 1-llamada; no existe ningún concepto de "predictive" (marcar más líneas que agentes libres) en ese pipeline. El campo `dialerType`/`bandwidthAllocation` que se guarda hoy en el `campaignConfig` de un bucket branded es config muerta — se persiste pero ningún código branded la lee.

**Qué se necesitaría para construirla de verdad:** rediseñar el dispatcher custom para soportar dial-ahead real (marcar N líneas por agente libre anticipando no-contesta/voicemail, con lógica de abandono cuando sobra una línea que sí contesta). Es trabajo de arquitectura, no un toggle de UI — tocaría `agent_lock.py`, `handler_caller.py`, `campaign_queue.py` y probablemente el modelo de datos de la cola.

**Estado actual:** branded se queda en su comportamiento de siempre (1:1 vía lock por agente). No es un blocker funcional — el problema real detectado el mismo día (ver BUGLOG.md) fue de *throughput* (falta de re-arm por timer + contención de eventos de disponibilidad entre campañas concurrentes), no del modo de dialing.

---

## 2026-08-31 — `cs["reconcile"]` nunca se llena para branded cuando el seeder encola 0 leads

**Pedido original:** identificado en el review final de la rama `feature/reconcile-normalization` (plan `docs/superpowers/plans/2026-08-31-reconcile-normalization.md`): las campañas branded deberían reportar `cs["reconcile"] = {"expected", "actual", "retries"}` en el mismo caso — segmento creado con éxito (`expected`/`actual` ya calculados) pero `_invoke_seeder` devuelve `0` (nada quedó encolado) — que hoy es, precisamente, el más útil de diagnosticar (permite distinguir "el filtro no matcheó nadie" de "matcheó gente pero se descartó en el seeding/normalización de teléfono").

**Por qué se pausó:** en `_start_one_campaign` (`services/api-plans/src/executor.py`), el branch branded llama a `_create_segment(...)` (que ya devuelve `expected`/`actual`) y luego a `_invoke_seeder(...)`. Cuando `seeded == 0`, el código retorna temprano vía `_stop_branded_campaign(cs)` + `cs["status"] = "completed"` / `exitReason = "empty_segment"` — ANTES de llegar a la línea que escribe `cs["reconcile"]` unas líneas más abajo (la que sí corre cuando `seeded > 0`). Arreglarlo bien requiere mover el reconcile-write más arriba en el flujo branded (antes del `if seeded == 0` early-return), no es un fix de una línea — es un cambio deliberado de orden de escritura que el review de esta rama marcó explícitamente como fuera de alcance (scoping gap del plan original, no bug de la implementación), para no tocar la lógica de branded en el mismo fix dispatch que solo debía corregir el bug de `_prestart_next_bucket`.

**Qué se necesitaría para construirla de verdad:** mover (o duplicar) el bloque `if expected is not None: cs["reconcile"] = {...}` a ANTES del `if seeded == 0:` early-return dentro de `_start_one_campaign`, de forma que un segmento con leads pero seeding en 0 también quede reconciliado. Requiere decidir qué `retries` reportar en ese caso (branded no tiene el mismo ciclo de reintento cross-tick que telephony-native/prestart) y agregar un test específico para `seeded == 0` con `expected`/`actual` no nulos.

**Estado actual:** sin cambios — branded con `seeded == 0` sigue sin `cs["reconcile"]`, exactamente igual que antes de esta rama. No es blocker: nada en producción lee `cs["reconcile"]` todavía (es data para el futuro Campaign Monitor UI).
