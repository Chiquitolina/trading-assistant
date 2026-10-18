# Manual operativo reproducible — Reaction Research Pipeline

**Versión:** 2026-10-17 · VPS `/home/multiservices-1m` · investigación solamente  
**Alcance:** Replay V2 → contexto causal 1h/4h → cartera simulada 1 slot → Competition Ledger → Shadow Outcomes → Streamlit.  
**Objetivo:** repetir un experimento desde los datos originales sin inventar factores, mezclar corridas ni modificar estrategias congeladas.

> Estado de validación: el Replay V2 reprodujo **537/537** eventos de V1 en **20 símbolos, 17–24 sept**. La paridad exacta **NO** está demostrada aún para los 290 símbolos / 14 días. La paridad de **229/229** órdenes seleccionadas del Shadow se verificó para el estudio de **20 símbolos / 14 días**; es una comprobación contra el **simulador histórico**, no contra Binance. El contexto numérico 1h/4h frente a datos LIVE aún tiene `independent_numeric_parity_verified=false`. Los portfolios siguen siendo `HISTORICAL_RESEARCH_UNVERIFIED_OR_PARTIAL` si así los etiqueta el motor.

## 0. Mapa de dependencias y artefactos

```text
UNIVERSO FIJO (290 símbolos)      FECHAS + CACHE 1m/15m
              └───────────┬───────────────┘
                   REPLAY V2 causal
                  replay_events.csv
                       summary.json
                           │
       [V1 / V2 PARITY por ventana, cuando existe V1]
                           ▼
                  CONTEXTO HISTÓRICO 3B
          (Room 1h, Return Rank4h, Volume Rank4h)
                   historical_factors.csv
                       summary.json
                           │
                      PREFLIGHT
                           ▼
                 PORTFOLIO 1-SLOT
          (V1 Frozen, Challenger #220, Guard 95/90)
              HIST_*/manifest.json, decisions.csv,
                    trades.csv, equity, etc.
                           │
                  COMPETITION PHASE 1
             competition_manifest.json,
       competition_decisions.csv, slot_episodes.csv,
                    ready_groups.csv
                           │
                   SHADOW PHASE 2
            shadow_manifest.json,
       shadow_outcomes.csv, shadow_paths_1m.sqlite3
                           │
              STREAMLIT CANDIDATE COMPETITION
```

Cada paso conserva las salidas anteriores **inmutables**. Cada nueva ejecución usa su **propio directorio**; no se sobreescriben cohortes. Los Shadow Outcomes son **simulaciones por candidata aislada**, no cartera adicional ni ganancias acumulables. Los eventos excluidos por factores causales faltantes deben contarse y reportarse.

## 1. Preflight general (antes de experimentos largos)

```bash
cd /home/multiservices-1m
pwd
python3 --version
python3 -m py_compile replay_continuous_v2_optimized.py \
  runner_historical_context_phase3b.py runner_historical_phase3.py \
  candidate_competition_lab.py candidate_competition_shadow.py
wc -l /root/runner_phase3b_smoke_sep/universe.txt
sort -u /root/runner_phase3b_smoke_sep/universe.txt | wc -l
free -h
df -h /
du -sh /root/runner_phase3_parity_cache
tmux ls
```

**Criterios:** 290 líneas y 290 símbolos distintos en el universo congelado del estudio actual. Revisar CPU/RAM, espacio de disco y procesos simultáneos. La VPS observada tenía 11 GiB RAM, sin swap; el disco tenía 17 GB libres al arrancar el replay grande (dato histórico, volver a medir). **No borrar cachés ni carpetas de investigación durante un replay en ejecución.** Nunca confundir `free` bajo con poca memoria disponible: observar también `available`.

## 2. Replay causal (Phase 3A) — datos estructurales 15m con checkpoints 1m

### 2A. El replay de 290 símbolos × 14 días YA está lanzado

- **Sesión tmux:** `runner-v2-290-14d`
- **Ventana:** `2026-09-17T00:00:00-03:00` a `2026-10-01T00:00:00-03:00`; **fin exclusivo**.
- **Motor:** `replay_continuous_v2_optimized.py` (caché causal; no modifica scanner canónico).
- **Universo:** `/root/runner_phase3b_smoke_sep/universe.txt` (290 símbolos).
- **Salida:** `/root/runner_sep_290_14d_v2`.
- **Log:** `/root/runner_sep_290_14d_v2.log`.
- **Cache persistente:** `/root/runner_phase3_parity_cache`.

