# Portfolio Policy Research · Dashboard integrado (Fase 4.1)

**Fecha de entrega:** 2026-10-17. **Modo:** histórico / SOLO LECTURA.

## Propósito

Exponer los resultados reales del **Portfolio Policy Replay Fase 4**, sin re-simular órdenes ni sumar Shadow Outcomes, desde el dashboard existente:

`🧪 Reaction & Swing Lab → 🎯 Candidate Competition → 📊 Portfolio Policy Research`.

Contiene tres modos: `Equity Comparison`, `Policy Divergences` y `Slot Consequences`. Se trata de **un solo tab nuevo** en Competition Lab; el selector interior cambia el modo y carga sus datos bajo demanda. Esto evita cargar todas las series 1m de equity cuando solo se quiere inspeccionar una competencia.

## Archivos / intervención manual en VPS

Proyecto: `/home/multiservices-1m`.

| Ruta relativa | Acción |
|---|---|
| `dashboard/views/reaction_lab_competition_view.py` | **Reemplazar archivo completo** (mantiene Timeline, Shadow, Simultáneas y Selection Research) |
| `dashboard/views/reaction_lab_policy_research_view.py` | **Crear** |
| `dashboard/services/reaction_lab_policy_dashboard.py` | **Crear** (helpers solo lectura) |
| `tests/test_reaction_lab_policy_dashboard.py` | **Crear** |
| `docs/PORTFOLIO_POLICY_DASHBOARD.md` | **Crear** |
| `docs/PORTFOLIO_POLICY_REPLAY.md` | **Actualizar** (integración Fase 4.1) |
| `docs/PLAYBOOK_REPLAY_CONTEXT_PORTFOLIO_COMPETITION.md` | **Actualizar** (paso final del pipeline) |

No modificar `dashboard/app.py`, `dashboard/views/reaction_lab_competition_entry.py`, `dashboard/services/reaction_lab_portfolio.py`, `dashboard/services/reaction_lab_portfolio_policy_fork.py`, ninguna estrategia ni los procesos del replay 290. **No ejecutar instaladores ni patches automáticos.** Si tenés cambios locales posteriores al ZIP anterior en `reaction_lab_competition_view.py`, comparalos antes de reemplazarlo.

## Preflight para 20 símbolos, 14 días

Ya se generó el directorio completo:

`/root/runner_policy_14d20_20sym_v1`

Y Selection Lab correspondiente:

`/root/runner_competition_14d20_selection_v1`

Fase 2 (Shadow) está en:

`/root/runner_competition_14d20_shadow_v2`

Probar que los archivos del run y su metadata están presentes:

```bash
cd /home/multiservices-1m
ls -lh /root/runner_policy_14d20_20sym_v1/{manifest.json,summary.json,strategy_policy_summary.csv,policy_competitions.csv,policy_divergences.csv,policy_equity.csv,policy_decisions.csv,policy_trades.csv}
python3 -m py_compile dashboard/views/reaction_lab_competition_view.py \
  dashboard/views/reaction_lab_policy_research_view.py \
  dashboard/services/reaction_lab_policy_dashboard.py
python3 -m unittest discover -s tests -p 'test_reaction_lab_policy_dashboard.py' -v
```

**Esperado:** 7/7 tests. El motor real de Fase 4 ya demostró 8/8 y paridad de las carteras originales en VPS; estas 7 pruebas cubren únicamente **lectura, lineage, trazabilidad y presentación**.

El visor **no necesita generar nuevos CSV**: utiliza los de `/root/runner_policy_14d20_20sym_v1`.

### Abrir el dashboard

1. Abrir el Streamlit habitual, sin levantar un segundo servidor.
2. Ir a Reaction & Swing Lab → Candidate Competition.
3. Seleccionar el ledger `runner_competition_14d20_shadow_v2` y la cartera `Runner Challenger 220` o su variante Guard.
4. Entrar a `📊 Portfolio Policy Research`. Debe descubrir automáticamente `runner_policy_14d20_20sym_v1` y validar su SHA de `selection_manifest.json` y `candidate_factor_snapshots.csv`.
5. Elegir la vista interior deseada.

