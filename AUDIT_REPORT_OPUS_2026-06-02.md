# GEMA — Auditoría Independiente (Opus 4.8)
**Fecha de auditoría:** 2026-06-02 | **Código auditado:** sesión de implementación del 2026-05-30 (location filter, `run_full_pipeline`, `location_raw`, migración DB, fixes de selectores, setup wizard, 66 tests nuevos) | **Modo:** A (Audit) | código tratado como ajeno y verificado línea por línea

> **LEE ESTO PRIMERO.** Tres hallazgos HIGH que fallan en SILENCIO mientras los logs y los 505 tests dicen que todo está bien. Tu instinto de que "un par de cosas no cuadran" era correcto. No hay bugs que revienten el proceso — hay features que existen, pasan sus tests, y no hacen lo que prometen en producción.

**Orden de implementación:** #1 (pérdida de datos) → #3 (detector de drift muerto) → #2 (filtro de location) → #4 → #5.

---

## Executive Risk Matrix

| # | Severidad | Persona | Ubicación | Resumen |
|---|-----------|---------|-----------|---------|
| 1 | HIGH | [DATA] | `scraper.py:339–348` `_flush()` | El buffer se vacía con `b.clear()` aunque la escritura falle, y el log dice "jobs NOT lost" — pérdida silenciosa de datos + telemetría falsa |
| 2 | HIGH | [DATA] | `matcher.py:174–212` + `selectors_registry.py:322` | Las penalizaciones del filtro de location son inalcanzables en producción — solo workingnomads tiene selector de location, y sus valores neutros nunca disparan -20 |
| 3 | HIGH | [SDET] | `scraper.py:115–156` + `test_circuit_breaker.py` | `check_null_rate` divide nulls-consecutivos/total — detector de drift muerto con nulls intercalados, y el test nuevo lo encubre |
| 4 | MEDIUM | [ARCH] | `matcher.py:200–210` | `_score_location` evalúa OPEN antes que RESTRICTED — un aviso con "worldwide" y "us only" puntúa +10 en vez de penalizar |
| 5 | MEDIUM | [SRE] | `scraper.py:85` `HALF_OPEN` | Estado definido, nunca asignado — un burst transitorio de 429 mata un board por toda la sesión sin recovery |

**Nitpicks (LOW):** `update_tier` abre una conexión SQLite por job (347 por sesión); `test_run_full_pipeline` mockea `run_scrape_session`, la dependencia que dice integrar; `needs_setup()` corre `importlib.reload(config)` en cada rerun de Streamlit.

No hay hallazgos CRITICAL.

---

## 1. [DATA — HIGH] El writer vacía el batch tras un fallo de escritura y miente en el log

**WHAT:**
`scraper.py` líneas 339–348, `_flush()` dentro de `_db_writer_task`. El `b.clear()` de la línea 348 está fuera del `try/except` — se ejecuta siempre, incluso cuando `mark_seen_batch()` lanzó excepción en la línea 344. El handler (línea 347) loggea `"[DB WRITER] Write error (jobs NOT lost — in memory)"`. No hay reintento ni re-encolado.

**WHY:**
- *Scenario:* Durante el scrape (347 jobs, `DB_WRITE_BATCH_SIZE` cards por flush), el `_db_writer_task` corre `mark_seen_batch()` vía `run_in_executor` mientras tres tabs concurrentes leen `is_seen` sobre la misma DB SQLite WAL. Un lock contention transitorio, disco lleno, o esquema sin `location_raw` hace que `mark_seen_batch` lance excepción.
- *Consequence:* El batch completo se borra del buffer (línea 348). Esos jobs nunca entran a `seen_jobs_registry`. Pero siguen en `_all_jobs` (línea 359), así que `run_full_pipeline` los retorna, `bucket_jobs` los puntúa, y `update_tier` corre un `UPDATE ... WHERE job_hash=...` que matchea cero filas en silencio. El job aparece en la UI pero NO existe en la DB — el export `JOBS_*.md` lo omite, y como nunca quedó en "seen", el próximo scrape lo re-encuentra y re-dispara la alerta de Discord. Pings duplicados de Tier 1 al teléfono.
- *Decision:* Un handler que loggea "NOT lost" en la línea inmediatamente anterior a borrar los datos es peor que no tener handler. Falla el test de la 1 AM. El batch debe sobrevivir al fallo, reintentarse de forma acotada, y escalar a alerta explícita si no se puede persistir.