**No lanzar otra copia de esta corrida.** Solo monitorearla:

```bash
tmux attach -t runner-v2-290-14d  # salir Ctrl+B, después D
# O sin adjuntarse:
grep -E '^\[[0-9]+/290\]' /root/runner_sep_290_14d_v2.log | tail -10
tail -35 /root/runner_sep_290_14d_v2.log
df -h / && free -h
```

Al terminar, validar **exit code 0**, `summary.json`, 290 símbolos OK, 0 fallidos, 0 gaps, fechas/step/hash y conteo eventos. No inferir aprobación por el simple `[290/290]` si no se escribió resumen final:

```bash
python3 - <<'PY'
import json
from pathlib import Path
p = Path('/root/runner_sep_290_14d_v2')
s = json.loads((p / 'summary.json').read_text())
assert s['symbols_total'] == s['symbols_ok'] == 290
assert s['symbols_failed'] == 0 and s['data_gaps'] == 0
assert s['step_minutes'] == 1
assert s['start_arg'] == '2026-09-17T00:00:00-03:00'
assert s['end_arg'] == '2026-10-01T00:00:00-03:00'
assert (p / 'replay_events.csv').exists()
print('REPLAY FINALIZADO / CONTROL BÁSICO OK')
print('eventos:', s['replay_events'], 'hash:', s['canonical_hash'])
print('paridad V2 independiente:', s.get('v2_canonical_event_parity_independently_verified'))
PY
```

`FULL_ID_PARITY=false`, `journal_events=0`, `REPLAY_ONLY=N` son **normales en modo research**: no se comparó con journal LIVE. **No deben interpretarse como paridad validada.**

### 2B. Plantilla para *otro* período (cuando no haya una corrida igual en marcha)

```bash
cd /home/multiservices-1m
# Adaptar fechas y nombre de salida; siempre usar una carpeta nueva.
mkdir -p /root/research_scripts
cat > /root/research_scripts/replay_nuevo.sh <<'SH'
#!/bin/bash
set -o pipefail
cd /home/multiservices-1m || exit 1
/usr/bin/time -p python3 -u replay_continuous_v2_optimized.py \
  --project /home/multiservices-1m \
  --mode research \
  --start '2026-09-17T00:00:00-03:00' \
  --end '2026-10-01T00:00:00-03:00' \
  --step-minutes 1 \
  --symbols-file /root/runner_phase3b_smoke_sep/universe.txt \
  --output /root/REEMPLAZAR_SALIDA_NUEVA \
  --cache-dir /root/runner_phase3_parity_cache \
  2>&1 | tee /root/REEMPLAZAR_SALIDA_NUEVA.log
status=${PIPESTATUS[0]}
echo "RUN_EXIT_CODE=$status"
exit "$status"
SH
# Editar el script para el período nuevo antes de usarlo.
# tmux new-session -d -s replay-nuevo 'bash /root/research_scripts/replay_nuevo.sh'
```

La salida nueva es **create-only**. Para una interrupción, evaluar `--resume` con **exactamente** las mismas fechas, universo, motor y directorios; primero revisar la firma del progreso y los logs. No reiniciar en la carpeta de un resultado final.

### 2C. Paridad de motor (V1 vs V2)

El único test completo observado hasta ahora fue **537/537** eventos (20 símbolos, 17–24 sep), 0 keys faltantes/nuevas/filas cambiadas. Para **otra ventana**, se necesita un baseline V1 de *esa misma ventana y universo* antes de afirmar paridad exacta:

```bash
cd /home/multiservices-1m
python3 compare_replay_exact.py \
  --baseline /root/runner_sep_week20_1m \
  --candidate /root/runner_sep_week20_v2 \
  --symbols-file /root/runner_sep_week20_symbols.txt
```

**No usar** esa comparación como certificación del replay 290/14d; solo certifica la cohorte comparada. Criterio de aceptación: `strict_parity:true`, faltantes/añadidos/cambiados **0**, mismo hash AST y ventana, 0 fallidos/gaps.

