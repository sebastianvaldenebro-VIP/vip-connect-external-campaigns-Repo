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

## 2026-05-19 — Sesión 7: timezone mismatch en campaign end-time

### `ValidationException: Schedule end time needs to be greater than start time`

- **Síntoma:** Plan 2.1 New Lead v.2 (1a29f025) fallaba en todos los buckets del run `1779141574470-0911f796` a las 22:56–22:57 UTC (17:56–17:57 COT) con `ValidationException: Schedule end time needs to be greater than start time`. Los 4 buckets entraban en `error` simultáneamente.
- **Causa raíz — timezone mismatch:** `_daily_cutoff_iso()` y `_past_daily_cutoff()` usaban `America/New_York` (EDT en verano = UTC-4), mientras que todos los demás guards de tiempo en `tick()` (`workingHours.endTime`, `loop.endTime`) usan COT (UTC-5 fijo). Esto creaba una ventana de ~1 hora donde:
  - `tick()` veía COT 17:56 < 19:00 → seguía ejecutando
  - `_daily_cutoff_iso()` retornaba 7 PM EDT = **23:00 UTC**
  - `_create_campaign_only` calculaba `startTime = now + 6 min = 23:02 UTC`
  - `startTime (23:02) > endTime (23:00)` → Connect lanzaba `ValidationException`
  - Ventana rota: 22:54–00:00 UTC (17:54–19:00 COT) — toda la última hora del plan.
- **Bug secundario:** `_past_daily_cutoff()` con Eastern disparaba a las 23:00 UTC = **18:00 COT** para planes sin `loop.endTime` — forzaba el cierre 1 hora antes de lo esperado.
- **Fix (3 cambios en `executor.py`):**
  1. `_daily_cutoff_iso()` — reemplazado `ZoneInfo("America/New_York")` por COT (UTC-5 fijo). 7 PM COT = 00:00 UTC, consistente con el resto del sistema.
  2. `_past_daily_cutoff()` — mismo cambio. Ahora dispara a 19:00 COT = 00:00 UTC.
  3. `_DAILY_CUTOFF_HOUR_EST` renombrado a `_DAILY_CUTOFF_HOUR` y comentario actualizado.
  4. Guard en `_create_campaign_only`: si `start_dt >= end_dt` lanza `ValueError` descriptivo en lugar de dejar que Connect retorne un error opaco.
- **Archivo:** `executor.py:_daily_cutoff_iso`, `executor.py:_past_daily_cutoff`, `executor.py:_create_campaign_only`

---

## 2026-05-19 — Sesión 8: `_EmptySegmentError` retry no implementado

### MD-NL_1 cancelled `skipped_empty` sin reintentos durante rebuild parcial de Redis

- **Síntoma:** MD-NL_1 (Plan 2.2, bucket "1st Attempt TX/MD") apareció como `cancelled / skipped_empty` a los pocos segundos de activarse el bucket. `reconcileRetries=None`, sin reintentos. Los logs mostraban 3 warnings `"Redis rebuilding, will retry next tick"` seguidos de silencio y cancelación permanente.
- **Causa raíz — triple:**
  1. **Rebuild parcial de Redis:** Durante el rebuild, los leads de otros estados (NJ, CT) cargaban primero. `is_ready()` (LLEN > 0) pasaba, pero los filtros por estado MD retornaban 0 matches → `_EmptySegmentError`.
  2. **Bug en `_start_one_campaign`:** La excepción `_EmptySegmentError` tenía `break  # Empty segment is definitive — no retries` — cancelación permanente inmediata, sin respetar `reconcileRetryLimit`. El fix del BUGLOG 2026-05-13 que mencionaba reintentos **nunca se implementó** (o fue sobreescrito).
  3. **While-loop amplification:** El `while changed` en `tick()` re-invocaba `_dispatch_ready_campaigns` en cada iteración. Cada vez que `_RedisRebuildingError` hacía `return` con `cs["status"]="queued"`, el loop volvía a intentar inmediatamente. 3 `_RedisRebuildingError` rápidos → Redis parcialmente listo → `_EmptySegmentError` → cancel.
- **Fix:** `_EmptySegmentError` en `_start_one_campaign` y `_prestart_next_bucket` ahora usa `cs["reconcileRetries"]` como contador cross-tick: si `retries < reconcileRetryLimit (default 2)` → incrementa, pone `queued`, hace `return` (igual que `_RedisRebuildingError`). Solo cancela permanentemente al agotar reintentos.
- **Archivo:** `executor.py:_start_one_campaign` (~1583), `executor.py:_prestart_next_bucket` (~931)

---

### B1-A — `_dispatch_ready_campaigns` — `save_run` (claim) después de la call a Connect, no antes

- **Síntoma (potencial):** Dos invocaciones concurrentes del tick podían ambas ver una campaña en `queued`, ambas llegar a `_start_one_campaign`, y crear campañas Connect duplicadas. El two-phase claim (`creating`) ya existía en `_dispatch_cross_bucket_ready` pero **no** en `_dispatch_ready_campaigns` (path del bucket actual).
- **Causa:** El `save_run` con estado `creating` ocurría *después* de `_start_one_campaign`, no antes. La ventana de carrera era toda la call a Connect.
- **Fix:** Orden de fases en `_dispatch_ready_campaigns`: (3) `save_run` con campaña en `creating` → (4) `_start_one_campaign` → (5) `save_run` confirm. Si Phase 3 lanza `ConcurrentWriteError`, Phase 4 nunca se ejecuta.
- **Archivo:** `executor.py:_dispatch_ready_campaigns`

---

### B1-B — `_dispatch_cross_bucket_ready` — estado del bucket mutado antes de confirmar schedule

- **Síntoma (potencial):** Si `_schedule_tick` (EventBridge) fallaba al activar el siguiente bucket en la cadena, el bucket ya había sido marcado como `running` en DynamoDB. Ticks posteriores lo veían como activo pero sin schedule, dejando el plan atascado.
- **Causa:** El cambio de estado del bucket se aplicaba antes de confirmar que `_schedule_tick` tuvo éxito. Fallo en EventBridge → estado inconsistente persiste.
- **Fix:** Si `_schedule_tick` lanza, la excepción se propaga antes de `save_run`. El bucket permanece en `queued`. Rollback implícito.
- **Archivo:** `executor.py:_dispatch_cross_bucket_ready`

---

### B1-C — `_force_finish_internal` — lock no liberado si `save_run` falla

- **Síntoma (potencial):** Si `save_run` lanzaba `ConcurrentWriteError` dentro de `_force_finish_internal`, `unlock_plan_run` no se ejecutaba. El plan quedaba permanentemente bloqueado — ningún tick futuro podía procesarlo.
- **Causa:** `unlock_plan_run` no estaba en un bloque `finally`. Se alcanzaba solo si `save_run` tenía éxito.
- **Fix:** `unlock_plan_run` envuelto en `finally` en `_force_finish_internal`. Garantizado incluso cuando `save_run` lanza.
- **Archivo:** `executor.py:_force_finish_internal`

---

### B1-D — `_start_bucket` — continúa despachando campañas si `_schedule_tick` falla

- **Síntoma (potencial):** Si EventBridge fallaba al crear el schedule del bucket, `_start_bucket` continuaba hacia `_dispatch_ready_campaigns` y creaba campañas Connect sin schedule activo. Las campañas existían en Connect pero el tick nunca volvería a invocarse para ese bucket.
- **Causa:** La excepción de `_schedule_tick` no se propagaba; el flujo continuaba hacia el despacho de campañas.
- **Fix:** `_start_bucket` propaga la excepción de `_schedule_tick`. `_dispatch_ready_campaigns` no se llama si el schedule falla.
- **Archivo:** `executor.py:_start_bucket`

---

### B2-C — `_fire_bucket_chains` — ignora `workingHours` y el flag `isTemplate`

- **Síntoma:** Un plan downstream con trigger `on_plan_complete` se iniciaba aunque estuviera fuera de su ventana `workingHours`. También, planes marcados `isTemplate: true` se iniciaban como runs reales al completar el plan upstream.
- **Causa:** `_fire_bucket_chains` llamaba `start_run` directamente sin verificar `_within_working_hours` ni el flag `isTemplate`.
- **Fix:** Dos guards en `_fire_bucket_chains`: skip si `_within_working_hours(plan)` es `False`; skip si `plan.get("isTemplate")` es `True`.
- **Archivo:** `executor.py:_fire_bucket_chains`

---

### B2-D — `force_start_campaign` — `startedAt` no se resetea al reactivar bucket `completed`

- **Síntoma:** Al hacer force-start en una campaña de un bucket `completed`, el bucket se reactivaba (`status=running`) pero conservaba el `startedAt` original (horas atrás). `elapsed_min` calculaba tiempo transcurrido incorrecto → el bucket podía cerrarse inmediatamente por duración excedida antes de que las campañas terminaran.
- **Causa:** `force_start_campaign` seteaba `status=running` y `completedAt=None` pero no reseteaba `startedAt`.
- **Fix:** `startedAt` se resetea a `_now_iso()` cuando se reactiva un bucket `completed`.
- **Archivo:** `executor.py:force_start_campaign`

---

### B3-A — `_get_campaign_state` — `ResourceNotFoundException` devuelve `'Unknown'` en vez de `'Deleted'`

- **Síntoma:** Si una campaña Connect era borrada manualmente, `_poll_campaign_state` la veía como `'Unknown'`. `'Unknown'` no estaba en `_CONNECT_TERMINAL`, así que la campaña permanecía en `running` indefinidamente. El run nunca completaba.
- **Causa:** `_get_campaign_state` capturaba todas las `ClientError` con el mismo return `'Unknown'`, sin distinguir `ResourceNotFoundException` de errores transitorios.
- **Fix:** `ResourceNotFoundException` → return `'Deleted'`. `'Deleted'` está en `_CONNECT_TERMINAL` → campaña se cancela correctamente y el run avanza.
- **Archivo:** `executor.py:_get_campaign_state`

---

### B3-B — `_within_working_hours` — el default de `endTime` excluía las 23:59

- **Síntoma:** Planes con `workingHours.endTime` ausente dejaban de ejecutarse a las 23:59 COT — un minuto antes de medianoche — aunque se esperara que corrieran toda la noche.
- **Causa:** El default para `endTime` ausente era `"23:59"` (1439 minutos). La comparación `now_min < end_hhmm` a las 23:59 evaluaba `1439 < 1439` → `False` → plan marcado como fuera de horario.
- **Fix:** Default cambiado a `"24:00"` (1440 minutos). `23:59 (1439) < 1440` → `True`. El comentario en código documenta: `# 24:00 = 1440 min > 23:59`.
- **Archivo:** `executor.py:_within_working_hours`

