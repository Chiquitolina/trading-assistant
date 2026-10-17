# Candidate Selection Research — Factor Snapshot + Selection Analyzer (v1)

**Estado:** Fase 3 experimental, **solo lectura**; no cambia Replay V2, Context 3B, Challenger 220, Extreme Guard, Portfolio Simulator ni los Shadow Outcomes. La integración en el dashboard es una pestaña de Competition Lab. Este módulo **no** es Portfolio Policy Replay.

## Objetivo y preguntas

Para cada REACTION **completa** que atravesó el motor histórico se guarda una fotografía inmutable de la información causal conocida cuando la señal quedó `ready_ms`. Se estudian grupos de señales **elegibles simultáneas** con **slot inicialmente libre**: baseline mayor Room 1h versus ordenamientos alternativos usando exclusivamente las métricas que existían en ese instante. Los outcomes Shadow (TP, SL, TIMEOUT, NO_FILL) **solo** se vinculan después de escoger el candidato, para evaluación descriptiva. Nunca son input del ranker ni representan una cartera realizable.

Dos tipos de grupos que NO deben confundirse:

* `SIMULTANEOUS_FREE_SLOT`: dos o más elegibles compiten en el mismo `ready_ms`, con slot libre. Es el **único** tipo sobre el que el analizador produce alternativas de desempate. Se exige reproducir exactamente la selección original por mayor Room.
* `SIMULTANEOUS_PREOCCUPIED`: el slot ya estaba ocupado por una orden o posición anterior. No hay selección válida sin cambiar decisiones anteriores; se registra solo para inspección.

También conserva y vincula decisiones `FILTER_REJECTED` y `FILTER_REJECTED_EXTREME_STRENGTH`, pero NO las trata como elegibles para competir con la política congelada.

## Factor Snapshot y causalidad

Archivo: `candidate_factor_snapshots.csv`, **una fila por evento válido** (no una por estrategia). Conserva:

| Campo | Definición / fuente |
|---|---|
| `event_key`, `symbol`, `side`, `ready_ms` | Identidad y reloj de decisión del Portfolio Ledger original |
| `room_1h_pct` | Distancia a swing 1h opuesto confirmado, de `historical_factors.csv` |
| `departure_pct` | Magnitud estructural causal, del Decision Ledger original |
| `return_rank_pct_4h`, `volume_rank_pct_4h` | Rankings de universo cerrado 4h obtenidos de Context 3B |
| `directional_return_rank_4h` | LONG = rank de retorno; SHORT = `100 - rank de retorno` |
| `room_asof_ms`, `room_available_ms`, `ranks_asof_ms`, `ranks_available_ms` | Cortes y disponibilidad del contexto; cada uno debe ser <= `ready_ms` |
| `symbol_return_closed_4h_pct`, `btc_return_closed_4h_pct` | Retornos 4h de la **misma vela cerrada** extraídos de `market_snapshots_4h.csv` |
| `directional_strength_vs_btc_4h_proxy_pct` | LONG = retorno símbolo menos BTC; SHORT = BTC menos símbolo |
| `relative_volume_4h`, `market_breadth_4h` | Snapshot market 4h con `boundary_close_ms` coincidente |
| `strength_status`, `feature_snapshot_close_ms` | Fuente y ausencia explícita de métricas; no completar con cero ni datos futuros |

**Advertencia de interpretación:** `directional_strength_vs_btc_4h_proxy_pct` es un **proxy de retorno relativo de una única vela 4h cerrada**, no una reproducción certificada de `side_adjusted_strength_vs_btc_4h` de Candidate V2. Antes de denominarla «Strength canónico» hay que auditar su fórmula y paridad independiente. Si BTCUSDT no forma parte de los snapshots completos, el factor y las políticas que lo necesitan se marcan `SKIPPED_MISSING_CAUSAL_FEATURE`. **Nunca** se sustituye por un valor futuro o por cero.

El sistema comprueba que las métricas originales reproducen numéricamente las entradas del Decision Ledger y no están fechadas después de `ready_ms`. Pero eso **no** convierte la reconstrucción Context 3B en paridad independiente con LIVE; permanecen las etiquetas de investigación parcial.

