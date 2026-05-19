# Bug Log — vip-connect-external-campaigns / api-plans

Cada entrada: fecha · síntoma · causa raíz · fix aplicado.
Orden cronológico. **Append al final.**

---

## 2026-04 — Sprint 6, construcción inicial

### ADR-001 — `PutDialRequestBatch` / V2 external push no funciona para voice

- **Síntoma:** 5 variantes de `CreateCampaign V2 + PutDialRequestBatch/PutOutboundRequestBatch` fallaron con `InvalidInput`, `Operation is not valid for this campaign`, `Missing required campaign parameter Schedule`, o conflicto con `CommunicationTimeConfig`.
- **Causa:** AWS Outbound Campaigns V2 no soporta external push para voz. El patrón oficial de AWS usa `StartOutboundVoiceContact` vía Step Functions.
- **Fix:** Pivote a campañas segment-driven con recompute on-demand via `CreateSegmentEstimate` (ADR-002). Ver `docs/architecture-decisions.md`.

---

## 2026-05-13 — Fixes de producción, sesión 1

### SharedLayer v38 — `redis` package missing

- **Síntoma:** Todas las campañas fallaban con `"redis package not installed"`.
- **Causa:** El layer se reconstruyó desde source code directamente, sin partir del zip existente. Los paquetes pip instalados en el layer anterior (incluyendo `redis`) no estaban en el source — se eliminaron silenciosamente.
- **Fix:** Restaurado usando SharedLayer v37 como base (`get-layer-version` → download zip → patch archivo → publish v39). Regla: **nunca reconstruir el layer desde source — siempre parchear el zip existente**.

---

### `_version` faltante en `store.py:_run_from_item`

- **Síntoma:** Cada `save_run` fallaba con DynamoDB `ConditionalCheckFailedException`. Ningún cambio de estado de run se podía guardar.
- **Causa:** `_run_from_item` no mapeaba el campo `_version` del item DynamoDB al dict del run. `save_run` usaba `run["_version"]` para la condición de escritura optimista — al no existir, siempre fallaba.
- **Fix:** Añadido `_version` al mapeo en `_run_from_item`.
- **Archivo:** `store.py:_run_from_item`

---

### Race condition en `_dispatch_cross_bucket_ready` — campañas Connect duplicadas

- **Síntoma:** Dos invocaciones concurrentes del mismo tick creaban campañas Connect duplicadas para el mismo slot. Se encontraron y eliminaron 12 campañas huérfanas TX-NL/MD-NL.
- **Causa:** Dos Lambda invocations leían el estado simultáneamente, ambas veían la campaña en `queued`, y ambas llamaban `_start_one_campaign` antes de que la otra guardara el cambio.
- **Fix:** Two-phase DynamoDB claim: `UpdateItem` condicional pone la campaña en `"creating"` (condición: que siga en `"queued"`). Solo la invocación que gana el claim procede. Añadida recovery `"creating" → "queued"` al inicio del tick para campañas atascadas si el primer tick crasheó.
- **Archivo:** `executor.py:_dispatch_cross_bucket_ready`

---

### `_EmptySegmentError` no se reintentaba — cancelación permanente por rebuild parcial de Redis

- **Síntoma:** Campañas con leads válidos se cancelaban con `skipped_empty` inmediatamente.
- **Causa:** Durante un rebuild, los leads de CA cargaban primero. `is_ready()` (LLEN > 0) pasaba, pero los filtros por estado NE retornaban 0 matches → `_EmptySegmentError` → cancelación permanente. El sistema no distinguía "genuinamente vacío" de "rebuilding con datos parciales".
- **Fix:** `_EmptySegmentError` ahora es retriable: hasta `reconcileRetryLimit` reintentos con 30 s de delay en `_start_one_campaign` y `_prestart_next_bucket`. Solo cancela permanentemente al agotar todos los intentos.
- **Archivo:** `executor.py:_start_one_campaign`, `executor.py:_prestart_next_bucket`

---

## 2026-05-14 — Fixes de producción, sesión 2

### Error campaigns silently skipped en `_activate_warming_bucket`

- **Síntoma:** Cuando el pre-calentamiento fallaba, las campañas quedaban en `error`. Al activar el bucket, `_activate_warming_bucket` las ignoraba — el bucket iniciaba en frío sin campañas.
- **Causa:** `_activate_warming_bucket` solo iteraba campañas con `status == "warming"`. Las de `error` sin `connectCampaignId` eran invisibles.
- **Fix:** Recovery loop al inicio: campañas en `error` sin `connectCampaignId` se resetean a `queued` → `_dispatch_ready_campaigns` las recoge como cold-start.
- **Archivo:** `executor.py:_activate_warming_bucket`

---

### No existía warmup cross-plan — planes encadenados y loops iniciaban en frío