---

### B3-C — `prestart_check` — doble llamada a `list_plans` en la misma invocación

- **Síntoma (potencial):** `prestart_check` llamaba `list_plans()` dos veces por invocación: una para el loop de prestart y otra para el loop de stuck-run detection. El doble scan DynamoDB duplicaba latencia y costo, y abría una ventana donde los dos snapshots de planes podían diferir.
- **Causa:** Los dos loops se implementaron de forma independiente, cada uno con su propio `list_plans()`.
- **Fix:** Un único `list_plans()` compartido al inicio de `prestart_check`, reutilizado por ambos loops. Línea 1171: `# Single scan shared by both the prestart and stuck-run loops below`.
- **Archivo:** `executor.py:prestart_check`

---

### B3-D — `_poll_campaign_state` — no almacenaba detalle del error en `errorDetail`

- **Síntoma:** Campañas que terminaban en `error` no tenían ninguna indicación del estado Connect que causó el fallo. El campo `errorDetail` permanecía `None`. Los operadores debían ir a CloudWatch Logs para saber qué pasó.
- **Causa:** `_poll_campaign_state` actualizaba `status` y `exitReason` pero no escribía `errorDetail` cuando el estado terminal era de error.
- **Fix:** Cuando el estado Connect es terminal y el `exit_reason` es `REASON_ERROR`, se escribe `cs["errorDetail"] = f"Connect campaign failed (state: {state})"`.
- **Archivo:** `executor.py:_poll_campaign_state`

---

<!-- APPEND NUEVOS BUGS ABAJO DE ESTA LÍNEA -->

---

## 2026-05-21 — Sesión 11: bugs estructurales del executor

### S11-A — `connect_deleted` falso etiquetado en campañas que completaron correctamente

- **Síntoma:** Campañas que habían terminado en `completed` aparecían como `connect_deleted` tras el próximo tick, disparando la guarda S-11-A que abortaba el run.
- **Causa:** `_advance_bucket` eliminaba las campañas de Connect (cleanup) **antes** de persistir los estados terminales en DynamoDB. Si `save_run` fallaba con `ConcurrentWriteError` en ese momento, el siguiente tick re-consultaba Connect, encontraba las campañas en estado `Deleted`, y sobreescribía los estados correctos con `connect_deleted`. La guarda S-11-A lo interpretaba como borrado externo y abortaba un run que había completado legítimamente.
- **Fix:** Llamada a `save_run(run)` **antes** del bloque de cleanup en `_advance_bucket`. Los estados terminales quedan persitidos con `bucket.status = "running"` (retriable si falla). Solo después se eliminan los recursos de Connect.
- **Archivo:** `executor.py:_advance_bucket`

---

### S11-B — Cascade-cancel de dependientes por status del padre

- **Síntoma:** Campañas cuyo padre terminaba en `cancelled` o `error` eran automáticamente canceladas con `parent_cancelled`, impidiendo que corrieran. Operadores tenían que force-start manualmente cada dependiente.
- **Causa:** `_dispatch_ready_campaigns` Phase 2 contenía un bloque de cascade-cancel que propagaba cualquier estado terminal no-`completed` del padre a los hijos. El diseño original asumía que solo `completed` era un resultado "exitoso".
- **Fix:** Eliminado el bloque de cascade-cancel. Un dependiente se desbloquea cuando todos sus padres están en cualquier estado terminal (`completed`, `cancelled`, `error`, `expired`). El dependiente decide por sí mismo si tiene leads al intentar iniciar.
- **Archivo:** `executor.py:_dispatch_ready_campaigns`

---

### S11-C — `ThrottlingException` / `ServiceQuotaExceededException` marcaba campaña como error permanente

- **Síntoma:** Si Connect retornaba throttling o quota exceeded al crear una campaña, ésta quedaba en `error` permanentemente. El operador tenía que force-start manualmente.
- **Causa:** El `except ClientError` en `_start_one_campaign` trataba todos los errores de cliente igual — marcaba la campaña como `error` con `exitReason = creation_failed`. No distinguía errores transitorios (rate limit) de errores definitivos (configuración inválida).
- **Fix:** Nuevo `except ClientError` explícito antes del genérico: si el código es `ThrottlingException` o `ServiceQuotaExceededException`, revierte la campaña a `queued` (sin eliminar el segmento ya creado) y emite alerta SNS. El siguiente tick reintenta automáticamente.
- **Archivo:** `executor.py:_start_one_campaign`

---

### S11-D — `force_start_campaign` rechazaba campañas `queued` en bucket `completed`

- **Síntoma:** Al intentar force-start en una campaña `queued` dentro de un bucket ya `completed`, el API retornaba `"Bucket X is already completed — force-start only allowed for cancelled campaigns in a completed bucket"`.
- **Causa:** Este estado inconsistente (campaña `queued` en bucket `completed`) podía ocurrir cuando el cascade-cancel fallaba parcialmente por un `ConcurrentWriteError` — la campaña no alcanzó a ser cancelada pero el bucket sí cerró. La validación de `force_start_campaign` solo permitía `cancelled` en buckets completados.
- **Fix:** Condición ampliada a `cs["status"] not in ("cancelled", "queued")` para buckets `completed`. Si el estado es `queued`, se loguea un warning de estado inconsistente y se procede igual que con `cancelled` (reactiva el bucket, programa nuevo tick).
- **Archivo:** `executor.py:force_start_campaign`

---

## 2026-05-19 — Sesión 9

### S9-A — Botón skip silenciosamente fallaba — doble causa raíz

- **Síntoma:** Operador presiona "Skip" en una campaña activa. La UI no da feedback. La campaña sigue en el mismo estado. DevTools muestra 409 o sin respuesta.
- **Causa raíz 1 (backend):** `skip_campaign` no tenía retry ante `ConcurrentWriteError`. El tick de EventBridge (cada ~60 s) corre concurrente con el HTTP del skip. El tick gana la escritura DynamoDB optimista → `save_run` en el skip tira `ConcurrentWriteError` → el handler devolvía 409. Sin retry, la operación simplemente fallaba.
- **Causa raíz 2 (frontend):** `campaignActionMutation` (que incluye skip, stop, force-start, etc.) no tenía `onError`. El 409 llegaba a React Query, pero como no había handler, el error se descartaba silenciosamente — cero feedback al usuario.
- **Fix backend:** `skip_campaign` tiene ahora un loop de hasta 3 reintentos. En cada intento re-lee el run desde DDB. Si el tick avanzó la campaña a terminal durante el retry, retorna sin error (éxito implícito). Si se agotan los 3 reintentos, propaga 409.
- **Fix frontend:** `campaignActionMutation` ahora tiene `onError` que guarda el mensaje en `actionError` (state local). Se renderiza un banner rojo dismissable con el texto del error.
- **Archivos:** `executor.py:skip_campaign`, `frontend/src/pages/PlanDetail.tsx`

---

### S9-B — Borrar bucket dejaba `dependsOn` huérfanos → rechazo del backend

- **Síntoma:** Al borrar un bucket en el editor del plan, el backend rechazaba el save con `"Campaign 'X' depends on unknown campaign 'Y'"`.
- **Causa:** `removeBucket` solo filtraba el bucket del array pero no limpiaba las referencias `dependsOn` que otros campaigns en otros buckets tenían hacia campaigns del bucket eliminado. `_validate_dag` en el backend rechazaba el plan por tener referencias a IDs inexistentes.
- **Fix:** `removeBucket` ahora recoge los IDs de campaigns del bucket eliminado y los purga de los `dependsOn` de todos los campaigns restantes.
- **Archivo:** `frontend/src/pages/PlanNew.tsx:removeBucket`

---

### S9-C — `force_start_campaign` Phase 1 conservaba `connectCampaignId` viejo → error falso → duplicado en Connect

- **Síntoma:** Al presionar play en una campaña, ésta aparecía como `error` o `cancelled` aunque Connect la tenía como `Running`. Si el operador volvía a presionar play, se creaba una segunda campaña en Connect para el mismo slot.
- **Causa:** `force_start_campaign` Phase 1 guardaba en DDB `{creating, old_connectCampaignId}`. Phase 2 borraba ese campaign de Connect. Phase 3 creaba uno nuevo. Si el Lambda crasheaba antes del save final, DDB quedaba con el ID viejo (ya eliminado). El siguiente tick reseteaba a `queued` y llamaba `_start_one_campaign` con el ID viejo → Connect devolvía "not found" → campaign marcada `error`. El operador daba play de nuevo → duplicado.
- **Fix:** Phase 1 ahora captura el ID viejo en una variable local y escribe `connectCampaignId=None` en el save atómico. Si Phase 3 crashea antes del save final, DDB tiene `{creating, null}` → el recovery puede resetear a `queued` limpiamente sin intentar un ID stale.
- **Archivo:** `executor.py:force_start_campaign`

---

### S9-D — Ventana de crash entre Connect API y save final → ID huérfano → duplicado en recovery

- **Síntoma (potencial):** En cualquier función que cree una campaña Connect y luego guarde el ID en DDB, si el Lambda crashea en esa ventana estrecha, el campaign existe en Connect pero DDB no sabe su ID. El siguiente tick resetea el estado a `queued` y crea otro campaign → duplicado.
- **Causa:** `_start_one_campaign` llamaba `_create_and_start_campaign` (Connect API) y luego dependía de un único save externo (Phase 5 de `_dispatch_ready_campaigns` o el save final de `force_start_campaign`). No había marcador intermedio que permitiera al recovery saber que el campaign ya existía.
- **Fix:** Mid-flight save en `_start_one_campaign`: inmediatamente después de obtener el `connect_id`, hace `cs["connectCampaignId"] = connect_id`, temporalmente restaura `cs["status"] = "creating"` y llama `save_run(run)` (best-effort). Cualquier crash posterior deja DDB en `{creating, id}` — que el smart recovery puede detectar.
- **Archivo:** `executor.py:_start_one_campaign`

---

### S9-E — Phase 1 recovery de `_dispatch_ready_campaigns` era ciega — reseteaba `{creating, id}` a `queued` sin verificar Connect