## Entradas, provenance y salidas

**Requiere previamente**: Replay V2 → Context 3B → Portfolio Runner → Competition Ledger Phase 1 → Shadow Outcomes Phase 2; seguir `docs/PLAYBOOK_REPLAY_CONTEXT_PORTFOLIO_COMPETITION.md` en ese orden.

* `--shadow-dir`: carpeta que contiene `shadow_manifest.json`, `competition_manifest.json`, `competition_decisions.csv`, `ready_groups.csv` y `shadow_outcomes.csv`. No modificarla.
* `--factors`: EXACTAMENTE el `historical_factors.csv` cuya huella SHA-256 aparece en el `competition_manifest.json`. Si no coincide, se detiene.
* `--market-snapshots`: 0, 1 o múltiples archivos `market_snapshots_4h.csv` del Context 3B para el **mismo universo de mercado, metodología y ventanas**. Se unen por `(boundary_close_ms, symbol)`; un conflicto entre snapshots duplicados detiene la exportación. Si no se pasan, el analizador básico Room/Return Rank/Volume Rank funciona, pero Strength/Breadth/Relative Volume quedan ausentes.
* `--output`: **directorio nuevo**, nunca sobreescribir un export anterior.

Salida: `candidate_factor_snapshots.csv`, `selection_candidates.csv`, `selection_groups.csv`, `selection_policy_choices.csv`, `selection_manifest.json`. El manifiesto guarda hashes de los originales, cantidad de filas, estado de Strength, grupos y verificación de baseline. No copia ni altera históricos ni SQLite 1m.

## Ejemplo actual — 20 símbolos, 14 días (17 septiembre–1 octubre 2026)

Primero revisar existencia de los snapshots de cada semana:

```bash
cd /home/multiservices-1m
ls -lh \
  /root/runner_sep_week20_context/market_snapshots_4h.csv \
  /root/runner_sep_holdout20_context/market_snapshots_4h.csv \
  /root/runner_sep_14d20_merged/historical_factors.csv \
  /root/runner_competition_14d20_shadow_v2/shadow_manifest.json
```

Ejecutar (puede correrse fuera de tmux; es una agregación de CSVs, no otro replay):

```bash
python3 -u candidate_selection_lab.py \
  --shadow-dir /root/runner_competition_14d20_shadow_v2 \
  --factors /root/runner_sep_14d20_merged/historical_factors.csv \
  --market-snapshots \
    /root/runner_sep_week20_context/market_snapshots_4h.csv \
    /root/runner_sep_holdout20_context/market_snapshots_4h.csv \
  --output /root/runner_competition_14d20_selection_v1
```

Si uno de los dos snapshots no existe, **no inventar rutas**: verificar la salida exacta de los procesos Context 3B. Se puede ejecutar **sin** `--market-snapshots` para validar primero Room y rankings, usando otra carpeta de salida (p. ej. `runner_competition_14d20_selection_baseline_only`). Esto **no** es sustituto del análisis Strength.

### Controles básicos después de exportar

```bash
python3 - <<'PY'
import json
from pathlib import Path
p=Path('/root/runner_competition_14d20_selection_v1')
s=json.loads((p/'selection_manifest.json').read_text())
assert s['baseline_room_parity_verified'] is True
assert s['source_valid_events']==1030
assert s['strategy_candidate_rows']==3090
assert s['simultaneous_free_slot_groups_checked']==6
print('SNAPSHOTS:',s['source_valid_events'])
print('CANDIDATAS POR ESTRATEGIA:',s['strategy_candidate_rows'])
print('PARIDAD ROOM (6 grupos): OK')
print('STRENGTH STATUS:',s['feature_status'])
print('POLICY ROWS:',s['policy_rows'])
PY
```

