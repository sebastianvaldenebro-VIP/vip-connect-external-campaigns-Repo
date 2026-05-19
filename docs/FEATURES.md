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