- **Síntoma:** Cada vez que un plan B arrancaba (por `on_plan_complete` o loop) su primer bucket siempre iniciaba 5 min tarde.
- **Causa:** No existía lógica de pre-calentamiento para planes que aún no habían iniciado. Solo `_prestart_next_bucket` existía (dentro del mismo plan).
- **Fix:** Añadidos `_prestart_plan`, `_prestart_chained_runs`, y acción `prestart_check`. Ver `docs/FEATURES.md`.
- **Archivo:** `executor.py`, `store.py`, `handler.py`

---

### Plan corre toda la noche — `loop.endTime` no se respetaba

- **Síntoma:** Planes con `loop.endTime: "19:00"` seguían corriendo hasta las 3–4 AM.
- **Causa (doble):**
  1. `_past_daily_cutoff` usaba `ZoneInfo("America/New_York")` (observa DST). En verano (EDT = UTC-4), el cutoff disparaba a las **18:00 COT** en lugar de 19:00 — 1 hora antes de lo esperado.
  2. `_force_finish_internal` llamaba `_maybe_loop + start_run_chained` al finalizar. Con el cutoff a 18:00 y `endTime = "19:00"`, `_maybe_loop` evaluaba `true` → iniciaba un nuevo run → ciclo de ~60 reinicios rápidos entre 18:00–18:59. El último run corría 9+ horas sin detenerse.
- **Fix:** Reemplazado el check de cutoff por comparación directa contra `plan.loop.endTime` en UTC-5 fijo (COT, sin DST). Eliminadas las llamadas a `_maybe_loop` y `start_run_chained` de `_force_finish_internal`. Añadido guard `is_plan_locked` en `_maybe_loop`.
- **Archivo:** `executor.py:tick`, `executor.py:_force_finish_internal`, `executor.py:_maybe_loop`

---

## 2026-05-15 — Fixes de producción, sesión 3

### `StartCampaign` — "Campaign start time has already passed"

- **Síntoma:** `_activate_warming_bucket` y `_start_one_campaign` fallaban al llamar `StartCampaign` en campañas pre-calentadas.
- **Causa:** El jitter de EventBridge (±30–90 s) hacía que el `startTime` de la campaña (fijado 6 min en el futuro al crearla) expirara antes de que la activación llamara `StartCampaign`.
- **Fix:** `_create_campaign_only` ahora llama `StartCampaign` inmediatamente después de `CreateCampaign`. Si tiene éxito → `warmupStarted=True`; activación solo sincroniza estado sin llamadas API. Si falla → fallback con `UpdateCampaignSchedule + StartCampaign` al activar.
- **Archivo:** `executor.py:_create_campaign_only`, `_activate_warming_bucket`, `_start_one_campaign`

---

### `StartCampaign` — "Missing required campaign parameter Campaign Flow" (LI / NY)

- **Síntoma:** Campañas de los estados LI y NY en Plan 2.2 fallaban al iniciar. Plan 2.1 funcionaba.
- **Causa (doble):**
  1. Plan 2.2 no tiene `campaignFlowArn` en `campaignConfig` (Plan 2.1 sí, explícito). Sin él, `CreateCampaign` crea la campaña sin flow ARN y `StartCampaign` la rechaza.
  2. No existían flows tipo `CAMPAIGN` con nombre `campaign-LI` ni `campaign-NY` en Connect. `resolve_campaign_flow_arn` retornaba `None` → parámetro ausente. Connect habilitó la obligatoriedad del campo entre May 13–15.
- **Por qué no fallaba antes:** El error "start time already passed" (punto anterior) lo enmascaraba. El 13 de mayo, Connect aún aceptaba campañas sin flow ARN.
- **Fix:** Creados flows `campaign-LI` (`4dc64f8f-...`) y `campaign-NY` (`b3c8cf6f-...`) tipo CAMPAIGN en Connect. Añadido guard fail-fast en `_create_campaign_only` y `_create_and_start_campaign`: lanza `ValueError` antes de `CreateCampaign` si no hay flow ARN disponible.
- **Archivo:** `executor.py`; Connect (flows creados vía CLI).

---

## 2026-05-15 — Sesión 4: campaign flow ARNs stale

### Todos los `campaignFlowArn` en DynamoDB estaban eliminados de Connect

