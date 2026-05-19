# Handoff

## State

2026-05-18 — 26-bug sweep completo y desplegado a `vip-admin-ui-api-plans` a las 20:37 UTC (115 tests pasando):
- B1-A two-phase claim, B1-B schedule rollback, B1-C unlock-on-failure, B1-D schedule-before-dispatch
- B2-A no sleeps, B2-B lock-before-create, B2-C WH+template guards, B2-D cleanup order+startedAt
- B3-A 404 detection + errorDetail en poll, B3-B 23:59 fix, B3-C single scan, B3-D structured error
- Frontend (`PlanDetail.tsx`): campaign card rojo cuando `cs.status === 'error'` — pendiente deploy frontend.
- Script de deploy creado: `services/api-plans/deploy.sh` (usa python3 para empacar, no el zip wrapper roto).

## Next

1. Deploy frontend (`PlanDetail.tsx` red card fix) — build y subir a S3/CloudFront como siempre.
2. Deploy CDK stack para persistir `cloudwatch:PutMetricData` (actualmente solo manual en consola sobre `FunctionRoleDefaultPolicy41A10F9C`).
3. Stuck run `6203a0b5` / `1779115247053-dab1a8c9` (bucket 10 corriendo, bucket 11 creating sin schedule) — víctima pre-fix, debería auto-sanar cuando bucket 10 complete.

## Context

- `deploy.sh` usa `python3` para el zip — `/home/devaju/.local/bin/zip` es un wrapper Python roto (no soporta `--exclude`, sobreescribe en vez de append).
- Verificar siempre libs/layers antes de deploy: SharedLayer27DFABF0:39 es el actual; solo republicar si cambia `vip_shared/`.
- Nunca invocar Lambdas ni mutar estado producción — solo deploy explícito del usuario.
- `iam:CreateRole` requiere `--permissions-boundary arn:aws:iam::165505826690:policy/EngineeringPermissionBoundary`.
- `currentBucketIndex` en DynamoDB runs es display-only, nunca usado en lógica del executor.