## 3. Reconstruir Contexto causal (Phase 3B)

Solo ejecutar cuando Replay **haya terminado** y pasado los controles de la sección 2. Room 1h utiliza swings 5×5 confirmados sobre velas completas; Return Rank4h y Volume Rank4h se calculan sobre snapshots 4h cerrados con **todo el universo de 290**. **No** reconstruir ranks usando solo los símbolos que generaron señales. El universo es el del scanner actual, no un listado point-in-time histórico (posible sesgo de supervivencia).

```bash
cd /home/multiservices-1m
# Para procesamiento largo usar tmux: tmux new -s runner-290-context
set -o pipefail
python3 -u runner_historical_context_phase3b.py \
  --project /home/multiservices-1m \
  --replay /root/runner_sep_290_14d_v2/replay_events.csv \
  --universe-file /root/runner_phase3b_smoke_sep/universe.txt \
  --output /root/runner_sep_290_14d_context \
  --cache-dir /root/runner_phase3_parity_cache \
  --min-coverage 0.95 \
  2>&1 | tee /root/runner_sep_290_14d_context.log
```

**Salidas:** `historical_factors.csv`, `market_snapshots_4h.csv`, `summary.json`, `download_errors.csv`, `download_provenance.json`, `universe.txt`. El resultado de 20 símbolos/14 días anterior fue 1.063 eventos, 1.030 completos, **33 sin Room favorable 1h**. Es una referencia, no una estimación automática para 290.

Verificar:

```bash
python3 - <<'PY'
import json
from pathlib import Path
r=json.loads(Path('/root/runner_sep_290_14d_v2/summary.json').read_text())
c=json.loads(Path('/root/runner_sep_290_14d_context/summary.json').read_text())
assert c['replay_events']==r['replay_events']
assert c['universe_symbols']==290
assert c['data_download_errors']==0
assert c['complete_factor_events']+c['missing_factor_events']==c['replay_events']
print('Cobertura de eventos con factores:', c['complete_factor_events'], '/', c['replay_events'])
print('Motivos faltantes:', c['factor_issue_counts'])
print('Numeric parity independiente:', c['independent_numeric_parity_verified'])
PY
```

Si hay factores faltantes, **no inventar valores**. Si hubo errores de descarga, corregirlos antes de evaluar. `NO_FAVORABLE_CONFIRMED_OPPOSING_SWING_1H` es una ausencia estructural, no un error de red. `--min-coverage 0.95` es el mínimo de cobertura **de mercado** para rankings por boundary, no una garantía de que 95% de las REACTIONs tengan Room completo.

## 4. Preflight + Portfolio Simulator continuo (Phase 3)

**Objetivo:** tres carteras aisladas, mismo capital inicial, solo 1 slot por cartera, órdenes LIMIT con TTL, maker/taker/slippage, TP/SL, TIMEOUT; sin reinicios diarios/semanales. Configuraciones congeladas: V1 Frozen, Challenger #220 y Extreme Guard (SHORT Return Rank4h ≥95 y Volume Rank4h ≥90). El guard es experimental y sus umbrales se eligieron tras observar la primera semana.

Primero preflight, **sin simular ni exportar equity**:

```bash
cd /home/multiservices-1m
python3 -u runner_historical_phase3.py \
  --mode preflight \
  --project /home/multiservices-1m \
  --events /root/runner_sep_290_14d_v2/replay_events.csv \
  --summary /root/runner_sep_290_14d_v2/summary.json \
  --factors /root/runner_sep_290_14d_context/historical_factors.csv \
  --assumed-execution-delay-ms 0
```

Revisar `valid_factor_rows`, `missing_or_invalid_factor_rows`, `replay_symbols_failed`, `replay_data_gaps`, `structural_parity_verified=false`, `independent_factor_parity_verified=false`. En nuestra línea de investigación es esperable que `historical_validation_ready=false`. No esconderlo.

Ejecutar **solo si se acepta explícitamente que es histórico parcial/no verificado**:

```bash
cd /home/multiservices-1m
set -o pipefail
python3 -u runner_historical_phase3.py \
  --mode run \
  --project /home/multiservices-1m \
  --events /root/runner_sep_290_14d_v2/replay_events.csv \
  --summary /root/runner_sep_290_14d_v2/summary.json \
  --factors /root/runner_sep_290_14d_context/historical_factors.csv \
  --cache-dir /root/runner_phase3_parity_cache \
  --assumed-execution-delay-ms 0 \
  --allow-partial \
  --research-unverified \
  --extreme-strength-guard-experiment \
  2>&1 | tee /root/runner_sep_290_14d_portfolio.log
```

> `--allow-partial`: descarta eventos con factores o paths faltantes y cambia la competencia por slot. Documentar cuántos se excluyeron, por qué y en qué fechas. `--research-unverified`: **no** certifica paridad; solo permite investigar dejando una etiqueta honesta. El guard debe ser variante separada, nunca sobrescribir Challenger.

**Salida:** ruta nueva `Saved NEW HISTORICAL research run: /home/multiservices-1m/reports/reaction_lab/runner_portfolio_v1/HIST_...`. **COPIAR EXACTAMENTE ESA RUTA**, no elegir automáticamente "la última" si hay runs paralelos. Exporta manifest, decisiones, trades, curvas equity y otros artefactos.

Ejemplo verificado previo (20 símbolos, 14d), **NO reutilizar para los nuevos 290**:

```text
/home/multiservices-1m/reports/reaction_lab/runner_portfolio_v1/
HIST_20261017T200411382804Z_5e7eba8e4d
```

Verificar que `source_events` coincida con los válidos tras eventuales exclusiones de paths y que `unresolved=0` para los tres; registrar TP, SL, TIMEOUT, NO_FILL, `skipped_slot_busy`, fees y máxima caída marcada. Si no hay 240 minutos de velas 1m después del retest para una candidata, el motor debe reportar `missing_or_invalid_paths`. No concluir "dataset completo" solo porque el replay no tuvo gaps.

## 5. Candidate Competition Phase 1: dueños y competencia por slot

Requiere un **Portfolio Simulator terminado**, con `decisions.csv`, `trades.csv` y `manifest.json` en su run. **No** ejecutarlo directamente con REACTIONs sin portfolio previo.

```bash
cd /home/multiservices-1m
RUN_DIR='/home/multiservices-1m/reports/reaction_lab/runner_portfolio_v1/HIST_REEMPLAZAR_POR_EL_RUN_REAL'
python3 candidate_competition_lab.py \
  --run-dir "$RUN_DIR" \
  --events /root/runner_sep_290_14d_v2/replay_events.csv \
  --factors /root/runner_sep_290_14d_context/historical_factors.csv \
  --output /root/runner_competition_290_14d_phase1
```

**Salidas:** `competition_manifest.json`, `competition_decisions.csv`, `slot_episodes.csv`, `ready_groups.csv`. Validar `slot_busy_owner_verified == skipped_slot_busy` en cada estrategia. `simultaneous_eligible_groups` incluye grupos simultáneos elegibles aunque el slot estuviera previamente ocupado; `simultaneous_free_slot_groups` cuenta los casos donde estaba libre. LIMIT pendiente también reserva el slot.

## 6. Shadow Outcomes Phase 2: todas las candidatas

Exige **hashes idénticos** de los CSV de eventos/factores y del ledger/portfolio originales, misma caché 1m y política causal. **No** regenerar el CSV en otro orden o corregirlo manualmente: aunque los valores fueran iguales, el hash podría cambiar. Genera un outcome independiente por evento válido y **por estrategia**.

```bash
cd /home/multiservices-1m
RUN_DIR='/home/multiservices-1m/reports/reaction_lab/runner_portfolio_v1/HIST_REEMPLAZAR_POR_EL_RUN_REAL'
python3 -u candidate_competition_shadow.py \
  --run-dir "$RUN_DIR" \
  --ledger-dir /root/runner_competition_290_14d_phase1 \
  --events /root/runner_sep_290_14d_v2/replay_events.csv \
  --factors /root/runner_sep_290_14d_context/historical_factors.csv \
  --cache-dir /root/runner_phase3_parity_cache \
  --output /root/runner_competition_290_14d_shadow_v2
```

