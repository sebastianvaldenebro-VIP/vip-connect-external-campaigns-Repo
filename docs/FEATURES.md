# Features Log — vip-connect-external-campaigns / api-plans

Nuevas funcionalidades agregadas al código. Cada entrada describe QUÉ hace y POR QUÉ existe.
Orden cronológico. **Append al final.**

---

## 2026-04 — Sprint 6, construcción inicial del sistema

### Sistema de Plans Orchestrator (`api-plans` Lambda)

Motor de ejecución de planes de outbound que orquesta campañas en Amazon Connect. Corre como Lambda invocado por EventBridge. Reemplaza el feeder manual.

### Buckets con duración fija (`run_duration_minutes`)

Un bucket puede configurarse con un tiempo límite en minutos. Al expirar, el bucket se fuerza a terminar y el plan avanza al siguiente. Permite turnos de llamadas con horarios precisos.

### DAG de dependencias entre campañas (`dependsOn`)

Cada campaña dentro de un bucket puede declarar IDs de campañas de las que depende (dentro del mismo bucket o de buckets anteriores). El ejecutor solo inicia una campaña cuando todos sus padres están completos. Permite coordinar lanzamientos secuenciales o paralelos dentro de un plan.

### Cascade-cancel por dependencias

Si una campaña padre termina en `error`, `cancelled`, o `expired`, todos sus hijos se cancelan automáticamente con razón `parent_cancelled`. Evita correr campañas huérfanas.

### Trigger `time` — inicio programado

Un plan puede configurarse con `trigger.type = "time"` y una hora COT (`HH:MM`). El sistema lo inicia automáticamente a esa hora.

### Trigger `on_plan_complete` con `afterBucket` — encadenamiento de planes

Un plan B puede configurarse para iniciar cuando un plan A completa un bucket específico (`afterBucket: N`). Permite pipelines de llamadas multi-etapa.

### Trigger `loop` — repetición dentro de ventana diaria

Un plan puede repetirse automáticamente mientras `now < loop.endTime` (hora COT). Al terminar un run dentro de la ventana, inicia otro inmediatamente.

### Pre-calentamiento within-plan (`_prestart_next_bucket`)

Cuando al bucket actual le quedan ≤5 min, el sistema pre-crea las campañas Connect del bucket siguiente en estado `warming`. Al avanzar, el bucket ya tiene campañas listas — no pierde los 5–6 min de inicialización del dialer.

### Admin UI — Gestión de segmentos y campañas

Frontend React/Vite/TypeScript que permite a operadores crear segmentos Customer Profiles, lanzar campañas Outbound, y ver métricas. Autenticación vía Cognito MFA. 9 pantallas: Dashboard, Segments CRUD, Campaigns CRUD, Profiles Browser, Analytics, Audit Log.

### Lambda compartida (`vip_shared` layer)

Layer Python con clientes AWS reutilizables: `outbound_campaigns_client`, `customer_profiles_client`, `redis_lead_source`, `segment_builder`. Compartido entre `api-plans`, `api-segments`, `api-campaigns`, `api-metrics`, `api-profiles`.

### `redis_lead_source.is_ready()` — detección de rebuild

Verifica `LLEN(list_key) > 0` antes de iterar registros. Permite distinguir "Redis reconstruyendo (LLEN transitoriamente 0)" de "lista genuinamente vacía". Base para `_RedisRebuildingError`.

---

## 2026-05-13 — Mejoras de resiliencia, sesión 1

### `_RedisRebuildingError` — error transitorio de Redis

Nueva clase de excepción lanzada cuando `is_ready()` retorna `False`. A diferencia de `_EmptySegmentError` (cancela permanentemente), dispara reintentos con delay. La campaña no se cancela durante un rebuild.

### Two-phase DDB claim en `_dispatch_cross_bucket_ready`

Antes de llamar a Connect, el executor escribe `status = "creating"` con `UpdateItem` condicional. Solo la invocación que gana el claim crea la campaña. Elimina la race condition de ticks concurrentes.

### Recovery `"creating" → "queued"` al inicio del tick

Si un tick crashea después del claim pero antes de completar la creación, la campaña queda en `"creating"`. Al inicio del siguiente tick, se detecta y se resetea a `"queued"` para reintentarla.

---

## 2026-05-14 — Warmup cross-plan y nuevos triggers, sesión 2

### Recovery de error campaigns en `_activate_warming_bucket`

