# Candidate Portfolio Policy Replay — Fase 4

**Estado:** motor experimental offline, no trading LIVE; versión inicial 2026-10-17.  
**Objetivo:** comparar distintas prioridades de selección de **candidatas elegibles con el mismo `ready_ms`** mediante una **simulación de cartera completa y causal** con 1 slot, reinversión, órdenes LIMIT pendientes y TP/SL/timeout, y verificar que la política baseline reproduce exactamente la cartera histórica congelada.

## Contrato de datos y orden del pipeline

1. `replay_continuous_v2_optimized.py` produce `replay_events.csv` y `summary.json` con cierres 1m. No equivale a paridad LIVE.
2. `runner_historical_context_phase3b.py` reconstruye `historical_factors.csv` con Room 1h, rankings 4h y timestamps as-of (el universo 4h es de 290 símbolos aunque el conjunto de señales sea de 20).
3. `runner_historical_phase3.py` guarda la cartera histórica congelada `HIST_...` con `manifest.json`, `decisions.csv`, `trades.csv`, `equity.csv` y `summary.json`.
4. `candidate_competition_lab.py` reconstruye dueño y duración del slot; `candidate_competition_shadow.py` guarda cada outcome aislado. **Shadow no entra al Policy Replay ni influye en sus decisiones.**
5. `candidate_selection_lab.py` crea `candidate_factor_snapshots.csv`, `selection_manifest.json` y compara desempates descriptivos; el proxy Strength proviene de una vela 4h CERRADA, no es Strength canónico de Candidate V2.
6. **Fase actual:** `candidate_portfolio_policy_replay.py` reconstruye los paths 1m desde caché, verifica el hash de la fuente y los valores causales, simula el baseline con el motor congelado, compara con el run histórico y vuelve a ejecutar todas las políticas sobre la misma secuencia de eventos.

## Invariantes de seguridad

- **No modificar** `dashboard/services/reaction_lab_portfolio.py`, `runner_historical_phase3.py`, `replay_continuous_v2_optimized.py`, configuraciones V1/#220/Guard, archivos LIVE/Redis ni Binance.
- `dashboard/services/reaction_lab_portfolio_policy_fork.py` es **copia aislada** del motor congelado con un único cambio: una función opcional de ordenamiento **a igual `ready_ms`**. Se comprueba el SHA256 exacto del archivo original. Ante deriva del original, **detener** y volver a auditar; nunca desactivar el guard de integridad.
- Sin política: el fork y el motor original deben devolver el mismo resultado, **incluyendo decisions, trades, curvas de equity, parámetros, costos y trazas**.
- Los indicadores utilizados para ordenar deben estar calculados **antes o al momento** de decidir (`available_ms <= ready_ms`); las candidatas con contexto competitivo faltante abortan la política, en lugar de rellenarse con cero o mirar al futuro.
- Antes de crear output, el baseline debe reproducir el `source_digest` y los archivos del run guardado: cada decisión, cada trade y cada punto de equity. Si no coincide: **sin output**.
- Al estudiar otra política se recalculan cronológicamente las 1-slot decisiones, órdenes, fills, wallets y exits; **no** se suman los Shadow Outcomes hipotéticos.
- La reserva incluye LIMIT pendiente y posición abierta; una nueva señal bloqueada no se "revive" al liberar el slot.
- No existe parada/no-trade nueva ni Position Upgrade; solo se cambia la prioridad cuando dos o más señales admisibles coinciden en el mismo `ready_ms`.
- No hay garantías de ejecución LIVE: asunción OHLC 1m, sin financiación ni liquidación real, latencia virtual, historial parcial y factores 4h/1h aún no certificados contra LIVE a escala completa.

## Archivos nuevos a copiar manualmente a `/home/multiservices-1m`

| Archivo | Acción |
|---|---|
| `candidate_portfolio_policy_replay.py` | Crear en raíz |
| `dashboard/services/reaction_lab_policy_replay.py` | Crear |
| `dashboard/services/reaction_lab_portfolio_policy_fork.py` | Crear (fork experimental) |
| `tests/test_candidate_portfolio_policy_replay.py` | Crear |
| `docs/PORTFOLIO_POLICY_REPLAY.md` | Crear |
| `docs/PLAYBOOK_REPLAY_CONTEXT_PORTFOLIO_COMPETITION.md` | Reemplazar playbook extendido |

No se cambia `dashboard/app.py` ni se agrega aún una pestaña. El resultado queda en CSV/JSON para inspección y posterior integración visual.

## Paso A — Pruebas unitarias (20 símbolos no son necesarios aquí)

```bash
cd /home/multiservices-1m
python3 -m unittest discover -s tests -p 'test_candidate_portfolio_policy_replay.py' -v
python3 -m py_compile candidate_portfolio_policy_replay.py \
  dashboard/services/reaction_lab_policy_replay.py \
  dashboard/services/reaction_lab_portfolio_policy_fork.py
```

**Esperado:** 8/8 tests, sin depender de datos LIVE ni Binance.

## Paso B — Preflight y comparación en seco para 20 símbolos / 14 días

Primero verificar el run de referencia:

```bash
cd /home/multiservices-1m
ls -lh reports/reaction_lab/runner_portfolio_v1/HIST_20261017T200411382804Z_5e7eba8e4d/manifest.json
ls -lh reports/reaction_lab/runner_portfolio_v1/HIST_20261017T200411382804Z_5e7eba8e4d/{summary.json,decisions.csv,trades.csv,equity.csv}
```

El directorio correcto es `HIST_20261017T200411382804Z_5e7eba8e4d` (correr con las rutas del run realmente guardado si cambia). La primera ejecución recomendada es baseline sola:

```bash
python3 -u candidate_portfolio_policy_replay.py \
  --events /root/runner_sep_14d20_merged/replay_events.csv \
  --summary /root/runner_sep_14d20_merged/summary.json \
  --factors /root/runner_sep_14d20_merged/historical_factors.csv \
  --cache-dir /root/runner_phase3_parity_cache \
  --reference-run reports/reaction_lab/runner_portfolio_v1/HIST_20261017T200411382804Z_5e7eba8e4d \
  --selection-dir /root/runner_competition_14d20_selection_v1 \
  --output /root/runner_policy_14d20_20sym_v1 \
  --strategies both \
  --policies BASELINE_ROOM \
  --dry-run
```

**Sin archivos nuevos**. Tiene que mostrar `base_parity_verified=true`, `complete_records=1030` y los retornos **+60,22469772683308%** de Challenger y **+93,47254401552716%** de Guard (no introducir estos números como constantes en la lógica). Valores de DD históricos: −21,597541381951313% y −12,585278325905747% respectivamente.

Si falla por cambio de SHA, digest, capital, fills, equity, timestamps o 1m, investigar la discrepancia y no avanzar con las variantes.

## Paso C — Ejecutar todas las políticas en tmux

Crear `/root/run_policy_14d20_20sym.sh` con el contenido siguiente:

```bash
#!/usr/bin/env bash
set -o pipefail
cd /home/multiservices-1m || exit 1
python3 -u candidate_portfolio_policy_replay.py \
  --events /root/runner_sep_14d20_merged/replay_events.csv \
  --summary /root/runner_sep_14d20_merged/summary.json \
  --factors /root/runner_sep_14d20_merged/historical_factors.csv \
  --cache-dir /root/runner_phase3_parity_cache \
  --reference-run reports/reaction_lab/runner_portfolio_v1/HIST_20261017T200411382804Z_5e7eba8e4d \
  --selection-dir /root/runner_competition_14d20_selection_v1 \
  --output /root/runner_policy_14d20_20sym_v1 \
  --strategies both \
  2>&1 | tee /root/runner_policy_14d20_20sym.log
exit ${PIPESTATUS[0]}
```

La opción `--policies` omitida ejecuta las siete políticas predefinidas. El programa rechaza si la ruta `--output` ya existe (crear un nuevo nombre para otra ejecución). Lanzar:

```bash
chmod +x /root/run_policy_14d20_20sym.sh
tmux new-session -d -s runner-policy-14d20 'bash /root/run_policy_14d20_20sym.sh'
tail -30 /root/runner_policy_14d20_20sym.log
```

No cerrar el Replay V2 de 290 símbolos. Si CPU/RAM van justas, esperar a que termine antes de lanzar varias simulaciones pesadas.

## Archivos de salida

- `manifest.json`: rutas y hashes SHA256 de factores, snapshots y reference portfolio; versión, política, avisos de investigación.
- `summary.json`: resultado y comprobación de paridad (baseline 20 símbolos) por estrategia/política.
- `strategy_policy_summary.csv`: equity final, retorno, DD máximo, PF, trades, fees y otros contadores por **política simulada**, no por shadow.
- `policy_competitions.csv`: qué grupo realmente tenía slot libre **en cada política**, quién fue elegido, quién quedó afuera.
- `policy_divergences.csv`: decisiones distintas vs baseline, incluido su motivo.
- `policy_decisions.csv`, `policy_trades.csv`, `policy_equity.csv`, `policy_events.csv`: ledgers completos, con columna `policy`, para inspección posterior y dashboard.

## Criterios de aceptación antes de subir a 290

1. 8/8 unit tests.
2. Baseline original y fork coinciden **por igualdad completa** en test local. En VPS, hashes de código congelado aceptados y paridad del run guardado: source digest, todas las decisiones, todos los trades, toda la equity curve.
3. No se ejecutan candidatos filtrados/vetados como nuevos trades; se reordena solo a los elegibles simultáneos.
4. Ninguna política usa `shadow_status`, `shadow_net_pct`, MFE/MAE o resultados futuros para elegir.
5. Las diferencias entre carteras son trazables en `policy_competitions.csv`, `policy_divergences.csv` y curvas de equity.
6. Los resultados son exploratorios (los 20 símbolos fueron usados durante investigación; es posible sesgo de selección). No optimizar pesos sobre ellos.
7. Guardar el **commit y hashes** del motor, factores y datos. En 290 repetir el **pipeline íntegro**; no reutilizar fingerprints/manifest de los 20 símbolos para validar otro universo.

## Nuevos 290 símbolos: protocolo

Cuando termine el replay 290x14d: verificar 290/290, cero gaps, `summary.json`; reconstruir Context 3B 290-market; hacer Portfolio Simulator con guard opt-in; Competition Ledger → Shadow → Selection; verificar **paridad de baseline 290** usando su nuevo `HIST_*` como `--reference-run`; ejecutar Policy Replay con `--output` distinto y las rutas de ese mismo dataset. No inventar paths de runs futuros. El universo completo cambia la trayectoria de slot; no mezclar resultados de 20 y 290 como si fueran oportunidades independientes.

## Limitaciones cuantitativas

En la cohorte 20 símbolos hubo **tres instantes temporales distintos** de desempate de Challenger; resultados experimentales de una población pequeña. Las carteras en 290 pueden comportarse radicalmente distinto. No se considera el dataset de 290 temporalmente out-of-sample porque contiene los 20 símbolos y fechas ya analizados. Una validación independiente requerirá ventana y/o universo predefinidos antes de mirar los resultados.

**Commit sugerido:** `feat(reaction-lab): add isolated causal one-slot policy portfolio replay`.