**Salidas:** Phase 1 copiada + `shadow_manifest.json`, `shadow_outcomes.csv`, `shadow_paths_1m.sqlite3` indexado. Requiere `selected_orders_parity_verified == suma(orders_placed de las tres estrategias)` y `shadow_rows == source_events × strategy_count` para el mismo conjunto causal válido. **Abortar ante discrepancia.** El outcome shadow no implica capital libre, fill real ni cartera hipotética reoptimizada.

Cuatro clases clave:

| `shadow_class` | Qué significa | ¿Competidora admisible con la estrategia congelada? |
|---|---|---|
| `SELECTED_PARITY_CHECKED` | Orden histórica simulada enviada, resultado verificado | Sí, seleccionada |
| `ELIGIBLE_BLOCKED_SLOT` | Cumple filtros pero el slot ya estaba reservado | Sí, **si** el slot hubiera estado libre en ese momento |
| `GUARD_VETO_HYPOTHETICAL` | Elegible para Challenger, pero vetada por Guard | No bajo Guard |
| `OUTSIDE_FROZEN_FILTERS` | No cumplió criterios congelados | No |

MFE/MAE son envolventes OHLC 1m, no salidas ejecutables garantizadas. `NO_FILL` significa que la LIMIT no habría entrado. Las líneas post-salida son **mercado observado, no capital invertido**.

## 7. Dashboard / inspección

1. Abrir dashboard Streamlit habitual.
2. `🧪 Reaction & Swing Lab` → `🎯 Candidate Competition`.
3. En `Competition Ledger`, elegir `/root/runner_competition_290_14d_shadow_v2` cuando esté listo (auto-descubierto en `/root/runner_competition_*`).
4. Seleccionar `Runner Challenger 220` o `Runner Challenger 220 · Extreme Strength Guard`.
5. `⏱️ Timeline del slot`: elegir período y reserva; revisar propietaria y candidatas bloqueadas. Color = outcome. Barras = ocupación; cruces = candidatas perdidas por slot.
6. `🧪 Todas las candidatas`: para medir **alternativas realmente elegibles**, filtrar `ELIGIBLE_BLOCKED_SLOT`; estudiar TP/SL/TIMEOUT/NO_FILL, room/strength/ranks, MFE/MAE, lado LONG/SHORT, duración y gráfico.
7. `⚔️ Simultáneas`: grupos que tuvieron la misma disponibilidad causal y prioridad Room.
8. El resumen de bloqueadas distingue **owner_stage** `OPEN_POSITION` de `PENDING_LIMIT`, además de `shadow_status`.
9. El gráfico actualizado representa fill y salida a **precios simulados**, con la trayectoria posterior en línea tenue. No confundir con `Net %` tras fees.

Para decidir que una nueva política selecciona mejor, falta una **fase futura de Policy Replay**: reejecutar cronológicamente todas las decisiones con 1 slot, capital compuesto y reglas congeladas. No sumar los PnL individuales de las candidatas bloqueadas.

## 8. Reporte mínimo reproducible (guardar con cada experimento)

```text
EXPERIMENT_ID / UTC de ejecución:
Git commit o hashes SHA256 del scanner y módulos:
Universo y hash (290 / universo actual NO point-in-time):
Inicio / fin (tz local; fin exclusivo):
Replay engine / step 1m / output / cache / summary:
Replay: symbols_ok/failures/data_gaps/event count:
Paridad V1↔V2 (MISMA ventana): estado / ejemplos:
Context: universo 4h, porcentaje cobertura boundaries, factores completos/faltantes/motivos:
Factor numeric parity contra LIVE: VALIDADO o PENDIENTE:
Portfolio run: HIST_... / 3 perfiles / supuestos maker/taker/slippage:
Portfolio: selected/source count, TP/SL/TIMEOUT/NO_FILL, DD, fees, retorno:
Excluded factors / excluded 1m paths / reasons:
Competition: 3 estrategias / órdenes / slot busy / owner verified:
Shadow: clases / total filas / paridad de órdenes seleccionadas / fuentes SHA256:
Interpretación: cohort research / incertidumbres / hipótesis congeladas:
```

## 9. Checklist de fallos comunes

