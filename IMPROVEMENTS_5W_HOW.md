# Mejoras propuestas (5W + How)

Resumen ejecutivo (dual-audience)
- CTO Level: Priorizar pruebas de migración de base de datos y cobertura de integración. Implementar CI reproducible que instale el paquete (`pip install -e .`) y ejecute tests con coverage. Esto reduce riesgo de regresión en producción y garantiza que el packaging funciona en entornos de despliegue.
- Stakeholder Level: Aseguramos que los tests cubran los puntos críticos (DB, integraciones) y que la canalización de pruebas se ejecute automáticamente en cada PR.

Para cada mejora incluyo: What, Who, When, Where, Why y How

1) What: Tests de migración de DB
- Who: Equipo de backend / SRE
- When: Antes del primer despliegue a staging; prioridad alta
- Where: `tests/test_migrations.py`, job CI `run-migrations`
- Why: Migraciones no testeadas pueden romper producción en despliegues incrementales — riesgo HIGH
- How: Implementar test que crea DB con esquema antiguo (SQL dump o snapshot), ejecutar rutina de migración y verificar conteo de tablas/columnas y datos críticos.
- Solución práctica: añadir script `tools/emit_old_schema.sql` y test que aplica migraciones y valida.

2) What: Pipeline CI reproducible y gates de cobertura
- Who: DevOps / Release
- When: Inmediato para PRs
- Where: `.github/workflows/ci.yml`
- Why: Falta de CI es riesgo operacional (HIGH). PRs no validados introducen regresiones.
- How: Crear workflow que instale deps, instale paquete editable, ejecute tests y genere `coverage.xml`. Rechazar PR si cobertura en paths críticos < 80%.
- Solución práctica: ejemplo de `ci.yml` que puedo añadir.

3) What: Aislamiento de integraciones y redacción de fixtures
- Who: SDET / Integraciones owner
- When: En las próximas 2 sprints
- Where: `tests/fixtures/`, `tests/test_*_client.py`
- Why: Grabaciones con PII o credenciales reales exponen datos sensibles y complican reproducciones. Necesitamos playback seguro.
- How: Adoptar `vcrpy` o `responses`; añadir redactor de cassettes para limpiar campos sensibles.
- Solución práctica: agregar helper `tests/fixtures/utils.py::redact_response(payload)` y pipeline que niegue commits con cassettes sin redactar.

4) What: Tests de concurrencia para accesos a la DB
- Who: SRE / Backend
- When: Dentro de un sprint si la app corre concurrencia real
- Where: `tests/test_db_concurrency.py`
- Why: Race conditions producen pérdidas o registros duplicados silenciosos (HIGH for SRE)
- How: Ejecutar N tareas concurrentes que inserten/actualicen y validar contadores finales e integridad (hashes únicos).
- Solución práctica: usar `ThreadPoolExecutor(max_workers=8)` y asserts finales sobre `get_registry_stats()`.

5) What: Política de secretos en fixtures y detección automatizada
- Who: Security / Dev
- When: Inmediato
- Where: Repo root CI job `scan-fixtures-for-secrets`
- Why: Evitar fuga de tokens/keys en fixtures y cassettes (CRITICAL if present)
- How: Implementar un job que escanee `tests/fixtures` y `tests/cassettes` con regexes (GROQ, NOTION, GOOGLE) y falle el build si encuentra coincidencias.
- Solución práctica: script `scripts/scan_fixtures.py` + CI job.

Decisión recomendada y coste estimado
- Alta prioridad e impacto bajo/medio: `CI pipeline` + `secrets-scan` — tiempo estimado: 1-2 días de trabajo.
- Media prioridad: `migrations tests` y `concurrency tests` — tiempo estimado: 2-5 días.
- Baja prioridad: flakiness monitoring y runs repetidos nocturnos — tiempo estimado: 1 día adicional y almacenamiento S3/artefactos.

Próximo paso sugerido
- ¿Quiere que implemente el `ci.yml` + `scripts/scan_fixtures.py` y un ejemplo de `tests/test_migrations.py` como PR? Responda "sí, crear PR" y procedo.