**Si el Policy Replay está en una ruta no descubierta automáticamente**, definir en el entorno del proceso Streamlit (antes de iniciarlo):

```bash
export CANDIDATE_POLICY_REPLAY_DIR=/root/runner_policy_14d20_20sym_v1
```

El explorador por defecto busca directorios `runner_policy_*`, `candidate_policy_*` y `policy_replay_*` hermanos del Selection Lab. El ENV solo sirve para añadir un directorio explícito; no evita la validación de hashes. No es necesario definir la variable en la instalación actual cuando los dos directorios están en `/root` y Streamlit puede leerlos.

### Equity Comparison

- Tabla por política: equity final USDT, retorno realizado %, máximo drawdown marcado %, PF, WR, trades resueltos, órdenes, slot busy y costos acumulados. Todos se leen del CSV generado por el simulador, **no se recalculan desde resultados shadow**.
- Curvas superpuestas por estrategia y políticas seleccionadas. La serie predeterminada es `marked_equity_usdt`; se puede elegir `wallet_usdt` (realizada).
- Para no saturar RAM con muchos símbolos, la curva se lee por bloques de 100.000 filas y se reduce a **primero, último, máximo y mínimo por hora**. Esta reducción solo afecta al gráfico: las estadísticas vienen del archivo completo.
- El criterio `BASELINE_ROOM` se presenta como línea principal. Dos políticas con curvas iguales pueden tener decisiones distintas: verificarlo en los CSV.

Referencias conocidas **solo para el run 20 × 14d**, NO usar como constantes en código:

| Cartera | Mayor Room | Strength direccional proxy | Volume rank |
|---|---:|---:|---:|
| Challenger #220, equity final | 320,4494 USDT | 311,3732 USDT | 310,4615 USDT |
| Extreme Guard, equity final | 386,9451 USDT | 374,8846 USDT | 374,8846 USDT |

El dataset está marcado `HISTORICAL_RESEARCH_UNVERIFIED_OR_PARTIAL`; Extreme Guard se diseñó post-hoc y **no es validación fuera de muestra**.

### Policy Divergences

- Seleccionar una política experimental para compararla con Room original.
- Tabla de todas las competencias simultáneas con mismo `ready_ms`: candidato elegido por cada una y si cada portfolio tenía el slot libre en ese instante.
- Dentro de una competencia, tabla **de las siete políticas**, con candidato realmente enviado, resultado simulado y `net_pct` de esa operación (si existe).
- Tabla causal de **todos los candidatos elegibles** del grupo: Room, Departure, Return Rank, Volume Rank, Strength direccional proxy y timestamps as-of. Los resultados posteriores NO intervienen en la elección.
- Cuando una cartera ya tenía el slot ocupado se muestra **sin orden**; no se inventa un trade ni un fill.

### Slot Consequences

- Elegir política y competencia; mostrar todos los eventos cuyo `decision` **o `reason`** difirió de Room original a partir de ese instante.
- Separar cambios reales de decisión (`ORDER_SUBMITTED` vs `SKIPPED_SLOT_BUSY`) de casos que conservan decisión pero cambian `slot_busy_through_*` o resultado de la orden.
- Ventana local configurable (1 a 24 h): cargar decisiones por chunks y mostrarlas ordenadas por fecha, **sin cargar todo el ledger de 290 símbolos**.
- Advertencia explícita si ya hubo divergencias anteriores al grupo elegido: **las diferencias posteriores no son atribuibles exclusivamente a ese desempate**. La comparación completa de carteras es válida descriptivamente, pero no descompone causalmente el efecto de una decisión aislada si las trayectorias ya divergían.

## Auditoría / links de datos