Al inicio de `_activate_warming_bucket`, campañas en `error` sin `connectCampaignId` (fallidas antes de crear la campaña en Connect) se resetean a `queued`. El dispatcher las recoge como cold-start en lugar de ignorarlas.

### `_prestart_plan(plan_id)` — helper de pre-calentamiento cruzado

Pre-calienta las campañas del bucket 0 de un plan destino: crea las campañas en Connect, llama `StartCampaign`, y guarda los resultados en `pendingWarmup` en DynamoDB. Usado por `_prestart_chained_runs` y `prestart_check`.

### `_prestart_chained_runs(run, plan, bucket_index)` — warmup de planes encadenados

Llamado desde `tick` cuando el bucket actual es el último y quedan ≤5 min. Pre-calienta: (1) planes downstream con trigger `on_plan_complete` apuntando al plan actual; (2) el mismo plan si tiene loop activo y queda tiempo para otra vuelta.

### `prestart_check` action — warmup de planes por horario (`handler.py`)

Nueva acción del Lambda disparada por EventBridge `rate(1 minute)`. Escanea todos los planes con trigger `type=time` y pre-calienta los que inician dentro de 4–6 min. Cubre planes sin upstream que dispare el warmup. Regla EventBridge: `vip-plans-prestart-check`.

### `pendingWarmup` consumido en `start_run`

Cuando un plan tiene `pendingWarmup` en DynamoDB (pre-calentado previamente), `start_run` inyecta las campañas directamente en el estado del run con `status=warming`. El bucket inicia en caliente sin crear nada nuevo en Connect.

### `store.update_plan_pending_warmup(plan_id, data)` (`store.py`)

`UpdateItem` parcial sobre `PLAN#/META` para escribir o limpiar el campo `pendingWarmup`. Se usa al guardar resultados de pre-calentamiento y al consumirlos en `start_run`.

### Trigger `afterCampaign` — encadenamiento por campaña específica

Nuevo campo opcional en el trigger `on_plan_complete`: `afterCampaign: string` (campaignId). Plan B inicia cuando una campaña específica de Plan A completa — más granular que `afterBucket` que espera todo el bucket.

### `_fire_campaign_chains(upstream_plan_id, bucket_index, completed_campaign_ids)`

Al final del poll loop del tick, detecta campañas que pasaron a `completed` en esa vuelta. Si alguna coincide con el `afterCampaign` de un plan downstream, inicia ese plan inmediatamente.

### `_prestart_after_campaign(upstream_plan_id, campaign_id)` — warmup por campaña con duración fija

Pre-calienta planes cuyo trigger es `afterCampaign == campaign_id`, cuando la campaña upstream tiene `run_duration_minutes` configurado y quedan ≤5 min de su duración. Permite warmup anticipado para triggers a nivel de campaña individual.

### `afterCampaignPrewarmed` flag en campaignState

Bandera activada por el poll loop del tick tras llamar `_prestart_after_campaign`. Evita pre-calentamientos duplicados en ticks subsecuentes para la misma campaña.

### Guard `is_plan_locked` en `_maybe_loop`

Verifica que el plan no esté ya corriendo antes de iniciar una nueva vuelta del loop. Previene double-start en condiciones de concurrencia.

### COT end-time check en `tick` — reemplaza `_past_daily_cutoff`

Comparación directa contra `plan.loop.endTime` usando UTC-5 fijo (COT year-round, sin DST). El plan se detiene exactamente a la hora configurada — ya no se ve afectado por el cambio de horario de verano.

### `_force_finish_internal` sin loop restart

Eliminadas las llamadas a `_maybe_loop` y `start_run_chained` de `_force_finish_internal`. Un plan forzado a terminar (por cutoff o manualmente) ya no se reinicia — solo el avance natural de buckets puede disparar el loop.

### Frontend — Campaign picker en TriggerEditor (`PlanNew.tsx`)

Cuando el operador selecciona `afterBucket`, aparece un selector opcional de campaña (`afterCampaign`) con las campañas del bucket seleccionado del plan upstream. Permite configurar el trigger a nivel campaña individual.

### Frontend — Label `afterCampaign` en `PlanDetail.tsx`

Si el trigger tiene `afterCampaign` configurado, el detalle del plan muestra `"After {plan} → bucket {N} → campaign {id}"` en lugar de solo el bucket.

---

## 2026-05-15 — Warmup timing refactor y flow ARN guard, sesión 3

### `warmupStarted` flag — activación sin llamadas API a Connect

