# Persistencia y linaje real de los datos

**Fuente:** inventario `DATA/persistence_inventory.csv` del ZIP de auditoría; código de las funciones de escritura/lectura inspeccionado. **Fecha:** 18/10/2026. **Alcance del inventario:** 20.876 archivos, 6.752.854.991 bytes (~6,75 GB), 33 raíces inventariadas; no es una lista completa de todo el VPS. Es una fotografía, no un backup.

## A. REACTION activas: fuentes de verdad (SQLite)

Rutas relativas a `/home/multiservices-1m/`:

| Archivo | Fuente de escritura / significado | Tamaño visto |
|---|---|---:|
| `reports/reaction_lab/scanner_events_v1.sqlite3` | Scanner causal REACTION, eventos, ciclos y procedencia LIVE/RECOVERED | 6,5 MiB |
| `reports/reaction_lab/auto_paths_v1.sqlite3` | 1m OHLC contiguo asociado a eventos/monitores | 29,9 MiB |
| `reports/reaction_lab/causal_context_v1.sqlite3` | Contexto causal y followups; NO mezclar causal con outcome posterior | 751,1 MiB |
| `reports/reaction_lab/runners_v1.sqlite3` | Runners históricos, incluye información posterior y velas asociadas | 446,5 MiB |
| `reports/reaction_lab/btc_context_v1.sqlite3` | Contexto BTC asociado a scanner | 67,5 MiB |
| `reports/reaction_lab/deep_research_v1.sqlite3` | `event_features` causales vs `event_outcomes` posteriores | 37,1 MiB |
| `reports/reaction_lab/telemetry_v1.sqlite3` | Telemetría de mercado, prioridades y liquidaciones observadas | 79,2 MiB |
| `reports/reaction_lab/telemetry_link_v1.sqlite3` | Vinculación de scanner con snapshots de telemetría | 36,3 MiB |
| `reports/reaction_lab/continuous_v1_cohort.json` | Definición temporal / referencia de cohorte | diminuto |

La REACTION estructural del scanner NO coincide necesariamente con los subconjuntos V1/V2 del dashboard, ni con el universo de eventos Replay V2. **Clave frecuente:** `event_key = SYMBOL|SIDE|retest_timestamp`, acompañada de timestamps de disponibilidad y procedencia. Para conocer la columna exacta de una tabla, usar `PRAGMA table_info` sobre la copia de SQLite; no se extrajeron bytes de las bases, solo sus rutas/tamaños.

## B. Monitores, matrices históricas y datos auxiliares de REACTION

- `/home/multiservices-1m/candidate_v1_frozen_monitor.csv`, `candidate_v1_long_frozen_monitor.csv`, `candidate_v2_monitor.csv`.
- `reports/candidate_v1_analysis/` y `reports/candidate_v2_analysis/`: análisis Legacy, pickles de trayectorias, estados. **Los archivos `.pkl` solo deben leerse desde una fuente propia de confianza**.
- `reports/reaction_lab/legacy_archive_20261012_172158/`: copia histórica de monitores Legacy.
- `reports/candidate_long_horizon/`: caché OHLCV comprimida y runs para larga duración; **17.757 archivos, ~1.728 MiB** según la auditoría.
- `volume_exhaustion_events.csv`, `volume_exhaustion_outcomes.csv`, `micro_flow_snapshots.csv`, `micro_flow_events.csv`, `micro_flow_first_touch.csv` en la raíz del proyecto.
- `reports/replay_runs/`, `reports/research/`, `reports/reaction_lab/runner_portfolio_v1/` guardan ejecuciones derivadas y sus manifests.

## C. Investigación Replay V2 20 símbolos / 14 días

| Etapa | Directorio absoluto | Evidencia principal |
|---|---|---|
| Replay merged | `/root/runner_sep_14d20_merged/` | `replay_events.csv`, `summary.json` |
| Contexto 4h/1h | `/root/runner_sep_week20_context/`, `/root/runner_sep_holdout20_context/` | `historical_factors.csv`, `market_snapshots_4h.csv` |
| Contexto merged | `/root/runner_sep_14d20_merged/` | `historical_factors.csv` |
| Portfolio original | `/home/multiservices-1m/reports/reaction_lab/runner_portfolio_v1/HIST_20261017T200411382804Z_5e7eba8e4d/` | `decisions.csv`, `trades.csv`, `equity.csv`, `manifest.json`, `portfolio_ledger_v1.sqlite3` |
| Competition | `/root/runner_competition_14d20_phase1/` | `competition_decisions.csv`, `slot_episodes.csv`, `ready_groups.csv`, `competition_manifest.json` |
| Shadow | `/root/runner_competition_14d20_shadow_v2/` | `shadow_outcomes.csv`, `shadow_paths_1m.sqlite3`, `shadow_manifest.json` |
| Selection | `/root/runner_competition_14d20_selection_v1/` | `candidate_factor_snapshots.csv`, `selection_policy_choices.csv`, `selection_candidates.csv`, `selection_manifest.json` |
| Policy portfolios | `/root/runner_policy_14d20_20sym_v1/` | `policy_decisions.csv`, `policy_trades.csv`, `policy_equity.csv`, `policy_events.csv`, `policy_competitions.csv`, `policy_divergences.csv`, `manifest.json` |
| Precios para ejecutar/pasar paridad | `/root/runner_phase3_parity_cache/` | caché histórica usada por simuladores |

**Linaje:** Replay evento (`event_key`) → factor causal (`event_key`) → decisión (`strategy,event_key`) → Competition (`group_id`, slot) → Shadow independiente (`strategy,event_key`) → Selection (`strategy,group_id,policy`) → Policy Portfolio (`strategy,policy,event_key`, tiempo). **No sumar PnL de Shadow como equity.** Una misma señal puede aparecer en múltiples políticas y estrategias: los recuentos no son trades independientes.

## D. Replay 290 símbolos (EN CURSO al capturar)

`/root/runner_sep_290_14d_v2/`: el inventario vio `replay_events.csv`, `progress.json`, `history_gaps.csv`, `download_errors.csv`, pero **no vio aún `summary.json`**. El estado de esa carpeta era **parcial**; sus ~4,4 MiB no constituyen un run final. No asumir que existe contexto/Competition/Shadow/Policy sobre 290 símbolos hasta ejecutar las fases correspondientes.

## E. Reglas de conservación

1. Conservar **fuentes**: REACTION SQLite, Replay completo + `summary.json`, contextos y market snapshots, cachés 1m/4h, monitores.
2. Conservar **derivados con linaje**: manifests/hashes, Portfolio, Competition, Shadow SQLite, Selection, Policy y documentación/config congelada.
3. Conservar **código exacto del experimento** y dependencias/versión; la copia de auditoría es el punto de partida, no una prueba de que sean iguales en una fecha futura.
4. Hacer una **copia final después del Replay 290**, y no modificar registros origen durante el análisis.
5. Redis y WebSocket LIVE no son persistencia histórica recuperable automáticamente; respaldarlos requeriría procedimiento específico adicional, que no está cubierto por este espejo de archivos.

**Tamaño significativo por categoría del inventario:** `reports/reaction_lab` ~1.625 MiB; `reports/candidate_long_horizon` ~1.728 MiB; `micro_flow_snapshots.csv` ~1.321 MiB, `micro_flow_events.csv` ~534 MiB; `runner_phase3_parity_cache` ~247 MiB. El destino Windows debe reservar holgura para el `.tar.gz` **y** su futura extracción.