**Se verifica:** `manifest.engine`, `summary.base_parity_verified`, cobertura estrategia × política, timestamps, duplicados en grupos, digest de referencia, recuentos positivos de paridad y SHA256 de los *inputs* `selection_manifest.json` + `candidate_factor_snapshots.csv`.

**No se verifica:** firma criptográfica del contenido posterior de `policy_*.csv`, porque el generador Fase 4 no firma esos outputs. En este punto se detectan inconsistencias estructurales, no cambios maliciosos del contenido. Para una futura cadena de custodia completa se podría agregar un manifiesto adicional de hashes de outputs, sin reemplazar ni recalcular el run existente.

El visor es de solo lectura: ni `candidate_portfolio_policy_replay.py` ni `runner_historical_phase3.py` se ejecutan desde Streamlit. No accede a Binance, Redis ni al replay de 290.

## Protocolo para 290 símbolos

Cuando finalice y se audite la corrida grande, repetir **todo el pipeline** como en `PLAYBOOK_REPLAY_CONTEXT_PORTFOLIO_COMPETITION.md`:

`Replay V2 → Context 3B → Portfolio original/paridad → Competition Ledger → Shadow Outcomes → Selection Research → Portfolio Policy Replay → Dashboard Fase 4.1`.

El run de 290 necesitará su **propio** Selection Lab y referencia `HIST_*` creada para ese dataset. No enlazar el output de 290 a snapshots de 20 símbolos: los hashes lo rechazarán. Una vez los directorios estén disponibles y verificables, aparecerán como otra opción de run dentro de la misma pestaña, sin editar `app.py`.

## Limitaciones cuantitativas

- OHLC 1m aproximado, sin funding ni liquidación de Binance ni latencia real; posible ambigüedad intravela.
- Strength side-adjusted vs BTC a partir de **una vela 4h cerrada** es un *proxy*, no paridad certificada con Candidate V2.
- En los 20 símbolos solo hubo 3 instantes independientes de competición de Challenger. La ventaja de Room es retrospectiva, **no** una propiedad probada.
- Las políticas investigadas conservan admisión, TP/SL, TTL, costos, capital y un slot congelados; solo cambia el desempate a igual `ready_ms`.
- La cohorte de 290 comparte fechas y los primeros símbolos; no constituye automáticamente un test temporal fuera de muestra.

**Commit sugerido:** `feat(reaction-lab): visualize one-slot policy equity divergences and slot consequences`.

---

# Fase 4.2 — Slot Consequences: ventana verificable (2026-10-17)

## Motivo de la corrección

En Fase 4.1 el resumen superior computaba **todas las divergencias posteriores** a la competencia elegida, mientras que el control `Horas posteriores` solo filtraba el cuadro inferior. Por ejemplo, seleccionar **Challenger #220, Room vs Strength, 20/09 18:04 (ARG)** mostraba ocho divergencias: seis del 20/09 y dos de un **nuevo desempate del 24/09**. Ese número era engañoso si se interpretaba como efecto local de la elección XPLUS vs TURBO.

## Nuevo contrato del panel