- **Síntoma:** El resolver dinámico ocultaba el problema — encontraba flows por patrones de nombre permisivos (`"CT"` en el nombre del flow, `"TX"` en el nombre). Cuando esos flows ad-hoc se eliminaran, CT y TX fallarían igual que LI/NY.
- **Causa:** Los ARNs hardcodeados en los planes apuntaban a flows creados durante el setup inicial (mayo 7–13) que luego fueron eliminados. El operador borra flows "que parecen temporales" desde la consola de Connect sin saber que los planes los referencian. Todos los 8 ARNs distintos almacenados en DynamoDB estaban `ResourceNotFoundException`.
- **Fix (triple):**
  1. `resolve_campaign_flow_arn` ahora es estricto: solo acepta flows con nombre exactamente `campaign-<STATE>`. Si no existe, lo **auto-crea** con el contenido canónico (`PutDialRequest → EndFlowExecution`) y lo tagea `do-not-delete: true`. El Lambda nunca más falla por un flow faltante.
  2. Eliminado `cfg.get("campaignFlowArn")` de `build_campaign_params` — el resolver es la única fuente de verdad, los ARNs en DDB ya no se consultan.
  3. Limpiados todos los campos `campaignFlowArn` de los 4 planes en DynamoDB que los tenían.
  4. Agregados permisos IAM `connect:CreateContactFlow` y `connect:TagResource` al role del Lambda.
- **Archivo:** `builders.py:resolve_campaign_flow_arn`, `builders.py:build_campaign_params`; IAM inline policy `FunctionRoleDefaultPolicy41A10F9C`.

---

## 2026-05-15 — Sesión 5: segmentos borrados, force-start bloqueado

### `_force_finish_internal` — `deleteAfter: false` ignorado al limpiar segmentos

- **Síntoma:** Segmentos de Customer Profiles del bucket activo (NJ-NL, CT-NL) desaparecían mientras las campañas aún estaban corriendo. Connect reportaba `Failed` con "segment definition not found". DynamoDB seguía mostrando `status: running` hasta que el próximo tick detectara el `Failed`.
- **Causa:** `_force_finish_internal` usaba `bucket_def[0].get("cleanup", True)` — si la clave `cleanup` no existe, cae al default `True` en lugar de leer `deleteAfter`. Los planes tienen `deleteAfter: false` pero no tienen clave `cleanup`, entonces el default `True` borraba los segmentos. `_advance_bucket` tenía este fallback correcto (`bucket.get("cleanup", bucket.get("deleteAfter", True))`), pero `_force_finish_internal` no.
- **Fix:** `bucket_def[0].get("cleanup", True)` → `bucket_def[0].get("cleanup", bucket_def[0].get("deleteAfter", True))`.
- **Archivo:** `executor.py:_force_finish_internal`

---

### `force_start_campaign` — bloqueado en buckets `completed`; hijos cascade-cancelados no se reseteaban

- **Síntoma:** Al hacer force-start en una campaña con `parent_cancelled`, el API retornaba error `"Bucket X is 'completed' — force-start requires an active bucket"`. Aunque el bucket tenía campañas canceladas pendientes, sus hermanas ya habían completado y el bucket estaba cerrado. Adicionalmente, aunque se lograra forzar el inicio, los descendientes con `parent_cancelled` permanecían cancelados — había que forzarlos uno por uno manualmente.
- **Causa:** `force_start_campaign` rechazaba cualquier bucket con `status != running/warming/queued`. Un bucket con `parent_cancelled` puede estar `completed` porque las otras campañas del bucket ya terminaron. La lógica de reset no existía — solo se reseteaba la campaña objetivo.
- **Fix:**
  1. Buckets `completed` ahora se permiten cuando la campaña objetivo es `cancelled`. El bucket se reactiva (`status = running`, `completedAt = None`) y se programa un nuevo tick.
  2. Nueva función `_reset_cascade_cancelled_children(run, plan, campaign_id)`: resetea recursivamente todos los descendientes con `exitReason == "parent_cancelled"` a `queued`. El dispatcher los inicia automáticamente a medida que el padre vaya completando, restaurando la cadena sin intervención manual en cada uno.
- **Archivo:** `executor.py:force_start_campaign`, `executor.py:_reset_cascade_cancelled_children` (nueva)

---

## 2026-05-18 — Sesión 6: auditoría de raíz de bugs resueltos

### `abort_run` — campaña en `creating` no se cancelaba al abortar el run

- **Síntoma (potencial):** Si un run era abortado mientras una invocación concurrente estaba en la fase 1 del claim (`creating`), la campaña quedaba huérfana en estado `creating` en DynamoDB. El próximo tick de ese run (que ya no existiría) nunca llegaría, y la campaña jamás avanzaría a `running` ni sería cancelada.
- **Causa:** `abort_run` iteraba campañas con `if cs["status"] in ("running", "warming", "queued")` — la misma omisión que se había detectado y corregido en `_force_finish_internal`. El estado `creating` (two-phase DynamoDB claim) no estaba incluido.
- **Fix:** Añadido `"creating"` al tuple de estados en `abort_run`. Test `test_abort_run_cancels_creating_campaigns` añadido.
- **Archivo:** `executor.py:abort_run`

---

<!-- APPEND NUEVOS BUGS ABAJO DE ESTA LÍNEA -->