- **Síntoma:** Si el mid-flight save escribía `{creating, id}` y luego el Lambda crasheaba, el siguiente tick veía `{creating, id}` y lo reseteaba ciegamente a `{queued, null}` → creaba otra campaña → duplicado.
- **Causa:** La recovery Phase 1 originalmente hacía `cs["status"] = "queued"` para cualquier `creating`, sin importar si había un `connectCampaignId` presente que señalara un campaign ya existente en Connect.
- **Fix:** Si `cs["status"] == "creating"` y existe `connectCampaignId`, consulta el estado en Connect (`_get_campaign_state`). Si está activo (Running/Paused/etc.) → restaura a `running` sin crear nada nuevo. Si terminó (Stopped/Failed/Completed/Deleted) → limpia ID y resetea a `queued` para reintentar. Si la consulta falla → resetea a `queued` (safe default).
- **Archivo:** `executor.py:_dispatch_ready_campaigns`

---

## 2026-05-19 — Sesión 10

### PEND-1 → S9-F — `force_stop_campaign` con retry ante ConcurrentWriteError *(Fix 2026-05-19)*

- **Síntoma:** Operador presiona Stop. El tick concurrente ganaba la escritura DDB → `force_stop_campaign` devolvía 409. La campaña seguía corriendo.
- **Fix:** Loop de hasta 3 reintentos: re-lee run desde DDB, re-aplica mutaciones, reintenta `save_run`. Si la campaña ya está en estado terminal al reintentar, retorna éxito implícito.
- **Archivo:** `executor.py:force_stop_campaign`

---

### PEND-2 → S9-G — `abort_run` con retry ante ConcurrentWriteError *(Fix 2026-05-19)*

- **Síntoma:** Operador presiona Abort. Tick concurrente → 409 → run seguía corriendo.
- **Fix:** Loop de hasta 3 reintentos con semántica cuidadosa de `unlock_plan_run`: se llama exactamente una vez — en el último intento fallido, en error inesperado, o en éxito. Nunca en reintento intermedio.
- **Archivo:** `executor.py:abort_run`

---

### PEND-3 → S9-H — `force_finish_run` con retry ante ConcurrentWriteError *(Fix 2026-05-19)*

- **Síntoma:** Force Finish fallaba con 409 ante escritura concurrente del tick.
- **Fix:** Loop de hasta 3 reintentos. Re-lee run antes de cada intento; si ya completó/abortó, retorna éxito.
- **Archivo:** `executor.py:force_finish_run`

---

### S10-A — `_prestart_next_bucket` sin claim save ni mid-flight save → duplicado en warming *(Fix 2026-05-19)*

- **Síntoma (potencial):** Lambda crashea después de que `_create_campaign_only` crea el campaign de warming en Connect pero antes del `save_run` del caller. DDB queda con `bucket.status=queued`. Siguiente tick: `_next_bucket_warming` devuelve False → `_prestart_next_bucket` corre de nuevo → crea un segundo campaign Connect para el mismo slot de warming → duplicado.
- **Causa:** `_prestart_next_bucket` dependía de un único save externo en el caller (línea 379 del tick). No había claim intermedio que previniera re-entrada tras crash.
- **Fix:** Dos niveles de protección:
  1. **Claim save:** Inmediatamente tras `next_bucket_state["status"] = "warming"`, hace `save_run(run)` antes de entrar al loop. Si este save falla, revierte a `"queued"` y retorna — no se crea ningún campaign. El siguiente tick ve `"queued"` y lo reintenta.
  2. **Mid-flight save:** Dentro del loop, tras cada `_create_campaign_only` exitoso, hace `save_run(run)` (best-effort) con el `connectCampaignId` ya escrito. Crash posterior → DDB tiene `{warming, id}` → `_prestart_next_bucket` no re-entra porque `status != queued`.
- **Tests:** `test_prestart_claim_save_persists_warming_before_campaigns`, `test_prestart_mid_flight_save_persists_connect_id`, `test_prestart_claim_save_failure_reverts_to_queued`
- **Archivo:** `executor.py:_prestart_next_bucket`

---

### S10-B — Mutations de PlanDetail sin `onError` → errores de backend silenciosos *(Fix 2026-05-19)*

- **Síntoma:** Abort, Force Finish y Bucket actions fallaban sin ningún feedback visual. El operador no sabía si la acción había llegado al servidor.
- **Causa:** `abortMutation`, `forceFinishMutation`, `bucketActionMutation` en `PlanDetail.tsx` no tenían `onError`. React Query descartaba el error silenciosamente.
- **Fix:** Los tres mutation hooks ahora tienen `onError: (err) => setActionError(err.message)` y `onSuccess: () => setActionError(null)`. El banner rojo existente (ya usado para `campaignActionMutation`) ahora también cubre estas acciones.
- **Nota:** `saveMutation` en `PlanNew.tsx` ya tenía `saveMutation.isError` renderizado en JSX — no requería `onError` callback.
- **Archivos:** `frontend/src/pages/PlanDetail.tsx`

---

### S10-C — `removeCampaign` no purgaba `dependsOn` intra-bucket → siblings stuck en queued *(Fix 2026-05-19)*

- **Síntoma:** Al eliminar un campaign de un bucket, sus siblings dentro del mismo bucket que lo listaban en `dependsOn` quedaban stuck en `queued` en runtime — el executor buscaba el ID eliminado, no lo encontraba, y los siblings nunca podían arrancar.
- **Causa:** `removeCampaign` filtraba el campaign del array pero no limpiaba las referencias `dependsOn` de los siblings restantes en el mismo bucket. `removeBucket` sí hacía la limpieza cross-bucket (fix S9-B), pero `removeCampaign` no hacía lo equivalente intra-bucket.
- **Fix:** `removeCampaign` ahora captura el ID del campaign eliminado y hace `.map(c => ({ ...c, dependsOn: c.dependsOn.filter(d => d !== removedId) }))` sobre los campaigns restantes del mismo bucket.
- **Archivo:** `frontend/src/pages/PlanNew.tsx:removeCampaign`

---

### S10-D — `force_start_campaign` concurrente con tick → Phase 1 Recovery resetea claim activo → duplicado Connect *(Fix 2026-05-19)*

- **Síntoma:** Operador corre NJ manualmente mientras LI todavía está running. Cuando LI termina, el sistema crea un segundo Connect campaign para NJ en lugar de reconocer que ya estaba running.
- **Causa raíz:** Secuencia de eventos:
  1. `force_start_campaign` Phase 1 guarda `{NJ=creating, conn_id=null}` en DDB (para limpiar el ID viejo).
  2. LI completa → tick de bucket 11 llama `_advance_bucket` → ve bucket 12 en `"running"` → llama `_dispatch_ready_campaigns(run, plan, 12)`.
  3. Phase 1 Recovery ve `{creating, conn_id=null}` → resetea NJ a `"queued"` (no puede distinguir entre un claim activo de `force_start` y un claim stale de un tick crasheado).
  4. Phase 2 ve NJ=`"queued"` con LI completado → lo agrega a `newly_ready` → crea Connect campaign duplicado.
- **Fix:** Agregar `creatingAt = _now_iso()` en cada punto que hace un claim `"creating"` (force_start Phase 1, dispatcher Phase 3, cross-bucket dispatcher). Phase 1 Recovery solo resetea claims con más de 300 segundos de antigüedad — claims frescos son de un `force_start` activo y se dejan intactos.
- **Tests:** `test_dispatch_recovery_skips_fresh_creating_claim_no_conn_id`, `test_dispatch_recovery_resets_stale_creating_claim_no_conn_id`, `test_dispatch_recovery_resets_creating_no_conn_id_when_no_timestamp`
- **Archivos:** `executor.py:_dispatch_ready_campaigns` (Phase 1 Recovery), `executor.py:force_start_campaign` (Phase 1), `executor.py:_dispatch_cross_bucket_ready`

### S10-F — `_start_one_campaign` mid-flight save sin retry → Connect campaign huérfano → duplicado *(Fix 2026-05-20)*

- **Síntoma:** Dos Connect campaigns con el mismo estado/grupo visibles simultáneamente (ej. `20-5-26-TX-NL-1742` y `20-5-26-TX-NL-1747`). El run de DDB sólo trackea el segundo.
- **Causa raíz:** `_start_one_campaign` crea el Connect campaign, luego intenta el mid-flight save `{creating, connectCampaignId}`. Si ese save falla con `ConcurrentWriteError` (tick concurrente de otro bucket incrementó `_version`), el handler swallows la excepción sin retry. El save final de Phase 5 también falla (la versión ya cambió nuevamente). DDB queda en `{creating, connectCampaignId=null}` — el primer Connect campaign (1742) es huérfano. Después de 5 min (threshold S10-D), `force_start` crea un segundo campaign (1747) sin saber que 1742 existe.
- **Fix:** El mid-flight save en `_start_one_campaign` ahora reintenta hasta 3 veces en `ConcurrentWriteError`. En cada reintento: re-leer `_version` del run (sin reemplazar el estado en memoria). El caso típico — tick concurrente en bucket distinto — se resuelve con el bump de versión. Si los 3 intentos fallan, Phase-5 outer save sigue siendo el fallback.
- **Tests:** `test_start_one_campaign_mid_flight_save_retries_on_concurrent_write`, `test_start_one_campaign_mid_flight_save_exhausts_retries_logs_warning`
- **Archivos:** `executor.py:_start_one_campaign` (Phase 2 mid-flight save)

### S10-G — `_safe_delete_campaign` no elimina campaigns en estado `Completed` *(Fix 2026-05-20)*

- **Síntoma:** Campaigns de Connect se acumulan después de cada run. El setting `cleanup=True` del bucket no tiene efecto visible.
- **Causa raíz:** `_safe_delete_campaign` verifica el estado y llama `stop_campaign` si el campaign no está en `("Stopped", "Failed", "Created")`. Connect devuelve `ConflictException` al intentar detener un campaign en estado `COMPLETED`. Esa excepción es capturada por el `except` wrapper, se loguea como warning, y la función retorna **sin llamar `delete_campaign`**. Resultado: ningún campaign que completa naturalmente es eliminado.
- **Fix:** Agregar `"Completed"` a la lista de estados que omiten el `stop_campaign` call. El `delete_campaign` se ejecuta directamente.
- **Tests:** `test_safe_delete_campaign_skips_stop_for_completed`, `test_safe_delete_campaign_stops_before_delete_for_running`
- **Archivos:** `executor.py:_safe_delete_campaign`

### S11-G — Edit Plan: UI muestra datos viejos inmediatamente después de guardar *(Fix 2026-05-22)*