1. Primero se seleccionan política, competencia y número de horas (1–24). Todos los contadores y tablas principales usan **el mismo intervalo semiabierto** `[ready_ms, min(ready_ms+hours, siguiente_competencia_nueva))`.
2. `siguiente_competencia_nueva` significa el siguiente `ready_ms` en que **ambos portfolios tenían el slot libre Y enviaron distintas candidatas**. No es un «reinicio causal»: la equity acumulada puede diferir desde antes. Los grupos en que ambos slots ya estaban ocupados no activan ese corte.
3. Las diferencias posteriores quedan en un desplegable explícitamente rotulado **fuera de ventana / no atribuidas**. Nunca se suman a las métricas locales.
4. El visor enlaza decisiones por `(ready_ms, event_key)`, exige igualdad de claves entre portfolios y cruza el conjunto de diferencias contra `policy_divergences.csv`. Si los CSV discrepan, **no presenta un resumen supuestamente verificado**.
5. El timeline de ocupación se deriva **solo** de `policy_events.csv`: `ORDER_PLACED → ORDER_FILLED → POSITION_EXIT`, `ORDER_PLACED → ORDER_EXPIRED`, con reservas pendientes separadas de posiciones. El visor trae cierres fuera del rango visible y recorta únicamente la presentación. Si falta la cadena completa o existe `UNRESOLVED_POSITION`, no inventa un cierre y avisa.
6. Las señales marcadas como bloqueadas son `SKIPPED_SLOT_BUSY` de decisiones históricas simuladas; no se consideran trades rentables. La tabla **Órdenes realmente enviadas que cambiaron** enlaza `policy_trades.csv`: TP/SL/TIMEOUT son trades simulados, `NO_FILL` significa orden sin ejecución, y `No enviada` significa que el slot impidió enviarla. No suma retornos aislados como si fuesen equity adicional.
7. Si existieron divergencias ANTES del grupo elegido, se presenta una advertencia: ningún resultado posterior se atribuye causalmente solo al grupo actual.

### Regresión XPLUS/TURBO · 20 septiembre

En la ventana de **6 horas** desde 20/09 18:04 se esperan estos valores en Challenger #220, Room vs Strength:

| Indicador | Valor esperado |
|---|---:|
| Divergencias locales | **6** |
| Decisión distinta | **3** |
| Igual decisión, distinto motivo/reserva | **3** |
| Divergencias posteriores (24/09) | **2**, separadas y no atribuidas |
| Room eligió | XPLUSDT LONG, TP +1.96% |
| Strength eligió | TURBOUSDT LONG, TIMEOUT +0.6002% |
| Oportunidad adicional bajo Room | VELVET SHORT: NO_FILL, sin trade |
| Diferencia visible de liberación de slot | **~117 minutos** (basada en timestamps de eventos) |

El valor de 117 minutos se comprobará con los archivos `policy_events.csv` reales en VPS; la prueba automatizada reproduce los tiempos registrados en la captura, no acredita una auditoría independiente de esos CSV reales.

### Instalación manual sobre Fase 4.1

**Reemplazar solo dos archivos:**

- `dashboard/views/reaction_lab_policy_research_view.py`
- `dashboard/services/reaction_lab_policy_dashboard.py`

**Agregar prueba:** `tests/test_reaction_lab_policy_dashboard_phase4_2.py`.

Actualizar esta documentación y el playbook. **No reemplazar** `dashboard/views/reaction_lab_competition_view.py` ni `dashboard/app.py`: la pestaña ya existe. No ejecutar Portfolio Policy Replay de nuevo.

```bash
cd /home/multiservices-1m
python3 -m py_compile dashboard/views/reaction_lab_policy_research_view.py \
  dashboard/services/reaction_lab_policy_dashboard.py
python3 -m unittest discover -s tests -p 'test_reaction_lab_policy_dashboard*.py' -v
ls -lh /root/runner_policy_14d20_20sym_v1/policy_events.csv
```

**Esperado en la prueba local:** 18 tests (`7` anteriores y `11` nuevos), salvo que la suite se amplíe posteriormente. Abrir el dashboard, elegir Challenger 220 → Slot Consequences → Strength vs BTC 4h (proxy) → 20/09 18:04 → 6 horas. En la VPS real revisar contadores, barras y tabla VELVET.

El visor sigue siendo **SOLO LECTURA**, usa los mismos inputs de 20 símbolos y está preparado para el volumen de 290 con lectura de eventos por chunks y caché de datos condicionada por `mtime_ns`. Cualquier diferencia visible en el dashboard de 290 debe ser validada sobre su corrida particular.

**Commit sugerido:** `fix(reaction-lab): scope slot consequences and plot observed slot occupancy`