| Síntoma | Diagnóstico / acción |
|---|---|
| Existe tmux pero no aparece avance | `tmux attach`, `ps`, `tail -30 LOG`; tmux no prueba que el script siga activo |
| No hay `summary.json` | Aún procesando o ejecución abortada; revisar exit code, espacio y stacktrace |
| `REPLAY_ONLY=N` en modo research | Normal sin journal comparable; no es paridad validada |
| No coincide V1 vs V2 | Bloquear certificación; revisar ventana, universo, firma AST, timestamps, `causal_json` |
| Contexto sin Room | Swing opuesto 1h no confirmado/favorable; NO inventar Room ni eliminar sin contabilizar |
| Coverage de mercado <95% | Causal ranks 4h incompletos; revisar descargas/boundaries/universo |
| `historical_validation_ready=false` | Pendiente verificación histórica independiente; no comercializar como backtest validado |
| Portfolio no tiene paths 1m contiguos | Revisar `missing_or_invalid_paths`; no tomar 0 gaps del Replay como garantía |
| Phase 1 owner verification no coincide | Detener investigación de política; hay atribuciones de slot inconsistentes |
| Shadow falla SHA/provenance | Se mezclaron inputs de distintas corridas o fueron cambiados; recuperar originales |
| Shadow falla selected parity | Posible drift de simulador/fills/costos/candle cache; no publicar resultados |
| Shadow genera muchísimas TP fuera de filtros | Normal: son hipótesis; **no** convertirlas en trades elegibles retrospectivamente |
| La curva equity muestra +X% | Histórico OHLC 1m estimado, NO ejecución Binance; considerar fees, funding, liquidation, latencia, sesgo de selección |

## 10. Roadmap de calidad de investigación

- **Ahora:** terminar replay 290/14d en curso, revisar resumen 290/290 y errores/gaps.
- **Siguiente:** contexto 4h+1h con 290 como universo, cobertura, factores incompletos explicados.
- **Luego:** preflight y tres portfolios de 14 días *continuos*, auditar equity, pérdida máxima y selección.
- **Después:** Phase 1/2 con todas las candidatas; inspeccionar principales reservas bloqueadoras y resultados individuales.
- **Validación pendiente:** paridad exacta V1/V2 de muestra nueva del período; comprobación numérica causal contra fuentes LIVE archivadas; integridad y completitud del universo point-in-time; estudio de latencia, funding, liquidación y fill real.
- **Futuro:** políticas alternativas (prioridad Room/Strength, tiempos de slot, etc.) en un motor **1-slot contrafactual**, con comparación fuera de muestra/prospectiva **sin retocar los umbrales usando el mismo período**.

**Recomendación de versionado:** almacenar este manual en `docs/PLAYBOOK_REPLAY_CONTEXT_PORTFOLIO_COMPETITION.md`; cada cambio de contrato CLI, caché, orden temporal o esquema de eventos debe actualizar el playbook y los tests de paridad.

**Commit sugerido para el manual y mejoras visuales:** `docs(reaction-lab): document replay-to-competition pipeline and improve shadow inspection`

## 8. Candidate Selection Research — métricas as-of, desempates y documentación

**Módulo adicional, sin alterar fases 1–2 ni el Portfolio Simulator.** Una vez que existen los Shadow Outcomes completos, ejecutar la agregación offline descrita en [`docs/CANDIDATE_SELECTION_LAB.md`](CANDIDATE_SELECTION_LAB.md). Entrada: `shadow_manifest.json` + `competition_decisions.csv` + `ready_groups.csv` + `shadow_outcomes.csv`, `historical_factors.csv` hash-verificado, y opcionalmente uno o más `market_snapshots_4h.csv` para un Strength direccional **proxy** de retorno relativo a BTC. Salida nueva: `candidate_factor_snapshots.csv`, `selection_candidates.csv`, `selection_groups.csv`, `selection_policy_choices.csv`, `selection_manifest.json`.

Se exige paridad 1:1 de **cada desempate por Room con slot libre** contra la decisión congelada. No se admiten factores con `available_ms > ready_ms`, resultados futuros como input del selector, imputaciones silenciosas, reemplazos retrospectivos de posiciones ni suma de resultados shadow como equity. Otros rankers son comparaciones **descriptivas**, no nueva cartera. Para 290×14d volver a ejecutar este módulo después del Context/Portfolio/Competition/Shadow completos, usando el mismo `market_snapshots_4h.csv` del contexto 290 correspondiente. La ampliación a nuevas reglas operables requiere Portfolio Policy Replay con slot único y paridad exacta del baseline.