- **Síntoma:** El usuario guarda un plan editado, es redirigido a PlanDetail, pero la pantalla muestra los datos anteriores (nombre, trigger, buckets sin cambios). Los datos nuevos aparecen solo después de un segundo o dos, o nunca si se alejaba rápidamente.
- **Causa raíz:** El `onSuccess` de `saveMutation` en `PlanNew.tsx` llamaba `invalidateQueries({ queryKey: ['plans'] })` y luego `navigate`. Con `staleTime: 30_000` global y `refetchOnWindowFocus: false`, PlanDetail montaba con el dato stale del cache mientras esperaba el background refetch (~300-500ms de flash de datos viejos). La mutación retornaba el plan actualizado pero el código lo ignoraba para seedear el cache.
- **Fix:** `qc.setQueryData(['plans', plan.planId], { plan, latestRun: prev?.latestRun })` antes del navigate. PlanDetail recibe datos frescos en el primer render, sin esperar refetch.
- **Archivo:** `frontend/src/pages/PlanNew.tsx:saveMutation.onSuccess`

---

### S11-F — `duration_minutes` y campos numéricos viajan como string por bug de serialización Decimal → str *(Fix 2026-05-22)*

- **Síntoma:** `PUT /plans/{id}` devuelve `INTERNAL_ERROR: Unexpected error` al guardar un plan con buckets `time_based`. La UI muestra "Unexpected error" en la pantalla de Edit Plan.
- **Causa raíz:** boto3 DynamoDB resource deserializa todos los números como `Decimal`. `vip_shared.json_response` usa `json.dumps(..., default=str)`, lo que convierte `Decimal('30')` en `"30"` (string JSON). El frontend almacena el valor como string y lo retorna en el PUT body. `_validate_dag` hacía `duration < 10` → `TypeError: '<' not supported between instances of 'str' and 'int'`. El mismo bug afectaba `bandwidthAllocation`, `dialingCapacity`, y cualquier otro campo numérico de buckets/campaigns.
- **Fix (dos capas):**
  1. `store._normalize()` — helper recursivo que convierte `Decimal` → `int`/`float` en cualquier dict/list. Se aplica en `_plan_from_item` (read path) y en `put_plan` (write path).
  2. `handlers/plans._validate_dag` — `int()` defensivo en `duration_minutes` como segunda línea de defensa.
- **Alcance:** DynamoDB scan confirmó que los 350 planes existentes tienen `duration_minutes` como tipo `N` (número), no `S` (string). El problema era exclusivo del round-trip API → frontend → API.
- **Archivos:** `store.py:_normalize`, `store.py:_plan_from_item`, `store.py:put_plan`, `handlers/plans.py:_validate_dag`

---

### S11-E — `_create_and_start_campaign` sin guard de cutoff → `ValidationException` al crear campaign cerca de las 7 PM COT *(Fix 2026-05-22)*

- **Síntoma:** SNS alert: `"Campaign 'NJ-NL' failed to start (Connect error ValidationException). Error: Schedule end time needs to be greater than start time."` La campaña NJ-NL fallaba si el tick ocurría dentro de los 6 minutos previos al corte diario (7 PM COT = midnight UTC).
- **Causa raíz:** `_create_campaign_only` (warmup) tenía guard `if start_dt >= end_dt: raise ValueError`. Pero `_create_and_start_campaign` (cold-start, path de `_start_one_campaign`) **no tenía este guard**. Cuando `now + 6min > 7 PM`, Connect devuelve `ValidationException: Schedule end time needs to be greater than start time`.
- **Fix:** Agregar el mismo guard en `_create_and_start_campaign`. Introducir `_CutoffTooCloseError` (nueva excepción) para distinguir este caso de otros `ValueError`. `_start_one_campaign` captura `_CutoffTooCloseError` → marca campaign `expired` con `exitReason=cutoff_too_close` (no `error`). `_prestart_next_bucket` captura `_CutoffTooCloseError` → deja campaign en `queued` (sin cambio de estado).
- **Tests:** `test_create_and_start_campaign_guard_raises_when_start_gte_end`, `test_start_one_campaign_cutoff_too_close_marks_expired`
- **Archivos:** `executor.py:_create_and_start_campaign`, `executor.py:_start_one_campaign`, `executor.py:_prestart_next_bucket`, `executor.py:_CutoffTooCloseError`

---

## 2026-05-26 — sesión 13

### S13-A — `removeCampaign` no limpiaba referencias `dependsOn` cross-bucket → plan irguardable *(Fix 2026-05-26)*

- **Síntoma:** Al editar un plan y eliminar una campaign de un bucket, al intentar guardar el backend devolvía `"Campaign 'X' depends on unknown campaign 'Y'"`. El plan en DynamoDB quedaba intacto (el save fallido no escribió nada), pero el usuario no podía guardar ninguna modificación.
- **Causa raíz:** `removeCampaign` dentro de `BucketEditor` (`PlanNew.tsx`) limpiaba referencias `dependsOn` solo dentro del mismo bucket (via `remaining = bucket.campaigns.filter(...).map(...)`). Llamaba `onChange({ ...bucket, campaigns: remaining })` que se resolvía en `updateBucket(i, b)` del padre, el cual simplemente reemplazaba el bucket `i` sin propagar la limpieza a otros buckets. Campaigns en buckets posteriores que tenían `dependsOn: [id_eliminado]` conservaban la referencia huérfana invisible para el usuario.
- **Fix:** `updateBucket` en `PlanNew.tsx` ahora calcula el conjunto de IDs eliminados (`removedIds = prev[i].campaigns.ids − b.campaigns.ids`) y filtra esos IDs del `dependsOn` de todos los demás buckets antes de actualizar el estado. Zero overhead cuando no hay IDs eliminados (`removedIds.size === 0` → short-circuit).
- **Cobertura:** Todas las rutas de eliminación de campaigns pasan por `updateBucket` (verificado: `removeBucket` y `moveBucket` ya tenían cleanup cross-bucket; `addBucket` y la carga inicial no requieren limpieza).
- **Archivos:** `frontend/src/pages/PlanNew.tsx` (`updateBucket`, línea 1264)

---

## 2026-05-26 — sesión 12

### S12-A — `reconcileRetryLimit: 1` hardcodeado en 5 planes → campaigns canceladas por rebuild de Redis *(Fix 2026-05-26)*

- **Síntoma:** CT-NL (y otras campañas de estados específicos) aparecían con `skipped_empty` / `No Redis records match campaign filters` después de solo 2 intentos, incluso cuando había leads disponibles. Operadores debían hacer `force-start` manual.
- **Causa raíz:** Los 5 planes activos (Plan 2.1 New Leads, All NLs, Plan 2.1 New Lead v.2, SAT Plan 2.1 New Lead, Plan 2.1 New Lead) tenían `reconcileRetryLimit: 1` explícito en todos sus buckets, anulando el default de 5 que fue subido en S11-F. Con límite=1 solo había 2 intentos totales (~2 min), insuficiente para rebuilds de Redis.
- **Fix 1:** Eliminado `reconcileRetryLimit` de los 32 buckets afectados en DynamoDB (VipAdminPlans) vía `UpdateItem`. El default del código (5) aplica ahora.
- **Fix 2:** Agregado fallback en `executor.py`: cuando se agotan los reintentos de `_EmptySegmentError`, se hace un check final de `is_ready()`. Si Redis tiene LLEN=0 en ese momento (rebuild activo), se resetean los retries a 0 y se re-encola la campaña en vez de cancelarla. Si LLEN>0 (genuinamente vacío), se cancela con `skipped_empty`. Aplica tanto en `_start_one_campaign` como en `_prestart_next_bucket`.
- **Archivos:** `executor.py` (`_check_redis_ready()`, `_start_one_campaign`, `_prestart_next_bucket`), DynamoDB `VipAdminPlans` (5 planes).

---

## 2026-05-25 — sesión 11

### S11-F — `reconcileRetryLimit` default demasiado bajo → campaigns canceladas durante rebuild de Redis *(Fix 2026-05-25)*

- **Síntoma:** Campañas NCA terminaban con `skipped_empty` y `exitReason: reconcile_exhausted` a pesar de tener leads disponibles (15 leads visibles en el preview del segmento).
- **Causa raíz:** Cuando el pipeline de ingest reconstruye la lista Redis (`wait_list:{team}:list`), hay una ventana donde `LLEN > 0` pero los leads de un estado específico (ej. NCA) aún no han sido cargados. En ese estado, `_create_segment()` lanza `_EmptySegmentError` aunque eventualmente haya leads. Con el default de 2 reintentos (3 intentos totales, 1 por minuto), el executor agotaba el límite antes de que el rebuild terminara y marcaba el campaign `cancelled/skipped_empty`.
- **Fix:** Aumentar default de `reconcileRetryLimit` de `2` a `5` (6 intentos totales = 6 minutos de ventana). Cambiado en ambos paths: `_start_one_campaign` (line 1789) y `_prestart_next_bucket` (line 1087).
- **Archivos:** `executor.py` (2 cambios: líneas 1087 y 1789)
- **Nota:** El valor sigue siendo configurable por bucket vía `reconcileRetryLimit` en la definición del plan.

---

## 2026-05 (continuación sesión 10)

### S10-E — `force_start_campaign` save final sin retry → campaign bloqueada 5 minutos en `creating` *(Fix 2026-05-19)*

- **Síntoma:** Después del fix S10-D, una campaña iniciada manualmente quedaba en `"creating"` por ~5 minutos antes de aparecer como `"running"`. El operador observó NJ-NL_4-5 stuck después de que force_start fue ejecutado concurrentemente con un tick de otro bucket.
- **Causa raíz:** `force_start_campaign` Phase 3 llama `save_run(run)` (save final) sin retry. Si un tick concurrente incrementó el `_version` de DDB entre el save de Phase 1 y el save final, el save final falla con `ConcurrentWriteError`. DDB queda en `{creating, conn_id=null, creatingAt=T}` — el campaign de Connect ya existe y está Running. Phase 1 Recovery (S10-D) respeta el claim fresco por 300 segundos antes de invocar smart recovery. Resultado: 5 minutos de delay.
- **Fix:** Agregar retry loop (hasta 3 intentos) en el save final de `force_start_campaign`. En cada retry: re-leer run de DDB, detectar si un tick ya adoptó el campaign (status=running con mismo connectCampaignId → retornar éxito), re-aplicar el snapshot de `_start_one_campaign` y reintentar el save.
- **Tests:** `test_force_start_final_save_retries_on_concurrent_write`, `test_force_start_final_save_returns_early_when_tick_already_adopted`
- **Archivos:** `executor.py:force_start_campaign` (Phase 3)

---

## 2026-05-28 — Sesión 14

### S14-A — executor.py sin logging visible en CloudWatch → errores de pre-warm invisibles *(Fix 2026-05-28)*