`_create_campaign_only` llama `StartCampaign` inmediatamente tras `CreateCampaign`. Si tiene éxito, guarda `warmupStarted=True`. Al activar el bucket, la campaña pasa directamente a `running` sin ninguna llamada API — el dialer ya está corriendo desde el warmup.

### `UpdateCampaignSchedule` fallback en activación

Si `warmupStarted=False` (el `StartCampaign` del warmup falló), la activación llama `UpdateCampaignSchedule` para refrescar el schedule a `now+60s` antes de reintentar `StartCampaign`. Evita "start time already passed" sin recrear la campaña.

### Fail-fast campaign flow ARN guard en `_create_campaign_only` y `_create_and_start_campaign`

Verifica que `connectCampaignFlowArn` esté en los params **antes** de llamar `CreateCampaign`. Si falta, lanza `ValueError` con instrucciones: crear un flow `campaign-<STATE>` en Connect o configurar `campaignFlowArn` en `campaignConfig`. Evita campañas creadas en Connect que nunca pueden iniciarse.

### Flows `campaign-LI` y `campaign-NY` creados en Connect

CAMPAIGN-type contact flows creados vía CLI con el mismo contenido que los canonicos existentes (`campaign-NJ`, `campaign-CT`, etc.). `resolve_campaign_flow_arn` ahora resuelve los 8 estados (NY, LI, NJ, CT, TX, MD, NCA, SCA) automáticamente para cualquier plan.

---

## 2026-05-15 — Auto-recreación de campaign flows, sesión 4

### Resolver auto-recreación de flows canónicos (`resolve_campaign_flow_arn`)

Si no existe un flow `campaign-<STATE>` en Connect, el resolver lo crea automáticamente con el contenido canónico (`PutDialRequest → EndFlowExecution`) y lo tagea `do-not-delete: true, managed-by: vip-plans`. Un operador puede borrar cualquier flow y el sistema se auto-repara en el siguiente tick sin errores visibles.

### Resolver estricto — solo acepta `campaign-<STATE>` exacto

Eliminado `_STATE_FLOW_PATTERNS` con patrones de substring permisivos. El resolver ahora requiere nombre exactamente `campaign-<STATE>`. No puede matchear accidentalmente flows de otro propósito (ej. `flow-TX cancella/noshow`).

### `campaignFlowArn` eliminado de `build_campaign_params` y de DynamoDB

Eliminado el fallback `cfg.get("campaignFlowArn")` de `build_campaign_params`. El único source of truth es el resolver. Limpiados todos los campos `campaignFlowArn` de los planes en DynamoDB (4 planes afectados).

### Permisos IAM `connect:CreateContactFlow` y `connect:TagResource`

Agregados al role del Lambda en la política `FunctionRoleDefaultPolicy41A10F9C` para soportar la auto-recreación de flows.

---

<!-- APPEND NUEVAS FEATURES ABAJO DE ESTA LÍNEA -->

---

## 2026-06-16 a 2026-07-01 — Progressive Branded Dialer

### Sistema completo Progressive Branded Dialer (`ApiProgressiveDialerStack`)

Nueva capa de infraestructura para llamadas salientes branded disparadas por disponibilidad del agente, no por scheduler. Amazon Connect Agent Event Stream (Kinesis) → consumer Lambda → SQS (22s delay) → caller Lambda → `StartOutboundVoiceContact` + First Orion INFORM.

**Tablas DynamoDB nuevas:**

- `VipActiveBrandedCampaigns` — PK=`CAMPAIGN#{campaignId}`, GSI=`queueArn-index`. Contiene campañas activas con `queueArn`, `contactFlowId`, `sourcePhone`, `priority`, `segmentName`, `segmentArn`.
- `VipProgressiveCampaignQueue` — PK=`campaignId`, SK=timestamp. Queue de leads pre-poblada por el seeder con TTL=24h. Cada item tiene `phone`, `contactId`, `contactUUID`, `status` (PENDING→DIALED).
- `VipAgentLock` — PK=`agentArn`. Lock por agente con TTL para prevenir double-dispatch.

**Tres Lambdas nuevas:**

- `vip-admin-progressive-dialer-consumer`: consume Kinesis Agent Event Stream, filtra `STATE_CHANGE` ROUTABLE Available, busca campañas por InboundQueues del agente, adquiere agent lock, desencola lead, dispara First Orion push, encola SQS.
- `vip-admin-progressive-dialer-seeder`: invocado por el executor de api-plans al iniciar una branded campaign. Lee el segmento CP, extrae teléfonos, popula `VipProgressiveCampaignQueue`.
- `vip-admin-progressive-dialer-caller`: consume SQS (22s delay), lee el lead de DDB, llama `StartOutboundVoiceContact`, actualiza status a DIALED.