---

## Fase 7 — Portfolio Policy Replay (nuevo, experimental)

Después de completar **Replay → Contexto → Portfolio original → Competition Ledger → Shadow → Selection**, consultar `docs/PORTFOLIO_POLICY_REPLAY.md` antes de evaluar prioridades alternativas de un slot.

`candidate_portfolio_policy_replay.py` comprueba contra el run congelado guardado la fuente, decisiones, trades y equity, y ejecuta variantes causales cambiando **solo el orden de señales simultáneas admisibles**. Publica reportes separados por `policy` y no suma Shadow Outcomes ni los utiliza como señales de decisión.

Validar primero los 20 símbolos y 14 días contra `HIST_20261017T200411382804Z_5e7eba8e4d`; después aplicar la misma secuencia con el nuevo run de 290 símbolos. Strength sigue siendo un proxy de retornos de vela 4h cerrada, no paridad canónica con Candidate V2 ni LIVE.

---

## Fase 4.1 — Inspección visual Portfolio Policy Research (después de Policy Replay)

**Requisito:** corrida `candidate_portfolio_policy_replay.py` ya finalizada, con `manifest.json`, `summary.json`, `strategy_policy_summary.csv`, `policy_competitions.csv`, `policy_divergences.csv`, `policy_decisions.csv`, `policy_trades.csv` y `policy_equity.csv`. No hay recálculo de cartera desde el dashboard.

El dashboard original `Reaction & Swing Lab → Candidate Competition` ahora incorpora `📊 Portfolio Policy Research`, con `Equity Comparison`, `Policy Divergences` y `Slot Consequences` (selector interno).

Pasos verificables para 20 símbolos:

```bash
cd /home/multiservices-1m
python3 -m unittest discover -s tests -p 'test_reaction_lab_policy_dashboard.py' -v
ls -lh /root/runner_policy_14d20_20sym_v1/manifest.json
```

Después abrir el ledger `runner_competition_14d20_shadow_v2` y seleccionar la cartera (Challenger o Extreme Guard). El visor ubica su Selection Research enlazado, y busca únicamente Policy Replays con SHA de Selection correspondientes. La guía detallada, limitaciones de proveniencia, preparación para 290 y semántica de `Slot Consequences` están en `docs/PORTFOLIO_POLICY_DASHBOARD.md`.

**Nunca** seleccionar otra política por ver un outcome futuro; las variantes de prioridad son las ya ejecutadas en el motor causal de Fase 4. Para 290 repetir Replay → Context → Portfolio → Competition → Shadow → Selection → Policy Replay y abrir su run correspondiente.

---

## Actualización Fase 4.2 — auditoría local de Slot Consequences

Una vez generado `Portfolio Policy Replay` y sus `policy_events.csv`, habilitar el visor corregido reemplazando únicamente `dashboard/views/reaction_lab_policy_research_view.py` y `dashboard/services/reaction_lab_policy_dashboard.py`.

**Por qué:** El contador antiguo incluía diferencias varios días después de la competencia seleccionada; la vista nueva aplica `Horas posteriores` a las **mismas** métricas y tablas, y corta el alcance al llegar al siguiente desempate con ambos slots libres (sin afirmar causalidad independiente).

**Verificación 20 símbolos:** Challenger #220, política Strength vs BTC 4h proxy, fecha 20/09 18:04 ARG, ventana 6 horas → seis divergencias locales (3 de decisión, 3 de motivo/reserva), y dos divergencias del 24/09 fuera de ventana. Mostrar timeline `ORDER_PLACED / ORDER_FILLED / POSITION_EXIT / ORDER_EXPIRED` leído del CSV; NO_FILL no es trade.

**Verificación general (incluida próxima cohorte 290):** comprobar que `policy_events.csv`, `policy_decisions.csv`, `policy_trades.csv`, `policy_divergences.csv` y `policy_competitions.csv` provienen del mismo run auditado; el enlace de manifests del visor sigue verificando el Selection Lab original. Ninguna selección histórica se altera ni se vuelve a simular desde Streamlit.

Detalles y límites en `docs/PORTFOLIO_POLICY_DASHBOARD.md`, sección Fase 4.2.