- **Síntoma:** Campañas NJ/LI/MD del plan "Plan 1.1 - Cancellation / No Show" arrancaron a las 7:06 AM en vez de las 7:00 AM. Solo NY arrancó puntual. Al investigar, se confirmó que el error de pre-warm (en `_prestart_plan`) fue completamente silencioso — ningún log visible en CloudWatch.
- **Causa raíz:** `executor.py` usa `logger = logging.getLogger(__name__)` (stdlib Python logging). El Lambda root logger opera en nivel WARNING, por lo que todos los `logger.info()`/`logger.error()` del executor son descartados silenciosamente. Solo los eventos de `StructuredLogger` (definido en `handler.py`) aparecen en CloudWatch. Esto dejó las funciones `_prestart_plan`, `_prestart_next_bucket`, `prestart_check` y `_activate_warming_bucket` completamente opacas.
- **Fix:** Agregar `_slog` (instancia de `StructuredLogger`) a `executor.py` con patrón `try/except ImportError` para preservar compatibilidad con el entorno de tests (que no tiene `vip_shared` en el path). Logs estructurados añadidos en:
  - `_prestart_plan`: `prestart_plan_campaign_ok`, `prestart_plan_campaign_failed` (con `error_type`), `prestart_plan_summary`
  - `prestart_check`: `prestart_check_warming_plan`, `prestart_check_plan_failed`, `prestart_check_done`
  - `_activate_warming_bucket`: `activate_warming_campaign_prewarmed`, `activate_warming_campaign_cold_started`, `activate_warming_campaign_failed`
  - `_prestart_next_bucket`: `prestart_next_bucket_start`, `prestart_next_bucket_campaign_ok`, `prestart_next_bucket_campaign_failed`, `prestart_next_bucket_redis_rebuilding`, `prestart_next_bucket_empty_segment_retry`, `prestart_next_bucket_cutoff_too_close`
- **Tests:** 150 passing (sin regresiones).
- **Archivos:** `services/api-plans/src/executor.py`

### S14-B — `_prestart_plan` no reintentaba campaigns fallidos → pre-warm parcial permanente *(Fix 2026-05-28)*

- **Síntoma:** Cuando `_prestart_plan` fallaba para algunos campaigns en el primer tick (6:54 AM), los ticks siguientes (6:55, 6:56 AM) no reintentaban los fallidos. Solo el primer campaign exitoso arrancaba a las 7:00 AM; los demás arrancaban 6 min tarde.
- **Causa raíz:** Dos problemas combinados:
  1. `_prestart_plan` tenía el guard `if target_plan.get("pendingWarmup"): return` — cualquier `pendingWarmup`, aunque parcial, bloqueaba todos los reintentos en los ticks siguientes.
  2. Las excepciones individuales por campaign (ej. `_EmptySegmentError` cuando Redis aún no tiene leads a las 6:54 AM) eran atrapadas y descartadas sin oportunidad de retry. `prestart_check` corre cada minuto en la ventana 4–6 min, pero el guard impedía aprovechar esa ventana.
- **Fix:** Cambiar el guard para que solo salte si **todos** los stage-1 campaigns ya están en `pendingWarmup`. Si hay un warmup parcial, continúa iterando solo los campaigns faltantes y hace merge con los existentes. Así cada tick de `prestart_check` (6:54, 6:55, 6:56) reintenta los que fallaron el tick anterior.
- **Tests:** 150 passing (sin regresiones). Simulación local confirma: tick 1 → NY warmed, tick 2 → NJ/LI/MD warmed, pendingWarmup final = 4 campaigns.
- **Archivos:** `services/api-plans/src/executor.py` (`_prestart_plan`)

---

## 2026-06-01 — Sesión 15

### S15-A — `pendingWarmup` stale consumido el día siguiente → plan completa en ~33s sin marcar llamadas *(Fix 2026-06-01)*

- **Síntoma:** Plan 1.1 - Cancellation / No Show (programado a las 7 AM) completó en solo 33 segundos el 1 de junio. Status=completed, exitReason=completed en los 4 campaigns, 0 llamadas realizadas.
- **Causa raíz:** El plan tiene `workingHours.days: [MON-SAT]` — no corre el domingo. El 31 de mayo (domingo) a las 6:54 AM `prestart_check` creó `pendingWarmup` con los 4 campaigns (segmentos y schedules con fecha del 31 de mayo). El `scheduled_run` de las 7 AM detectó "domingo = fuera de horario" y saltó **sin limpiar el `pendingWarmup`**. El lunes 1 de junio, `start_run` consumió ese `pendingWarmup` stale con campaigns que tenían `startTime/endTime` del 31 de mayo (ya vencidos por 24+ horas). Connect los activó y al encontrar el schedule expirado los marcó "Completed" inmediatamente → 33s de duración total, 0 dials.
- **Fix en 4 capas:**
  1. **`_prestart_plan`**: agrega `createdAt` ISO al `pendingWarmup` para permitir validación de frescura.
  2. **`start_run`**: descarta `pendingWarmup` si `createdAt` tiene más de 2 horas (WARMUP_MAX_AGE_SECONDS=7200). Emite `start_run_warmup_stale_discarded` a CloudWatch. Cubre todos los callers.
  3. **`scheduled_run`**: limpia `pendingWarmup` al skipear por `outside_working_hours`. Emite `scheduled_run_outside_hours_warmup_cleared`. Además migra todos los logs a `_slog` para visibilidad en CloudWatch.
  4. **`start_run_chained` + `_fire_bucket_chains` + `_fire_campaign_chains`**: limpian `pendingWarmup` inmediatamente cuando un plan encadenado es skipeado por `_within_working_hours`.
- **Tests:** 150 passing (sin regresiones).
- **Archivos:** `services/api-plans/src/executor.py` (5 funciones modificadas)

---

## 2026-07-01 — Sesión Progressive Branded Dialer E2E

### BD-001 — Consumer Lambda matcheaba `DefaultOutboundQueue` en vez de `InboundQueues` → ninguna campaña branded encontrada *(Fix 2026-07-01)*

- **Síntoma:** El consumer Lambda procesaba los eventos `STATE_CHANGE` correctamente pero nunca encontraba campañas branded para ningún agente disponible. Los logs mostraban que el lookup de `VipActiveBrandedCampaigns` siempre retornaba vacío.
- **Causa raíz:** `extract_agent_info` extraía solo `routing_profile.DefaultOutboundQueue` (la queue usada cuando un agente inicia una llamada saliente). Las campañas branded están registradas contra las queues de `InboundQueues` (las queues que el agente atiende). En el caso real: la campaña estaba en la VIP test queue (`eb308429`, en `InboundQueues`), pero el consumer buscaba por la `DefaultOutboundQueue` del agente (`ee129237`). El GSI nunca retornaba match.
- **Fix:** `extract_agent_info` ahora retorna tanto `queue_arn` (DefaultOutboundQueue) como `inbound_queue_arns` (lista de todas las InboundQueues). El consumer itera todas las ARNs candidatas en orden y usa la primera que tiene campañas activas como `matched_queue_arn`. El SQS message lleva el `matched_queue_arn` (queue de la campaña), no el DefaultOutboundQueue del agente.
- **Archivos:** `services/api-progressive-dialer/src/agent_event_filter.py`, `services/api-progressive-dialer/src/handler_consumer.py`

### BD-002 — `_EmptySegmentError` al seeder: Lead UUIDs de Redis ≠ CP internal ProfileIds *(Fix 2026-07-01)*

- **Síntoma:** El plan "Test Branded" / campaña "NY-NL_13" fallaba con `_EmptySegmentError` en el live monitor. El seeder invocado por el executor retornaba 0 leads.
- **Causa raíz:** `_create_segment` construía el segmento de Customer Profiles usando los `customerid` de Redis (UUIDs del CRM, ej. `abc123-uuid`) como filtro `ID.INCLUSIVE`. Amazon Customer Profiles usa sus propios ProfileIds internos (completamente distintos de los IDs del CRM). El segmento se creaba en CP correctamente pero no matcheaba ningún perfil → 0 results → seeder sin leads → `_EmptySegmentError`. El bug existía desde el inicio del feature y nunca había sido activado en producción sin `pinnedSegmentArn`.
- **Fix:** `_create_segment` ahora extrae teléfonos de los registros Redis, los normaliza a E.164 via `_normalize_phone_e164`, y construye el segmento con `PhoneNumber.INCLUSIVE` via `SegmentGroupsTranslator.phones_to_segment_groups`. CP puede filtrar por `PhoneNumber` directamente ya que ese campo viene del CRM sync. El seeder (`_extract_phones_from_filter`) lee los teléfonos del `segmentGroups` definition sin necesidad de `BatchGetProfile`.
- **Archivos:** `services/api-plans/src/executor.py` (`_normalize_phone_e164`, `_create_segment`), `services/shared/python/vip_shared/domain/services/segment_groups_translator.py` (`phones_to_segment_groups`)
- **Nota:** `redis_lead_source.py` también actualizado con `ssl=True` en el mismo batch (migración Valkey).

---

## 2026-08-10 — Investigación reporte "llamadas a California/Washington"

### BD-003 — `_normalize_phone_e164` acepta NPA inválido (0/1 inicial) → números no discables marcados en silencio *(Fix 2026-08-10)*

- **Síntoma:** Contact center reportó llamadas salientes de campañas NY/LI/NJ/CT/MD/TX a números con área code 818/323 (CA) y 425 (WA). Investigación exhaustiva (Connect CTR, Customer Profiles, Redis vía `connectcampaignRedisAuxiliar` de solo lectura, `describe-contact` sobre los Contact IDs exactos) descartó por completo la hipótesis de segmentación rota: los 3 números reportados resultaron ser callbacks **entrantes** de pacientes de California (cola "existing patient", `InitiationMethod: CALLBACK`, acumulados ~2.5 días), sin relación con ninguna campaña saliente. El filtro `location` de las 6 campañas de hoy verificó correcto en todos los `segmentName` (sin `-all-`, sin fail-open).
- **Hallazgo colateral real:** durante la investigación se detectó que 9/130 números en el segmento NY y 9/95 en TX de hoy tenían un NPA imposible (`134`, `192`, `191`, `000` — NANP prohíbe NPA que empiece en 0 o 1), producidos por `_normalize_phone_e164` (executor.py:3725) al anteponer `+1` ciegamente a un valor de 10 dígitos ya truncado/corrupto en el CRM (ej. `1347555123` → `+11347555123`). Un dial de hoy en la campaña MD falló con `TELECOM_NUMBER_INVALID` por esta causa. La alerta `segment_phones_excluded` no lo detecta porque solo dispara cuando la normalización retorna `None`; un valor malformado-pero-no-`None` pasa en silencio.
- **Causa raíz:** las 3 ramas de `_normalize_phone_e164` (10 dígitos, 11 dígitos con `1` inicial, passthrough `+...`) no validaban que el NPA resultante fuera válido (no puede empezar en 0 o 1). El origen del dato truncado en el CRM queda sin trazar (Redis es VPC-aislado, no se pudo leer el payload crudo sin invocar Lambda de escritura).
- **Fix:** las 3 ramas ahora rechazan (retornan `None`) cuando el NPA resultante empieza en `0` o `1`. 8 tests nuevos (`TestNormalizePhoneE164` en `test_executor_v2.py`) cubriendo los 4 casos del bug + 4 casos de comportamiento válido preexistente. TDD: tests fallaron antes del fix (RED), pasan después (GREEN). 315/315 tests de la suite completa sin regresiones.
- **Archivos:** `services/api-plans/src/executor.py` (`_normalize_phone_e164`, líneas 3725-3750), `services/api-plans/tests/unit/test_executor_v2.py` (`TestNormalizePhoneE164`)
- **Deploy:** `vip-admin-ui-api-plans`, 2026-08-10T16:58:24Z, confirmado `State: Active` / `LastUpdateStatus: Successful`.
- **Pendiente:** origen del dato truncado en el CRM sin identificar.