**Flujo completo:**

1. Plan executor detecta `deliveryType: "branded"` → invoca seeder
2. Seeder lee CP segment, escribe leads en `VipProgressiveCampaignQueue`
3. Agente va Available en Connect → Kinesis emite `STATE_CHANGE`
4. Consumer Lambda: busca campaña por InboundQueues → adquiere lock → desencola lead → First Orion INFORM → SQS enqueue (22s)
5. Caller Lambda: `StartOutboundVoiceContact` → actualiza DDB item a DIALED

### `pinnedSegmentArn` en branded campaigns

Si una campaña tiene `pinnedSegmentArn` en su config, el executor usa ese segmento existente directamente en lugar de crear uno dinámico con `_create_segment`. Permite reutilizar segmentos CP ya construidos externamente sin consumir el timeout de creación de segmento en cada run.

### `sourcePhoneNumber` fallback en executor.py

`_start_one_campaign` acepta tanto `cfg["sourcePhone"]` como `cfg["sourcePhoneNumber"]` (fallback). El frontend del Plan editor escribe `sourcePhoneNumber`; planes legacy en DynamoDB pueden tener `sourcePhone`. Ambos formatos funcionan sin migración.

### `_normalize_phone_e164` — normalización de teléfonos del CRM

Nueva función en `executor.py` que convierte teléfonos del CRM a E.164 (`+1XXXXXXXXXX`) antes de construir el segmento CP. Maneja: 10 dígitos → `+1` prefix, 11 dígitos con `1` → `+` prefix, ya E.164 → sin cambio. Formatos como `(555) 123-4567` o `555-123-4567` también normalizados.

### `phones_to_segment_groups` — segmentos CP por teléfono (fix `_EmptySegmentError`)

`SegmentGroupsTranslator.phones_to_segment_groups` construye filtros `PhoneNumber.INCLUSIVE` en grupos de 50. Reemplaza `customer_ids_to_segment_groups` en el path branded porque CP no expone los Lead IDs del CRM — solo los teléfonos son buscables directamente. El seeder (`_extract_phones_from_filter`) lee los teléfonos del `segmentGroups` definition sin `BatchGetProfile`.

### Redis/Valkey `ssl=True`

`redis_lead_source.build_from_env` ahora pasa `ssl=True` al cliente Redis para soportar el endpoint TLS de Valkey. Sin esta flag, la conexión era rechazada silenciosamente.

### Consumer: `InboundQueues` fix — matching correcto de campañas branded

`extract_agent_info` retorna tanto `DefaultOutboundQueue` como todas las `InboundQueues` del routing profile. El consumer itera ambas en orden y usa la primera con campañas activas. Antes solo buscaba por `DefaultOutboundQueue` (queue para llamadas agente-iniciadas), pero las campañas branded se registran contra las queues de `InboundQueues`.

### Frontend — Plan editor: tipo de entrega Branded (Progressive)

`PlanNew.tsx` ahora incluye la opción `Branded (Progressive)` en el selector de delivery type. Al seleccionarlo:

- Aparece campo **Queue ARN** (ARN completo de la queue de agentes)
- Se ocultan los campos `dialerType`, bandwidth, dialing capacity, AMD enabled, AMD await prompt (irrelevantes para branded)
- `contactFlowId` y `sourcePhoneNumber` permanecen visibles

`PlanDetail.tsx` muestra un badge amber "Branded" en cada campaña con `deliveryType: "branded"`.

`api.ts`: `deliveryType` extendido con `'branded'`, `BucketCampaignConfig` incluye `queueArn?: string`.

---

## 2026-05-21 — Sesión 11: alertas operacionales SNS y UX del Scheduler

### Alertas SNS operacionales (`_notify_sns`)

Nueva función fire-and-forget en `executor.py` que publica mensajes al topic `vip-plans-alerts` (SNS). Nunca lanza excepción — los fallos de alerting no afectan la ejecución del plan.

Puntos de emit:

- Campaña detectada como `connect_deleted` externamente (guarda S-11-A)
- Run abortado por borrado externo masivo (S-11-A abort)
- Creación de campaña fallida (`creation_failed`)
- Throttling / quota exceeded de Connect (revertida a `queued`)
- Run completado con campañas en error

