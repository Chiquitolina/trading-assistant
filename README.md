# Reaction Lab · Extreme Strength Guard — experimento independiente

## Alcance

Nueva estrategia opt-in **Runner Challenger 220 · Extreme Strength Guard**.
Es una copia exacta de los parámetros congelados Challenger #220, con una
sola regla causal extra, **antes de reservar el slot**:

```
side == SHORT AND return_rank_4h >= 95 AND volume_rank_4h >= 90
```

No filtra LONG. En las señales SHORT que ya cumplen las reglas base, registra
`FILTER_REJECTED_EXTREME_STRENGTH` y un `guard_id` auditables. No hace
ningún filtro retrospectivo de trades y vuelve a ejecutar la simulación
cronológica completa (capital compuesto y slot único). Los dos perfiles
originales siguen intactos y la variante solo aparece al pasar
`--extreme-strength-guard-experiment`.

Es una variante **IN-SAMPLE POST-HOC**: umbrales 95/90 sugeridos después
de analizar pérdidas en la semana de septiembre. NO es evidencia de
rentabilidad futura. El historical sigue marcado como UNVERIFIED/PARTIAL.

## Archivos

- `runner_historical_phase3.py`: flag opt-in del histórico.
- `runner_portfolio_lab.py`: flag opt-in para replay retrospectivo de LIVE.
- `dashboard/services/reaction_lab_portfolio.py`: cartera separada, veto
  causal, nuevos conteos y metadatos del experimento.
- `dashboard/services/reaction_lab_portfolio_reader.py`: etiqueta de variante.
- `dashboard/views/reaction_lab_portfolio_view.py`: nueva pestaña
  `🧪 Extreme Strength Guard`, A/B y trades nuevos/ausentes, veto por evento.
- `tests/test_extreme_strength_guard.py`: siete tests sin datos externos.

No incluye ni modifica `dashboard/app.py`, porque la tab ya existente
`📈 Runner Portfolio` importa dinámicamente el módulo view.
No cambia `replay_continuous_v1_historical.py`, V2 optimizado, scanner,
Redis, PM2 ni ejecución real de órdenes.

## Instalación segura (usar una terminal del VPS)

1. Descargar y subir ZIP al VPS, por ejemplo como `/root/runner_extreme_strength_guard.zip`.
2. En tu VPS:

```bash
cd /home/multiservices-1m
mkdir -p /root/runner_extreme_strength_guard_stage
unzip -o /root/runner_extreme_strength_guard.zip -d /root/runner_extreme_strength_guard_stage

# Nunca aplicar a ciegas en módulos divergentes
patch --dry-run -p1 < /root/runner_extreme_strength_guard_stage/extreme_strength_guard.patch
```

Si el dry-run falla, NO aplicar ni copiar módulos completos: comparar
contra las fuentes de `/home/multiservices-1m` y reconciliar los cambios.

3. Si el dry-run pasó, backups antes de modificar:

```bash
cd /home/multiservices-1m
BACKUP="/root/runner_extreme_guard_backup_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACKUP"
cp --parents runner_historical_phase3.py runner_portfolio_lab.py \
  dashboard/services/reaction_lab_portfolio.py \
  dashboard/services/reaction_lab_portfolio_reader.py \
  dashboard/views/reaction_lab_portfolio_view.py "$BACKUP/"
echo "BACKUP=$BACKUP"
patch -p1 < /root/runner_extreme_strength_guard_stage/extreme_strength_guard.patch
mkdir -p tests
cp /root/runner_extreme_strength_guard_stage/tests/test_extreme_strength_guard.py tests/
```

4. Pruebas:

```bash
python3 -m py_compile runner_historical_phase3.py runner_portfolio_lab.py \
  dashboard/services/reaction_lab_portfolio.py \
  dashboard/services/reaction_lab_portfolio_reader.py \
  dashboard/views/reaction_lab_portfolio_view.py
python3 -m unittest discover -s tests -p 'test_extreme_strength_guard.py' -v
python3 runner_historical_phase3.py --help | grep extreme-strength
```

**El flag por defecto está apagado**. Las ejecuciones anteriores no se reescriben.

## Reproducir la semana con tres variantes

Primero es aconsejable ejecutar `--dry-run` y comparar las métricas congeladas
de las dos estrategias originales con el run de referencia
`HIST_20261017T132444703763Z_5e726a03b3` (Challenger #220:
$230.01893346804837, 36 trades, -21.597541381951313% drawdown).

```bash
cd /home/multiservices-1m
python3 -u runner_historical_phase3.py \
  --mode run \
  --project /home/multiservices-1m \
  --events /root/runner_sep_week20_1m/replay_events.csv \
  --summary /root/runner_sep_week20_1m/summary.json \
  --factors /root/runner_sep_week20_context/historical_factors.csv \
  --cache-dir /root/runner_phase3_parity_cache \
  --assumed-execution-delay-ms 0 \
  --allow-partial --research-unverified \
  --extreme-strength-guard-experiment \
  --dry-run
```

Sin `--dry-run` genera un **nuevo** directorio `HIST_...` con tres estrategias,
SQLite ledger, decisiones, operaciones, equity y manifest con
`experimental_variants`. Luego en Streamlit → Reaction & Swing Lab →
`📈 Runner Portfolio`, seleccionar el NUEVO run y abrir
`🧪 Extreme Strength Guard`.

La población de referencia es de 516 de las 537 REACTIONs: los otros 21
no tienen Room 1h favorable confirmado y quedan fuera de esta simulación
parcial (no se inventan factores).

## Comparaciones y auditoría

- 77 elegibles base Challenger; hay 6 señales SHORT con ambos ranks extremos
  (4 `ORDER_SUBMITTED`, 2 `SKIPPED_SLOT_BUSY` en el run ORIGINAL).
- En la nueva variante deberían quedar 6 decisiones `FILTER_REJECTED_EXTREME_STRENGTH`
  sobre esta fuente, pero **no predecimos** el PnL ni los slots de la variante.
- El dashboard muestra decisiones vetadas y trades que aparecen o desaparecen
  respecto de #220; NO suma mecánicamente pérdidas evitadas.
- El resultado sigue siendo estimación OHLC 1m, sin funding/liquidaciones,
  sin fill real, y sin validación externa del criterio 95/90.

## Commit sugerido

`feat(reaction-lab): add isolated experimental extreme-strength guard portfolio`