---

## 2026-08-11 — Investigación de 2 alertas SNS (alarma fallback + "campaña eliminada manualmente")

### INC-01 — Alarma `vip-plans-scheduled-run-fallback` recurrente en domingos *(Documentado, sin fix — comportamiento esperado)*

- **Síntoma:** SNS ALARM el 2026-08-09 11:57 UTC → OK 12:17 UTC, mismo patrón el 07-19, 07-26 y 08-02 (siempre domingo, ~11:57 UTC).
- **Causa raíz (ya documentada en `monitoring-stack.ts:234-241`):** El plan "Plan 1.1 - Cancellation / No Show" (`planId=339aefac...`, trigger `07:00`) pierde semanalmente el permiso de invocación de su regla EventBridge `vip-sched-339aefac...` — probablemente por un deploy de CDK que recrea el Lambda y borra el statement `add_permission` custom. `prestart_check` detecta la falta de run 80s después del trigger esperado (`no_run_found_80s_after_scheduled_trigger`, executor.py:2579-2629), dispara el run manualmente vía `scheduled_run()`, y el self-healing (`_ensure_scheduled_run_permission`) restaura el permiso para el día siguiente. Confirmado en logs: 12:01:23 UTC `prestart_fallback_triggered` para `339aefac...`, run arrancó sin intervención.
- **Impacto:** Ninguno — el plan corrió igual, solo con ~4 min de retraso máximo. Ruido operacional, no incidente.
- **Pendiente:** Investigar por qué justo este plan (de todos los que tienen trigger `time`) pierde el permiso cada semana y los demás no — podría indicar que su regla se recrea con más frecuencia que las demás, o algún proceso re-guarda su config semanalmente.

### INC-02 — Alerta "Campaign deleted externally... may have been deleted manually" era un falso positivo por race condition, no una eliminación manual *(Documentado, sin fix)*

- **Síntoma:** SNS alert `[VIP Plans] Campaign deleted externally: CT-NL_1` el 2026-08-07 18:11:58 UTC, texto sugiere eliminación manual desde la consola.
- **Causa raíz confirmada (CloudTrail + CloudWatch Logs + DynamoDB, sin ambigüedad):** Dos invocaciones concurrentes del mismo Lambda procesaron el mismo tick (`planId=6203a0b5`, `runId=1786117593426-cf53aae0`, `bucket_index=1`) con 3.7s de solapamiento:
  1. RequestId `c73b5ed4` (18:11:30–18:11:36) ejecutó el cleanup normal del bucket (`cleanup: true`) y llamó `DeleteCampaign` sobre `56a755e2` (CT-NL_1) a las 18:11:33 — acción legítima, autenticada como `vip-admin-ui-api-plans` (rol Lambda, no un usuario humano). Al guardar el nuevo estado del run recibió `tick_concurrent_write` (conflicto optimista en DynamoDB) — su actualización se perdió.
  2. RequestId `997c22d0` (18:11:57) reprocesó el mismo tick con el estado viejo (que aún mostraba la campaña como `running`), intentó `StopCampaign`/`DeleteSegmentDefinition` sobre la misma campaña ya eliminada, recibió `ResourceNotFoundException`, y `_poll_campaign_state` (executor.py:3655-3681) interpretó "no existe en Connect" como `exitReason: connect_deleted` → disparó la alerta con el texto "may have been deleted manually".
- **Verificación de descarte de causa manual:** CloudTrail en la ventana completa (17:00–19:00 UTC) muestra **cero** llamadas `DeleteCampaign`/`StopCampaign` por identidades humanas o vía la ruta de UI (`vip-admin-ui-api-campaigns`) sobre este campaign ID — únicamente la propia `vip-admin-ui-api-plans`. `AdminAuditLog` no tiene registros de `delete` manual el 07-08. El "borrado externo" fue 100% interno al sistema, solo que la segunda invocación no tenía visibilidad del cleanup que ya había hecho la primera.
- **Impacto:** Bucket 1 del plan "Plan 2.2 version 5/17" completó con CT-NL_1 en `cancelled/connect_deleted` en vez de `completed` — posible corte prematuro de leads pendientes en esa campaña. El guard de aborto por eliminación externa (`_n_deleted > 0 and _n_completed == 0`, executor.py:1192-1224) no se disparó porque la campaña hermana NJ-NL_1 sí completó normalmente, así que el run avanzó sin alertar sobre esto como anomalía de mayor severidad.
- **Fix propuesto (no aplicado):**
  1. El texto de la alerta SNS en `_poll_campaign_state` no debería sugerir "manual" cuando la causa es indistinguible entre borrado humano real y cleanup propio sobre estado stale — separar en dos `alertType` distintos o al menos verificar primero si el propio sistema ya tiene un registro de haber borrado esa campaña antes de alarmar.
  2. Revisar el manejo de `tick_concurrent_write`: hoy la invocación que pierde el conflicto simplemente termina sin reintentar con estado fresco, dejando a la siguiente invocación reprocesar con datos obsoletos. Correlacionar el `RequestId` ganador vs el perdedor ayudaría a diagnosticar más rápido.
- **Archivos relevantes:** `services/api-plans/src/executor.py` (`_poll_campaign_state:3655`, `tick_concurrent_write` handling, `_dispatch_cross_bucket_ready` — mismo patrón de race ya resuelto para creación de campañas en la sesión 2026-05-13, aquí reaparece en el cleanup/poll).

### BD-004 — Gate TCPA de horario para callbacks existe pero está desconectado en producción *(hallazgo, sin fix — fuera de este repo)*

- **Síntoma:** los 3 contactos investigados en BD-003 resultaron ser callbacks (`InitiationMethod: CALLBACK`, cola "existing patient", `queue 78a601cc...`) a pacientes de California, atendidos hoy 2026-08-10 a las 08:41-08:47 EDT = **05:41-05:47 PDT** — fuera de cualquier ventana horaria razonable para el cliente.
- **Causa raíz:** existe una infraestructura completa de validación TCPA por zona horaria del cliente (Lambda `cloudhesive-integration-callback-tz-TimezoneCheck`, mapea área code → timezone, valida ventana 8am-8pm local; Lambda `RequeueCallbacks` + regla EventBridge cada 5 min; tabla `DeferredCallbacks`), pero **el bloque que la invoca (`InvokeLambdaFunction 3c7a473d`) es inalcanzable en los 4 queue flows de producción** (`*QueueFlow`, `*QueueFlowSpanish`, `*QueueFlowRussian`, `*QueueFlowFibroid`) — 0 aristas entrantes, huérfano en el canvas. Solo es alcanzable en `*QueueFlow-Testing`. 3 fuentes independientes confirman que nunca corrió contra tráfico real: `DeferredCallbacks` con `ItemCount: 0` desde su creación (2026-06-16), el Lambda `TimezoneCheck` sin invocaciones desde 2026-08-04, y los 3 contactos investigados sin ninguno de los atributos que ese Lambda inyecta.
- **Naturaleza del bug:** es de disparo automático — `CallbackMode: AgentFirst` hace que Connect reserve al agente y marque al cliente automáticamente en cuanto el agente se pone disponible, sin verificar la hora local del cliente. El agente no tiene visibilidad de la timezone del cliente en la UI. No es un problema de proceso/entrenamiento del agente.
- **Contribuyente:** colapso de capacidad real el viernes 2026-08-07 (handled rate 24.3% vs 67-88% normal, abandonos 183 vs 37-88) + cola cerrada sábado/domingo + `MaximumConnectionAttempts: 1` sin expiración → callbacks sin atender esperan indefinidamente hasta el siguiente turno con capacidad.
- **Gap de ownership:** los 2 Lambdas, la tabla DynamoDB y la regla EventBridge de este guardrail fueron creados por CloudHesive **fuera de CDK** — invisibles a `cdk diff`, sin tests, sin IaC en este repo. TCPA/calling-hours no está documentado en ningún lugar de este repo (`hipaa-checklist.md`, `architecture-decisions.md`, `RUNBOOKS.md` — cero menciones).
- **Estado:** hallazgo de compliance, NO fix de código de este repo. Requiere decisión de negocio: reconectar el bloque en los 4 flows de producción (posible fix de bajo riesgo, requiere acceso a Connect Contact Flow designer) y corregir `CALLBACK_QUEUE_ID=""` vacío en `RequeueCallbacks` (defecto latente — si se reactiva el gate sin corregir esto, los callbacks reencolados enrutarían al flow de inbound principal en vez de a la cola original).
- **Archivos:** fuera de este repo (Connect Contact Flows administrados vía consola/CloudHesive). Ver también `docs/connect/queues-and-routing.md` (plantilla vacía, campo "Callback config" sin rellenar).

### BD-005 — Branded Monitor: 100% de agentes mostrados como "Away" por `StatusType` inexistente en la API *(Fix 2026-08-11)*