**Infraestructura:** Topic `arn:aws:sns:us-east-1:165505826690:vip-plans-alerts` creado con KMS CMK. IAM `sns:Publish` en el role del Lambda. `SNS_ALERTS_TOPIC_ARN` env var inyectada via CDK. Para recibir alertas: `aws sns subscribe --topic-arn ... --protocol email --notification-endpoint <email>`.

### Scheduler — planes enabled al tope de la lista

`PlansScheduler.tsx`: `nonTemplatePlans` ahora se ordena con planes habilitados (`trigger.type !== 'manual'`) primero. Los planes desactivados (manual) van al final. El orden dentro de cada grupo es el original de la API.

### "Apply to run" — aplicar cambios del plan a un run activo

Nuevo botón **"Apply to run"** en PlanDetail. Aparece solo cuando el run está activo (`status === 'running'`) y el plan fue editado después de que el run inició (`planSnapshot.buckets` difiere del plan actual).

Al presionarlo, llama `POST /plans/{id}/runs/{runId}/apply-snapshot`. El backend actualiza el `planSnapshot` del run **solo en buckets con `status: "queued"`** — los buckets ya iniciados (running, completed, cancelled) conservan su configuración original. El executor del próximo tick ya usa la nueva config para los buckets pendientes.

**Infraestructura:**

- `store.apply_plan_to_run()` — merge de planSnapshot con optimistic locking
- `handlers/runs.apply_plan_snapshot` — endpoint POST, devuelve 409 si run no está activo o si hay write concurrente
- `api.plans.applySnapshotV2()` — llamada desde el frontend

### `_CutoffTooCloseError` — campaigns expiradas limpiamente cerca del corte diario

Nueva excepción en `executor.py` que reemplaza el `ValueError` genérico en el path de creación de campaigns cuando `start_time >= end_time` (campaña creada dentro de los 6 min antes de las 7 PM COT). El campaign se marca `expired` con `exitReason=cutoff_too_close` en lugar de `error`, y no dispara alerta de fallo. En el path de warmup (`_prestart_next_bucket`) el campaign simplemente queda en `queued` sin modificarse — el próximo tick del siguiente día lo iniciará normalmente.

### Fallback de rebuild en `_EmptySegmentError` exhaustion *(2026-05-26)*

Cuando una campaña agota sus reintentos de `_EmptySegmentError`, el executor ahora hace un check final de `_check_redis_ready()` antes de cancelar:

- Si `LLEN=0` (Redis en rebuild): resetea `reconcileRetries=0` y re-encola — no cancela.
- Si `LLEN>0` (genuinamente vacío): cancela con `skipped_empty` como antes.

Aplica en `_start_one_campaign` y `_prestart_next_bucket`. Evita falsos `skipped_empty` cuando un rebuild activo sucede exactamente entre el último retry y la decisión de cancelar.

### `reconcileRetryLimit` default aumentado a 5 *(2026-05-25)*

El default de reintentos para reconciliación de segmentos durante rebuilds de Redis aumentó de 2 a 5 (6 intentos totales, ~6 minutos de ventana). Reduce falsos `skipped_empty` cuando el pipeline de ingest está reconstruyendo la lista y los leads del estado aún no han sido cargados. Configurable por bucket con el campo `reconcileRetryLimit`.

### CDK: SNS topic importado por ARN (no managed)

`api-plans-stack.ts`: El topic se referencia con `sns.Topic.fromTopicArn()` para no depender de `SNS:GetTopicAttributes` en el CFN exec role (bloqueado por `EngineeringPermissionBoundary`). Fix adicional: `buildSharedLayer(this)` hoistado a `const sharedLayer` — antes se llamaba dos veces en el mismo scope y fallaba con "duplicate construct name".

---

## 2026-05-28 — Journey delivery type support

### Journey campaigns from Plans

Operators can now configure each campaign in a plan bucket as **Campaign** (default, MANAGED type) or **Journey** (JOURNEY type) directly from the Plan editor.

**How it works:**

- Each campaign in the BucketEditor has a new **Delivery type** dropdown: `Campaign` | `Journey`
- Journey campaigns use the canonical `Test-Journey-Flow` contact flow (CAMPAIGN type) instead of the per-state `campaign-<STATE>` flow
- The payload sent to Connect adds `type: "JOURNEY"` and `communicationLimitsOverride` — all other parameters (queue, contact flow, source phone, dialer type, AMD, schedule, segment) are identical to regular campaigns
- The monitor (PlanDetail) shows a purple `Journey` badge on each Journey campaign card