**HOW TO PATCH:**
```python
async def _flush(b: list, _attempt: int = 1) -> None:
    if not b:
        return
    jobs_copy = list(b)
    try:
        await loop.run_in_executor(None, self.db.mark_seen_batch, jobs_copy)
        self._log(f"[DB WRITER] Flushed {len(jobs_copy)} jobs to registry.")
        b.clear()
    except Exception as exc:
        if _attempt < config.DB_WRITE_MAX_RETRIES:   # e.g. 3
            self._log(
                f"[DB WRITER] Write failed (attempt {_attempt}/"
                f"{config.DB_WRITE_MAX_RETRIES}) for {len(jobs_copy)} jobs — "
                f"retrying. Cause: {exc}"
            )
            await asyncio.sleep(0.5 * _attempt)   # backoff, leave buffer intact
            await _flush(b, _attempt + 1)
        else:
            self._log(
                f"[DB WRITER] PERMANENT WRITE FAILURE — {len(jobs_copy)} jobs "
                f"DROPPED from registry. Cause: {exc}"
            )
            self.summary.errors.append(
                f"DB write dropped {len(jobs_copy)} jobs: {exc}"
            )
            b.clear()   # only clear after recording the loss honestly
```
Añadir `DB_WRITE_MAX_RETRIES: int = 3` a `config.py`.

**HOW TO VERIFY:**
```python
def test_flush_failure_retries_then_succeeds(monkeypatch):
    # mark_seen_batch raises once, succeeds on retry → batch persisted, summary.errors empty
    ...

def test_flush_permanent_failure_records_loss_in_summary():
    # mark_seen_batch always raises → summary.errors names the dropped count
    assert any("DROPPED" in e for e in summary.errors)
```

**WHO / WHERE / WHEN:** El propio `_db_writer_task`; capa de persistencia; bajo lock contention WAL con 3 tabs concurrentes, o cuando la migración de esquema falló.

---

## 2. [DATA — HIGH] El filtro de location no penaliza nada en producción — da falsa seguridad

**WHAT:**
`matcher.py` líneas 174–212 (`_score_location`) + `selectors_registry.py`: el selector `location` existe en UN solo board (workingnomads, línea 322). Para los otros 12 boards activos `location_raw` es siempre `None`. Y en el único board donde se puebla, los valores son países planos ("USA", "Brazil", "Portugal", "Poland, Serbia") que `RESTRICTED` no matchea — `RESTRICTED` busca frases como `"us only"` y `"must be authorized to work in the us"`, que viven en el cuerpo del JD, no en el metadata del card.

**WHY:**
- *Scenario:* Los 16 Tier 1 del scrape del 30 de mayo salieron todos de workingnomads. `location_raw`: Canonical "Anywhere", Motley Fool "USA", Enveritas "USA", dragonboat "Portugal", CloudWalk "Brazil", Gcore "Poland, Serbia, Cyprus, Germany". Ni HARD_BLOCK (-50) ni RESTRICTED (-20) matchearon — "USA" no es "usa only". Solo Canonical disparó OPEN (+10) por "Anywhere".
- *Consequence:* De 16 Tier 1, 15 eran inaplicables para Lima (US/EU/UK/Portugal/Brazil), y el filtro penalizó CERO. Subió Canonical de 90 a 100 y dejó los otros 15 intactos en 90. Cuando Diego revisa una lista que cree filtrada por location, gasta su tiempo en roles que no puede tomar — el problema exacto que la feature decía resolver. Una feature que da falsa seguridad es peor que no tenerla.
- *Decision:* El contrato de matching está desalineado con la fuente de datos. La señal se busca donde no está (JD) y `location_raw` se puebla en 1 de 13 boards. Para Lima, un `location_raw` de país plano US/UK/EU no es neutro — es restricción blanda. El matching debe operar sobre `location_raw` específicamente, y el selector debe poblarse en todos los boards que exponen location.