- **Síntoma:** en el frontend, Branded Monitor > Agents mostraba agentes con "Time in status" de cientos de horas (ej. 477h, 68h) en estado "Away" con alert "Extended break", incluyendo agentes que en Connect estaban `Available` trabajando normal o en ausencia programada (`Out of the Office`).
- **Causa raíz (3 bugs en `services/api-metrics/src/handlers/branded.py:get_agent_roster`):**
  1. `status.get("StatusType", "CUSTOM")` — el campo `StatusType` **no existe** en `AgentStatusReference` (la forma que retorna `GetCurrentUserData`; solo tiene `StatusStartTimestamp`, `StatusArn`, `StatusName`). El default `"CUSTOM"` se aplicaba siempre → el 100% de los agentes caían en la rama `else: effective = "Unavailable"`, sin importar su estado real. Confirmado: 0 de 57 agentes con `StatusType` presente; 19 agentes `Available` en Connect mostrados como "Away".
  2. Contactos en estado `ENDED` se contaban como ACW activo sin límite de antigüedad — un `ENDED` de hace 115 horas seguía generando "ACW 115h" en agentes deslogueados desde junio (mismo patrón "ghost agent" de julio, nunca documentado).
  3. El alert "Extended break" del frontend disparaba contra estados intencionales (`Out of the Office`, `Vacation`), indistinguibles de una pausa real porque Connect los modela igual (`Type: CUSTOM` para ambos).
- **Fix:** `_status_type_for_arn()` resuelve el tipo real vía `ListAgentStatuses` (cacheado por ARN, mismo patrón que `_rp_name_cache`); corte de antigüedad de 5 min para contactos `ENDED`; nuevo `effectiveStatus: "Offline"` separado de `"Unavailable"`; nuevo campo `isIntentionalAbsence` que el frontend usa para no disparar el alert. IAM: agregado `connect:ListAgentStatuses` al stack (`infra/lib/stacks/api-metrics-stack.ts`) — permiso faltante, verificado que `EngineeringPermissionBoundary` no lo bloquea.
- **Tests:** 8 tests nuevos (`test_branded_roster.py`, `TestStatusTypeResolution`/`TestAcwFreshness`/`TestIntentionalAbsenceFlag`). TDD: RED confirmado antes del fix, GREEN después. Suite completa `api-metrics`: 18/18. Frontend: typecheck limpio, 29/32 (3 fallas preexistentes en `chainMap.test.ts`, sin relación, confirmadas idénticas en baseline).
- **Archivos:** `services/api-metrics/src/handlers/branded.py`, `services/api-metrics/tests/unit/test_branded_roster.py`, `infra/lib/stacks/api-metrics-stack.ts`, `frontend/src/lib/api.ts` (`AgentRosterEntry`), `frontend/src/pages/BrandedMonitor.tsx` (`agentIdleAlert`, `StatusBadge`, `STATUS_ORDER`, conteo "online").
- **Sin cobertura previa:** `get_agent_roster` no tenía tests — por eso la rama muerta (`elif status_type == "ROUTABLE"`, inalcanzable desde el commit inicial) pasó inadvertida.

---

## 2026-08-17 — Alarma CloudWatch `vip-plans-scheduled-run-fallback` (domingo 08-16)

### INC-01-CORRECCIÓN — Causa raíz real no era pérdida de permiso EventBridge, sino `workingHours.days` sin validar en `prestart_check` *(Fix 2026-08-17)*

- **Contexto:** el alarm de CloudWatch se disparó de nuevo el domingo 2026-08-16 11:57 UTC (5ta vez consecutiva, mismo patrón que INC-01: 07-19, 07-26, 08-02, 08-09). INC-01 (sesión 2026-08-11) documentó la causa como "el plan `339aefac...` pierde semanalmente el permiso de invocación de su regla EventBridge, probablemente por un deploy de CDK que recrea el Lambda". Esa teoría **no se pudo confirmar** al repetir la investigación con la alarma en vivo — se descarta parcialmente abajo.
- **Causa raíz real (verificada en vivo, 5/5 ocurrencias en domingo, ningún otro PlanId ha emitido jamás la métrica):** el plan `339aefac-6b17-4cd5-9379-b077bc26c145` tiene `workingHours.days = [MON..SAT]` (excluye domingo) — correctamente configurado, el plan no debe correr los domingos. `prestart_check` (executor.py, rama fallback `delta == -1` que emite la métrica `VIPPlans/ScheduledRunFallback`, y rama pre-warm `4<=delta<=6`) **nunca valida `workingHours.days`** antes de actuar — solo chequea `isTemplate`. El domingo 2026-08-16 verifiqué en vivo que el permiso de EventBridge estaba intacto todo el tiempo (cero logs de restauración `_ensure_scheduled_run_permission`), y la alarma se disparó igual — prueba directa de que la teoría de permiso perdido no es necesaria para explicar el disparo. Efecto colateral peor al documentado en INC-01: la rama de pre-warm SÍ pre-calentó 4 campañas reales de Amazon Connect ese domingo (llamadas API reales a Connect) para un plan que nunca iba a correr — desperdicio de llamadas, no solo ruido de alarma.
- **Impacto real:** `scheduled_run()` sí protegió correctamente contra dial-outs indebidos (detecta `outside_working_hours` y no arranca el run) — ninguna llamada salió mal. El `pendingWarmup` de las campañas precalentadas innecesariamente terminó en `null` (sin rastro stale para el lunes), así que tampoco hubo riesgo para el run real siguiente.
- **Fix:** nuevo helper `_is_working_day(plan)` (chequeo de *solo día*, sin franja horaria — `_within_working_hours()` no se puede reusar tal cual en la rama de pre-warm porque esa corre 4-6 min *antes* del `startTime`, y el chequeo de horario completo la rompería para todos los planes). Guard `if not _is_working_day(plan): continue` agregado en ambas ramas de `prestart_check`. `_within_working_hours()` refactorizado para reusar el mismo helper (comportamiento sin cambios, test existente `test_within_working_hours_inclusive_end` sigue pasando).
- **Tests:** 6 nuevos en `test_executor_v2.py` (2 negativos verificados empíricamente — fallan sin el fix, pasan con él —, 1 de regresión confirmando que un plan con día permitido sigue funcionando igual, 3 unitarios de `_is_working_day`). De paso se corrigió un test preexistente (`test_prestart_fallback_skips_template_plans`) cuyo fixture de hora (`09:41` para un trigger `08:40`) en realidad producía `delta=-61`, no `-1` — nunca ejercitaba la rama que decía probar. Suite completa: 327/327. ruff clean.
- **Deploy:** `vip-admin-ui-api-plans` (CDK hotswap), 2026-08-17T14:18:49Z, verificado `LastUpdateStatus: Successful` + diff byte a byte contra el código local. Commit `7a55942`.
- **Verificación programada:** cron one-shot 2026-08-18 (día laboral, confirma que el fix no rompe el happy path) y 2026-08-23 (domingo, confirma que la alarma se queda en silencio) — ver tareas en `.claude/scheduled_tasks.json` de la sesión.
- **Archivos:** `services/api-plans/src/executor.py` (`_is_working_day`, `_within_working_hours`, `prestart_check`), `services/api-plans/tests/unit/test_executor_v2.py`.

---

## 2026-08-13/14 — Alarma CloudWatch `vip-plans-throttles` (5 reserved concurrency agotada)

### INC-02 — `_dispatch_ready_campaigns` reportaba `changed=True` sin verificar progreso real — el "while-loop amplification" diagnosticado en la Sesión 8 (2026-05-19) nunca se arregló en su raíz *(Fix 2026-08-14, deploy 2026-08-18)*

- **Síntoma:** alarma `vip-plans-throttles` (`CRITICAL: api-plans throttled - 5 reserved concurrency exhausted`) disparó a las 2026-08-13T17:32:05 UTC, auto-recuperada 1 minuto después. `ConcurrentExecutions` subió de su rango normal (2-3) a 5 exactamente a las 17:31 UTC, con 1 `Throttle`. CloudWatch Logs Insights localizó la invocación exacta (`RequestId 4dd76121-...`, 2026-08-12T18:06:37Z, `Duration: 18986.70ms`) con ~90 líneas idénticas de `_start_one_campaign[0/1]: Redis rebuilding, will retry next tick` en 19 segundos dentro de la misma invocación.
- **Causa raíz:** la Sesión 8 (2026-05-19, ver entrada arriba) ya había diagnosticado exactamente este mecanismo — punto 3, "While-loop amplification" — pero el fix aplicado entonces solo agregó el contador `reconcileRetries` para `_EmptySegmentError`; **el re-loop del `while changed:` en sí nunca se corrigió**. `_dispatch_ready_campaigns` retornaba `True` incondicionalmente tras Fase 4 si `newly_ready` no estaba vacío, sin verificar si `_start_one_campaign` realmente avanzó las campañas o las revirtió a `"queued"` (p. ej. `_RedisRebuildingError` con Redis a mitad de reconstrucción). El `while changed:` de `tick()` reintentaba entonces inmediatamente, sin backoff, hasta que Redis terminara de reconstruirse o el Lambda alcanzara el timeout de 5 min — ocupando uno de los 5 slots de concurrencia reservada por hasta 19+ segundos en vez de los ~300ms normales. Con 23+ reglas de EventBridge (`vip-plan-*-run-*-b*`, una por bucket de plan activo) disparando sincronizadas cada minuto, esto ocasionalmente agotaba los 5 slots.
- **Fix (`_dispatch_ready_campaigns`, `executor.py`):**
  1. Fase 5 ahora calcula `made_progress` verificando si alguna campaña de `newly_ready` avanzó más allá de `"queued"` — solo entonces retorna `True`.
  2. Nuevo parámetro `stalled: set[int]`, propagado por los 5 call-sites del `while changed:` (líneas ~1180, ~1700, ~1820, ~1945, ~2215) a través de todas las iteraciones de la misma invocación — evita que una campaña recién revertida se re-identifique como "lista" en la siguiente vuelta aunque otra campaña/cadena de dependencias del mismo bucket sí esté progresando genuinamente (hallazgo de una revisión adversarial con 4 sub-agentes tras el fix inicial; verificado con test que reproduce el mix y confirma solo 1 reintento por campaña atascada, no N).
  3. Nueva métrica CloudWatch `CampaignDispatchStalled` (namespace `VIPPlans`), emitida dos veces por evento (con dimensión `CampaignId` para drill-down + sin dimensión para el agregado) — mismo patrón que `ScheduledRunFallback`. Antes, los 3 caminos de revertir a `"queued"` (Redis rebuilding, empty-segment retry, redis-not-ready) solo hacían `logger.warning`, sin ninguna señal accionable en CloudWatch.