**Backend:**

- `builders.py`: `resolve_journey_flow_arn()` resolves `Test-Journey-Flow` by name; `build_campaign_params()` accepts `delivery_type` param
- `executor.py`: reads `campaign.deliveryType` in both `_create_and_start_campaign` and `_create_campaign_only` (pre-warm path)

**Files:** `services/api-plans/src/builders.py`, `services/api-plans/src/executor.py`, `frontend/src/lib/api.ts`, `frontend/src/pages/PlanNew.tsx`, `frontend/src/pages/PlanDetail.tsx`

## 2026-08-18 — Mapeo dinámico de estados/locations + Location Onboarding Guard

### Segments/SegmentDetail/PlanNew leen `VipLocationMapping` en vivo en vez de mapas hardcodeados

`Segments.tsx`, `SegmentDetail.tsx` y `PlanNew.tsx` (el checklist de "States" del editor de campaign) tenían cada uno su propia copia estática de la lista de estados (`STATE_LOCATION_MAP`, y un 4to array independiente en `PlanNew.tsx`), que nunca se sincronizaban con la tabla DynamoDB `VipLocationMapping` — Pennsylvania y otras locations nuevas no aparecían en ningún lado hasta que alguien actualizaba manualmente cada copia. Los tres ahora usan `useLocationMapping()` (react-query, ya existente, antes solo parcialmente adoptado) como fuente única de verdad; los mapas estáticos quedan solo como fallback mientras la query resuelve.

**Archivos:** `frontend/src/lib/stateLocationMap.ts`, `frontend/src/pages/Segments.tsx`, `frontend/src/pages/SegmentDetail.tsx`, `frontend/src/pages/PlanNew.tsx`.

### Teléfono/área canónicos de Pennsylvania

`STATE_DEFAULT_PHONES.PA = '+12154009167'` y `STATE_AREA_CODES.PA = ['215']` agregados a `areaCodeMap.ts` (verificados en vivo contra Connect: `list-phone-numbers-v2`).

**Archivos:** `frontend/src/lib/areaCodeMap.ts`.

### `EnableCampaignModal` resuelve el campaign flow vía backend, con fallback al heurístico de cliente

El botón "Enable Campaign" de Segments dependía 100% de una búsqueda de substring en el nombre de los contact flows ya cargados en el cliente (`suggestCampaignFlow`). Ahora llama primero a `POST /plans/resolve-campaign-flow` (nuevo endpoint, wrapper delgado sobre `builders.resolve_campaign_flow_arn`, ya usado por el executor de Plans — auto-crea el flow `campaign-<ESTADO>` si no existe), y solo si esa llamada falla o no encuentra nada cae al heurístico de cliente existente. Cierra la clase de bug de la sección de Bug Log de abajo para cualquier estado futuro sin tocar código.

**Archivos:** `frontend/src/lib/api.ts`, `frontend/src/components/EnableCampaignModal.tsx`, `services/api-plans/src/handlers/plans.py`, `services/api-plans/src/router.py`, `infra/lib/stacks/api-stack.ts`.

### Location Onboarding Guard — alarma SNS cuando un estado nuevo no tiene teléfono canónico

Nuevo Lambda (`vip-location-onboarding-guard`, sin permisos de Connect — solo detección + alerta) triggereado por DynamoDB Streams (`INSERT`) sobre `VipLocationMapping`. Cuando aparece un `stateCode` genuinamente nuevo (ninguna otra location comparte ese código) sin el atributo `canonicalPhone` seteado, publica a `vip-plans-alerts` (mismo topic/formato que las alertas existentes de `executor.py`). Nota importante: `canonicalPhone` en `VipLocationMapping` todavía **no** es la fuente de verdad real para selección de teléfono en producción — eso sigue siendo `frontend/src/lib/areaCodeMap.ts` — el mensaje de la alerta señala esto explícitamente para no confundir a operaciones. Backfill de `canonicalPhone`/`areaCodes` para los 9 estados existentes: `infra/scripts/backfill-location-canonical-phone.py` (creado, **no ejecutado en producción** — corresponde a Sebastian decidir cuándo correrlo).

**Archivos:** `services/api-plans/src/location_onboarding_guard.py`, `infra/lib/stacks/api-plans-stack.ts`, `infra/scripts/create-alarms.sh`, `infra/scripts/backfill-location-canonical-phone.py`.