**HOW TO PATCH:**
```python
# matcher.py — matching específico sobre location_raw (no sobre el blob concatenado)
def _score_location(job: JobResult) -> tuple[int, list[str], list[str]]:
    # ... HARD_BLOCK / RESTRICTED / OPEN, pero AÑADIR antes del return final ──
    loc = (getattr(job, "location_raw", None) or "").lower().strip()
    SOFT_GEO = ["usa", "united states", "u.s.", "uk", "united kingdom",
                "emea", "europe", "north america", "canada"]
    if loc and not any(o in loc for o in
                       ["anywhere", "worldwide", "global", "latam",
                        "south america", "remote - "]):
        for geo in SOFT_GEO:
            if geo in loc:
                return -10, [], [f"Location soft-restricted (Lima): '{job.location_raw}'"]
    return 0, [], []
```
Y poblar `location` en `selectors_registry.py` para los boards que lo exponen (python.org `.listing-location`, remoteok `.location`, builtin `[data-cy='job-location']`, arc.dev span de location del card). Sin esto el matcher no tiene de dónde leer.

**HOW TO VERIFY:**
```python
def test_bare_usa_location_is_soft_restricted_for_lima():
    j = _job(location="USA")
    delta, _, miss = _score_location(j)
    assert delta == -10

def test_anywhere_still_wins_over_soft_geo():
    j = _job(location="Anywhere (US timezone preferred)")
    assert _score_location(j)[0] == 10
```

**WHO / WHERE / WHEN:** El matcher en cada `score_job`; capa de scoring post-scrape; en cada revisión que Diego hace de la lista Tier 1 creyéndola filtrada.

---

## 3. [SDET — HIGH] `check_null_rate` mide nulls consecutivos, no tasa — y el test nuevo lo encubre

**WHAT:**
`scraper.py` líneas 115–156. `record_success` (línea 117) resetea `null_count` a 0. `check_null_rate` (línea 147) computa `null_count / total`. Como `null_count` es la racha consecutiva, la división da "nulls consecutivos al final / muestras totales", no la fracción de cards nulos del run. El docstring (línea 94) promete "(b) null_rate across a full SRP run" — eso no es lo que el código calcula.

**WHY:**
- *Scenario:* arc.dev cambia su DOM y la extracción falla de forma intercalada — card sí, card no, card sí — porque el selector matchea la mitad de las variantes. En el loop (líneas 820–883), cada éxito llama `record_success` (línea 879, resetea `null_count`) y cada fallo llama `record_null` (línea 871). `null_count` rebota entre 0 y 1. Al final, `check_null_rate` (línea 770) computa ≈ 1/30 = 3%.
- *Consequence:* El drift real es 50% pero el detector ve 3% y no dispara. arc.dev devuelve la mitad de los jobs en silencio, sesión tras sesión, sin que `[DRIFT ALERT]` se emita nunca. Y peor: `test_circuit_breaker.py::test_check_null_rate_trips_above_threshold` graba 5 nulls CONSECUTIVOS (`null_count=5, total=5` → 100%) y pasa — certifica como funcional la única ruta que sí funciona, ocultando que el caso intercalado real está muerto. Cobertura falsa-positiva del manual del SDET.
- *Decision:* Contador consecutivo y tasa acumulada son dos métricas distintas con un solo campo. Separarlas: un `null_total` que nunca se resetea alimenta `check_null_rate`; `null_count` consecutivo se queda para el threshold. El test debe ejercer el caso intercalado.