- **Alarma nueva:** `vip-plans-campaign-dispatch-stalled` (`VIPPlans/CampaignDispatchStalled`, período 60s, 5 evaluation-periods/datapoints-to-alarm) — dispara solo si una campaña queda atascada **5 minutos consecutivos** (no en el blip normal de 1-2 ticks que el diseño ya tolera). Atada a `vip-admin-alerts` (mismo SNS que las demás alarmas de `api-plans`; suscriptor confirmado: email de Sebastian). Definición agregada a `infra/scripts/create-alarms.sh` (16 alarmas).
- **Verificación empírica del fix:** revertí temporalmente `made_progress` a `return True` y confirmé que el nuevo test end-to-end (`test_tick_redis_rebuilding_does_not_busy_loop_within_one_invocation`) cuelga en loop infinito contra el código viejo — luego restauré el fix desde backup y confirmó pasar en <1s.
- **Tests:** 5 nuevos + 10 preexistentes actualizados (mockeaban `_start_one_campaign` sin simular avance de estado, lo que ahora rompía `assert changed is True`). Suite completa: 327/327. ruff clean.
- **Deploy:** `vip-admin-ui-api-plans`, 2026-08-18T16:36:10Z, `CodeSha256: W91f6raUvEe/NpUyKsrR9kCGrSgGeaNaTthkuI2LA8Q=`, verificado descargando el código real desde Lambda. **Nota de proceso:** el fix se commiteó el 2026-08-14 (`7e23507`, bundleado con un fix no relacionado de validación de números NPA) pero quedó sin desplegar hasta hoy — el redeploy de esta sesión lo llevó a producción junto con el dual-emit de la métrica y la alarma.
- **Archivos:** `services/api-plans/src/executor.py` (`_dispatch_ready_campaigns`, `_emit_dispatch_stalled_metric`), `services/api-plans/tests/unit/test_executor_v2.py`, `infra/scripts/create-alarms.sh`.

---

## 2026-08-18 — Auditoría plan branded "Jason10th" (prueba manual con contacto agregado a mano)

### BD-006 — KPIs del dashboard con cero a la izquierda (`048`, `013`) por concatenación de strings, no problema de formato *(Fix 2026-08-18)*

- **Síntoma:** en Branded Monitor, tiles "SEEDED", "ATTEMPTS" y equivalentes mostraban valores con cero a la izquierda (`048` en vez de `48`, `013` en vez de `13`) mientras la tarjeta de campaña individual mostraba el número correcto sin padding.
- **Causa raíz:** los campos de la API (`contactsPlaced`, `contactsAnswered`, `segmentSize`, etc.) están tipados `number` en TypeScript pero llegan como string numérico en runtime (Decimal de DynamoDB serializado sin normalizar — mismo patrón ya documentado en `services/api-plans/src/store.py:_normalize`, que existe justamente porque `vip_shared.json_response` usa `default=str`). Los `.reduce((s, c) => s + campo, 0)` de `BrandedMonitor.tsx` usan el operador `+` de JS: con un acumulador numérico y un string, `+` hace concatenación (`0 + "48"` → `"048"`), no suma. Confirmado también en `metrics_collector_handler.py:74` (`"answerRate": str(round(...))`) como fuente adicional consistente con el patrón.
- **Fix:** todos los `reduce` afectados (`aggregateMetrics`, `KpiHeader`, `totalSeeded` ×2) envueltos en `Number(...)`.
- **Archivos:** `frontend/src/pages/BrandedMonitor.tsx`.

### BD-007 — Barra "Plan progress" atascada en 0/0% mientras la campaña sí avanzaba *(Fix 2026-08-18)*

- **Síntoma:** en el detalle de plan, "Plan progress" mostraba `0 / 048 contacts · 0%` mientras la tarjeta de la campaña debajo mostraba correctamente `13 / 48 contacts · 27%`.
- **Causa raíz:** `totalDialed` (vista de detalle, `PlanDetailView`) se calculaba como `group.campaigns.reduce((s, c) => s + (c.totalDialed ?? 0), 0)` — sin el fallback a `metricsMap.get(c.brandedCampaignId)?.contactsPlaced` que sí tiene la tarjeta individual (`CampaignCard`, línea 244). Cuando `campaign.totalDialed` no está poblado directamente (el dato real vive solo en el snapshot de métricas), el agregado del plan sumaba `0` para cada campaña.
- **Fix:** igualada la lógica — mismo fallback a `metricsMap`, mismo `Number(...)` del BD-006.
- **Archivos:** `frontend/src/pages/BrandedMonitor.tsx`.

### BD-008 — Seeder resuelve segmentos con Attributes.ID + PhoneNumber mezclados como if/else, no como OR — descarta miembros silenciosamente *(Fix 2026-08-18)*

- **Síntoma:** un contacto de prueba (`+18593124045`) agregado manualmente vía Filter Group 2 (PhoneNumber) a un segmento de Customer Profiles que ya tenía un Filter Group 1 con 48 IDs explícitos, nunca fue sembrado en `VipProgressiveCampaignQueue` ni marcado — el segmento sembró exactamente 48 contactos, no 49, a pesar de que Customer Profiles confirmó el perfil existente con ese teléfono (`SearchProfiles KeyName=_phone`).
- **Causa raíz:** `handler_seeder.py:lambda_handler` resolvía `_extract_profile_ids(segment_groups)` y, si no estaba vacío, tomaba **solo** esa rama (`_fetch_phones`) — la rama `else` con `_extract_phones_from_filter` (PhoneNumber) nunca se evaluaba cuando el segmento tenía ambos tipos de filter group a la vez (algo que Customer Profiles trata como OR, no como mutuamente excluyente). El perfil con ese teléfono no estaba en la lista de 48 IDs, así que solo lo cubría el Filter Group 2 — descartado en silencio.
- **Fix:** ambas extracciones se ejecutan siempre y se mezclan (`id_list_phones + filter_phones`), con dedupe por si un mismo perfil matchea ambos grupos. Logging actualizado para reportar los 3 conteos (`profile_ids_found`, `id_list_phones_resolved`, `filter_phones_found`, `total_phones_after_dedupe`) en vez del campo `path` binario anterior.
- **Auditoría de exposición:** de 6 planes que usan `pinnedSegmentArn` (4 segmentos únicos), **solo el segmento de este plan de prueba mezcla ambos tipos de filtro** — los otros 3 son puros ID-list o puro phone-filter, así que el if/else viejo daba el resultado correcto en esos casos por no haber nada que mezclar. Ningún plan de producción activo estaba afectado.
- **Corrección manual aplicada al contacto de prueba:** dado que el fix no es retroactivo para el seed ya ejecutado, se insertó el item directamente en `VipProgressiveCampaignQueue` (mismo shape exacto que produce el seeder) para poder probar el fix end-to-end sin resembrar toda la campaña — el contacto fue marcado, contestado (`AgentInfo.ConnectedToAgentTimestamp` confirmado vía `describe-contact`), y las métricas lo reflejaron tras el fix del BD-009.
- **Tests:** 2 nuevos (`test_lambda_handler_mixed_id_list_and_phone_filter_merges_both`, `test_lambda_handler_mixed_dedupes_overlapping_phone`) — el primero verificado empíricamente contra el código viejo (`assert 2 == 3`, falla sin el fix). 23/23 tests del archivo, 106/106 de la suite completa del servicio. ruff clean.
- **Deploy:** `vip-admin-progressive-dialer-seeder`, 2026-08-18T14:17:27Z, verificado byte a byte. Commit `7b6a103`.
- **Archivos:** `services/api-progressive-dialer/src/handler_seeder.py`, `services/api-progressive-dialer/tests/unit/test_handler_seeder.py`.

### BD-009 — `VipActiveBrandedCampaigns` vacía durante una campaña activa → métricas ("answered", etc.) nunca se calculan, aunque los contactos sí se procesen bien *(Hallazgo, corrección manual aplicada, causa raíz sin identificar)*

- **Síntoma:** el contacto del BD-008 fue contestado (confirmado vía `describe-contact`: `AgentInfo.ConnectedToAgentTimestamp`), pero el dashboard seguía mostrando `ANSWER RATE 0.0%` más de 10 minutos después.
- **Causa raíz parcial (confirmada) — falta la causa raíz completa:** `metrics_collector_handler.py` (`vip-admin-branded-metrics-collector`, EventBridge `rate(1 minute)`, confirmado corriendo sano cada minuto sin errores) empieza cada invocación con `_scan_active_campaigns()`, que lee `VipActiveBrandedCampaigns`. Esa tabla estaba **completamente vacía** — `metrics_collector: no active branded campaigns` en cada tick — a pesar de que la campaña `2d79af7e...` seguía marcando contactos activamente (dial exitoso confirmado ~1h después de arrancar). `executor.py:_start_one_campaign` (rama branded) SÍ registra un `put_item` atómico en esta tabla como paso obligatorio antes de sembrar — si hubiera fallado, la campaña no habría podido sembrar ni marcar (lo hizo). Eso implica que el registro se creó al arrancar y **algo lo borró después**, mientras la campaña seguía corriendo. `_stop_branded_campaign` (único código que borra de esta tabla) tiene 11 call-sites en `executor.py`; ninguno relacionado dejó rastro en logs (usa `logger.info`/stdlib, que según S14-A ya documentado se descarta silenciosamente a nivel Lambda), y el estado del run/campaña en DynamoDB seguía `status: running` sin `errorDetail` — descartando los call-sites de abort/error explícito.
- **Corrección manual aplicada (no retroactiva, no es el fix):** se reinsertó el registro en `VipActiveBrandedCampaigns` con los mismos campos que produce `_start_one_campaign` — el collector lo recogió en el siguiente ciclo (60s después) y calculó correctamente `placed=49 answer_rate=2.0%`.
- **Impacto:** cualquier campaña branded cuyo registro se pierda de esta forma queda con métricas congeladas en cero indefinidamente (answer rate, voicemail rate, etc.) sin que nada lo señale como error — silencioso, no genera alarma ni log de advertencia.
- **Pendiente (próxima sesión):** instrumentar los 11 call-sites de `_stop_branded_campaign` con logging que sobreviva el filtro de nivel del root logger (usar `_slog`, no `logger` stdlib — mismo fix ya aplicado en S14-A para otras funciones de este mismo archivo), para poder capturar cuál de ellos borra el registro de una campaña que sigue activa. Candidato más probable a revisar primero: línea 1593 (`_stop_branded_campaign({"brandedCampaignId": old_branded_id, ...})` con un dict construido a mano, no el `cs` real — sugiere lógica de reintento/restart que podría estar apuntando al campaign_id equivocado dado que `campaign_id` es determinístico vía `uuid5(planId#runId#bucket#campaign_index)`).
- **Archivos:** `services/api-plans/src/executor.py` (`_stop_branded_campaign` y sus 11 call-sites, sin cambios de código — solo diagnosticado), `services/api-metrics/src/metrics_collector_handler.py` (sin cambios, comportamiento confirmado correcto).