Los conteos 1.030, 3.090 y 6 son el **referente histórico de esta cohorte**, no valores universales para 290 símbolos. Cualquier discrepancia debe investigarse, no «ajustar» los asserts. El dashboard principal mostrará `Reaction & Swing Lab → Candidate Competition → 🔬 Selection Research` cuando la exportación esté vinculada por hash al `shadow_manifest.json` abierto.

## Después del Replay de 290 símbolos

No empezar aquí hasta que termine `runner-v2-290-14d`. Seguir Playbook: Context 3B (su `market_snapshots_4h.csv`) → Portfolio → Competition Ledger → Shadow Outcomes, todos sobre el **mismo** set completo 290×14d y guardando cada output en una carpeta nueva. Luego:

```bash
python3 -u candidate_selection_lab.py \
  --shadow-dir /root/RUTA_SHADOW_290_REAL \
  --factors /root/runner_sep_290_14d_context/historical_factors.csv \
  --market-snapshots /root/runner_sep_290_14d_context/market_snapshots_4h.csv \
  --output /root/runner_competition_290_14d_selection_v1
```

Sustituir `RUTA_SHADOW_290_REAL` por la ruta exacta producida en ese run. No ejecutar con una carpeta inventada.

## Política y baseline

Ordenamiento original (solo cuando `ready_ms` idéntico y slot inicialmente libre): `(ready_ms, -Room 1h, event_key)`; se exige paridad de la seleccionada en cada grupo. Variantes **descriptivas**, sin efecto en la cartera congelada: mayor Room, mayor retorno direccional vs BTC proxy, Strength→Room, Room→Strength, mayor Return Rank favorable al lado, mayor Volume Rank, mayor relative volume 4h.

* `Room → Strength` seguirá generalmente al baseline cuando Room difiera: Strength es criterio secundario, no crea peso oculto.
* Los valores faltantes hacen **skip** de la variante para el grupo completo. Se evita ranking parcial injusto.
* La elección se calcula **antes** de consultar `shadow_status`, `shadow_net_pct` o salidas futuras. Si cambia un outcome, **la elección no puede cambiar** (hay tests automáticos).
* `isolated_delta_net_pct_both_filled` solo existe cuando **ambas** órdenes aisladas tuvieron fill y resultado terminal. NO es equity, no suma comisiones/capital/slots de trayectorias alternativas.
* Si un slot ya estaba ocupado, reemplazarlo sería otro experimento distinto (Position Upgrade / Portfolio Policy Replay), NO un desempate.

## Próxima fase obligatoria antes de promover una política

1. Auditar fórmula exacta de Strength contra Candidate V2 y probar paridad independiente de sus snapshots. No sustituir el proxy por el nombre canónico sin esa prueba.
2. Aumentar el número de grupos con slot libre (en 20 símbolos/14d son apenas seis entre tres carteras; Challenger tiene tres), con universo y período independientes.
3. Congelar las políticas candidatas, separar discovery/validación temporal, no optimizar con TP/SL del mismo conjunto.
4. Construir **Portfolio Policy Replay** independiente y verificar paridad total del baseline (mismos trades, fill/exit, equity, drawdown, órdenes NO_FILL y `SKIPPED_SLOT_BUSY`) antes de comparar otros selectors. Simular cronológicamente con un solo slot, capital real del escenario y comisiones.
5. Solo más adelante validar en shadow LIVE y testnet controlado. Nunca considerar resultados OHLC históricos ejecución en Binance.

## Limitaciones y riesgos preservados

* `HISTORICAL_RESEARCH_UNVERIFIED_OR_PARTIAL`, solo 20 símbolos en la muestra actual, sin paridad LIVE completa.
* Universe actual de 290 símbolos NO equivale a universo histórico point-in-time (sesgo de supervivencia).
* Room ausente deja eventos excluidos (33/1.063 en la muestra actual).
* V2 paridad 537/537 demostrada **solo** en ventana 17–24 septiembre/20 símbolos, no en 290×14d.
* 1m OHLC no resuelve secuencia intravela; fees/slippage estimados, no funding ni liquidación; los shadow outcomes son contrafácticos aislados.
* Estrategias frozen no se modifican. La nueva carpeta selection es de solo investigación y create-only.