**HOW TO PATCH:**
```python
def _ensure(self, domain, threshold=config.CIRCUIT_BREAKER_THRESHOLD):
    if domain not in self._domains:
        self._domains[domain] = {
            "state": CircuitState.CLOSED, "null_count": 0,
            "null_total": 0, "total": 0, "threshold": threshold,
        }
    elif threshold > self._domains[domain]["threshold"]:
        self._domains[domain]["threshold"] = threshold

def record_null(self, domain, threshold=config.CIRCUIT_BREAKER_THRESHOLD):
    self._ensure(domain, threshold)
    d = self._domains[domain]
    d["null_count"] += 1     # consecutive — for threshold trip
    d["null_total"] += 1     # cumulative — for rate detection, never reset
    d["total"]      += 1
    if d["null_count"] >= d["threshold"]:
        d["state"] = CircuitState.OPEN
        logger.warning("[CIRCUIT] %s OPEN after %d consecutive nulls.", domain, d["null_count"])

def check_null_rate(self, domain, alert_threshold):
    self._ensure(domain)
    d = self._domains[domain]
    if d["total"] < 5:
        return False
    null_rate = d["null_total"] / d["total"]   # cumulative, true rate
    if null_rate > alert_threshold:
        d["state"] = CircuitState.OPEN
        logger.warning("[DRIFT ALERT] %s: null rate %.0f%% exceeds %.0f%%.",
                       domain, null_rate*100, alert_threshold*100)
        return True
    return False
```
Nota: `record_success` se queda igual (resetea solo `null_count`, NO `null_total`).

**HOW TO VERIFY:**
```python
def test_check_null_rate_trips_on_interleaved_nulls(cb):
    # 5 null + 5 success interleaved = 50% true rate, null_count nunca pasa de 1
    for _ in range(5):
        cb.record_null(DOMAIN, threshold=100)
        cb.record_success(DOMAIN)
    assert cb.check_null_rate(DOMAIN, alert_threshold=0.40) is True   # FALLA hoy
```

**WHO / WHERE / WHEN:** El loop de extracción por cada card; capa de scraping; cuando un board sufre drift parcial intermitente — el modo de drift más común y el más difícil de ver a ojo.

---

## 4. [ARCH — MEDIUM] `_score_location` prioriza OPEN sobre RESTRICTED

**WHAT:**
`matcher.py` líneas 200–210. El orden es HARD_BLOCK → OPEN → RESTRICTED. Un `searchable` con "worldwide" y "us only" retorna en OPEN (+10) y nunca llega a RESTRICTED.

**WHY:**
- *Scenario:* "Remote — Worldwide team, but you must be authorized to work in the US". Contiene "worldwide" (OPEN) y "must be authorized to work in the us" (RESTRICTED).
- *Consequence:* +10 en vez de -20. Un rol que exige autorización US sube al tope de Tier 1 por una palabra de marketing. Co-ocurrencia poco frecuente, pero cuando ocurre invierte el signo del veredicto.
- *Decision:* Para un usuario con restricción geográfica dura, la señal restrictiva domina sobre la aspiracional. RESTRICTED antes que OPEN.

**HOW TO PATCH:**
```python
    for signal in HARD_BLOCK:
        if signal in searchable:
            return -50, [], [f"Hard block: '{signal}'"]
    for signal in RESTRICTED:          # ← antes que OPEN
        if signal in searchable:
            return -20, [], [f"Location restricted: '{signal}'"]
    for signal in OPEN:
        if signal in searchable:
            return 10, [f"Location open: '{signal}'"], []
    return 0, [], []
```

**HOW TO VERIFY:**
```python
def test_restricted_wins_over_open_on_cooccurrence():
    j = _job(salary="worldwide team but us only")
    assert _score_location(j)[0] == -20
```

**WHO / WHERE / WHEN:** El matcher; capa de scoring; cuando un aviso mezcla lenguaje aspiracional global con una cláusula de autorización.

---

## 5. [SRE — MEDIUM] `HALF_OPEN` definido sin transición — sin recovery de sesión

**WHAT:**
`scraper.py` línea 85. `CircuitState.HALF_OPEN` existe y ningún método lo asigna. Una vez `OPEN`, no hay regreso a `CLOSED` dentro de la sesión.

**WHY:**
- *Scenario:* jobspresso.co (WordPress) devuelve 5×429 en 30s durante un pico. `open_circuit` lo abre. Recupera en 2 minutos, pero quedan 40+ títulos por scrapear.
- *Consequence:* Bloqueo transitorio = mismo resultado que permanente: cero jobs el resto del run. El Discord summary dice "jobspresso: 0" — indistinguible de un board caído. Diego no sabe si fue un hipo de 2 min o un bloqueo real.
- *Decision:* Un circuit breaker sin HALF_OPEN es un fusible que no se rearma. Requiere cooldown timer.

**HOW TO PATCH (requiere diseño de timing — esbozo):**
```python
def attempt_half_open(self, domain: str, cooldown_s: int = 120) -> bool:
    d = self._domains.get(domain)
    if not d or d["state"] != CircuitState.OPEN:
        return False
    if time.monotonic() - d.get("opened_at", 0) >= cooldown_s:
        d["state"] = CircuitState.HALF_OPEN
        d["null_count"] = 0
        return True   # caller permite UN probe; éxito→CLOSED, fallo→OPEN+backoff
    return False
```
Registrar `opened_at = time.monotonic()` al abrir, y llamar `attempt_half_open` desde `_bounded()` antes de cada request si el domain está OPEN.

**HOW TO VERIFY:** Test con `monkeypatch` de `time.monotonic` que avanza el reloj más allá del cooldown y verifica OPEN→HALF_OPEN.

**WHO / WHERE / WHEN:** Boards con CF bot management (jobspresso, remote.co) bajo pico; capa de scraping; primeros títulos de la sesión, donde abrir el circuito pierde el máximo de jobs restantes.

---

## Open Risks

**Contexto faltante que bloquea clearance de producción:**
El drain final del `_write_q` existe (línea 366) pero solo se alcanza si `run()` encola el sentinel `None`. No se auditó la ruta de excepción del task padre `run()`: si `run()` revienta antes de encolar el sentinel, `_db_writer_task` nunca ejecuta el `_flush` final y se pierde la cola en vuelo. Confirmar el `finally` que garantiza el sentinel.

**Mayor riesgo de falla silenciosa:**
Empate entre dos, ambos mienten:
1. `_flush()` (#1) borra el batch tras un fallo y loggea "jobs NOT lost". Sin métrica, sin alerta, con un log que afirma lo contrario de la verdad.
2. El filtro de location (#2) que crees que protege 13 boards protege 1, y ni en ese penaliza — solo da +10 a "Anywhere".

El #1 corrompe el registro de dedup y re-alerta jobs viejos. El #2 corrompe tu confianza en los tiers. Invisibles hasta que los buscas. Ninguno dispara alerta hoy.

---

## Veredicto

No hay estupidez de sintaxis ni bug que reviente el proceso — el código está bien estructurado y los 505 tests pasan. Lo que hay es peor en cierto sentido: tres cosas que fallan en SILENCIO mientras logs y tests dicen que todo está bien. El filtro de location (#2) y el detector de drift (#3) son features que existen, pasan sus tests, y no hacen lo que prometen en producción.

**Empezar por #1** (es el único que pierde datos de verdad), luego **#3** (el detector muerto deja entrar drift sin avisar), luego **#2** (la feature de falsa seguridad). #4 y #5 son rápidos después.
