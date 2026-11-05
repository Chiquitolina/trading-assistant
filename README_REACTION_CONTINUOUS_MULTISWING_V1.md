# Reaction & Swing Lab — Continuous V1 · Multi-Swing Confluence (V1)

**Fuentes revisadas:** ZIP `REACTION_SWING_LEGACY_CONTINUOUS_SOURCES.zip`, branch `microservices`, commit `a0c69e200e1be2cba961fdd6fa37f190d4d0a6bb` (archivo `REACTION_SOURCE_VERSION.txt`).

## Qué cambió y qué NO cambió

- Se mantiene el menú existente `Reaction & Swing Lab → Event swing structure`.
- Para `Continuous V1` y la porción Continuous de `Todos`, después de la tabla actual se agrega **Multi-Swing Confluence · Continuous V1**.
- Se reutiliza el constructor canónico de gráficos `build_volume_exhaustion_chart()` de `dashboard/app.py`, pero su fuente de velas proviene del **snapshot congelado del evento**, no de la ventana actual del Redis. El marcador de `REACTION` se agrega desde el journal persistido.
- Se mantienen los controles de Legacy: TF 1m/5m/15m/30m/1h, detector 2×2/3×3/5×5, prominencia y cantidad de velas. Se agregan tolerancia de precio, coincidencia temporal y un inspector de pivots originales que tocaron en el mismo minuto.
- NO modifica `SwingDetector`, el scanner original, `Candidate V1/V2`, SQLite, la recolección 1m ni servicios LIVE.
- El archivo `reaction_lab_research_view.py` original **no se modifica**; el panel se conecta a través de `dashboard/app.py`.
- **Esta entrega es un visor de investigación, no una validación estadística del supuesto edge.** Para estimar tasas por cohorte hará falta otra fase con cobertura congelada y resultados independientes.

## Archivos entregados

```
dashboard/app.py
# completo; añade un hook de 11 líneas bajo el camino Continuous

dashboard/views/reaction_continuous_multiswing_view.py
# nuevo visor read-only, usando journal y contextos congelados

test_reaction_continuous_multiswing_view.py
# 12 pruebas causales y de alcance del hook

test_reaction_continuous_multiswing_integration.py
# prueba sintética del render de una REACTION persistida con SQLite congelado
```

## Instalación (forma habitual: Git o WinSCP)

1. Comparar `git rev-parse HEAD` con el commit fuente. Si `dashboard/app.py` cambió desde entonces, hay que reconciliar solo el pequeño hook en vez de sobrescribir modificaciones más recientes.
2. Copiar los **archivos completos a sus mismas rutas** (mantener las carpetas). El `.py` de tests va en la raíz.
3. Ejecutar desde `/home/multiservices-1m`:

```bash
python3 -m py_compile dashboard/app.py dashboard/views/reaction_continuous_multiswing_view.py
python3 -m unittest -v test_reaction_continuous_multiswing_view test_reaction_continuous_multiswing_integration
```

4. Abrir Streamlit: **Reaction & Swing Lab → Continuous V1 → Event swing structure**, bajar al panel **Multi-Swing Confluence**.

El cambio es de dashboard, no requiere reiniciar el scanner ni los servicios LIVE. Si Streamlit no recarga el código de inmediato, reiniciar únicamente el servicio de dashboard según la rutina habitual.

## Fuentes de datos / cobertura

- Eventos (`REACTION`): `reports/reaction_lab/scanner_first_touches_v1.sqlite3`, tablas de lectura a través de `dashboard.views.reaction_lab_first_touches_view.get_page` y `get_touch`.
- Snapshot por Event ID: `reports/reaction_lab/causal_context_v1.sqlite3`, campo `contexts.candles_json` y `contexts.known_ts`. Este archivo se captura para REACTIONs; si un evento carece de snapshot se ve **NO_FROZEN_CONTEXT_FOR_EVENT** y no se sustituyen velas por las del presente.
- POST opcional: archivo original `auto_paths_v1.sqlite3`, limitado a prefijo consecutivo 1m. Por defecto POST = **0** para inspección a ciegas.
- Todos los candles de entrada se filtran por **cierre de vela ≤ known_ts**, precios OHLC válidos, UTC y continuidad por tramos. Un gap rompe el tramo: no se evalúa un swing a través del hueco.
- El 15m/3×3 **de origen es el persistido por el scanner**, y siempre constituye la referencia. Las coincidencias de TF alternos son descriptivas.
- **Pivots simultáneos del journal:** el panel muestra también todas las identidades `touch_key` (`LONG`/`SHORT`, `REACTION`/`FAILED`/`INDECISIVE`) que comparten `symbol` y minuto `retest_ts`. En la base de primeros toques pueden existir pivots diferentes para un mismo `legacy_event_key`; esas filas no deben colapsarse ni considerarse operaciones independientes.

## Definición exploratoria de «dos o más swings en la misma vela»

Para cada `REACTION` con pivot conocido:

- `SHORT` → buscar `HIGH` 1m/5m compatible.
- `LONG` → buscar `LOW` 1m/5m compatible.
- `15m` del scanner original cuenta como **una referencia**.
- Un TF alterno cuenta una sola vez si su pivot cae en el mismo bloque temporal de 15m (opción alternativa 5m), está a una distancia máxima del swing original de **0,10%** (editable para exploración), y estaba **confirmado por cierre** antes o en el `known_ts` de la REACTION.
- Si un timeframe tiene varios pivots elegibles, solo cuenta el más cercano en precio. No se infiere independencia estadística: un HIGH 1m y HIGH 5m pueden ser exactamente el **mismo extremo físico** visto a distinta resolución.

Por ejemplo, una REACTION SHORT 15m a 1,0000 podría coincidir con un HIGH 5m a 1,0005 de la misma vela de 15m. Si 5m recién se confirma *después* del retest, no cuenta como contexto causal, aunque el gráfico retrospectivo pudiera mostrar el pivot.

## Pruebas

13 tests: causalidad y lookahead, misma vela vs misma zona, dirección incorrecta, deduplicación por TF, validación OHLC/velas cerradas, interrupción por gaps, inmunidad ante mutaciones futuras, identidad frozen `known_ts`, no inventar histórico, retests simultáneos con distintos pivots y alcance del hook en el menú.

**Nota de validación:** tests sintéticos locales aprobados. El ZIP de fuentes no incluye el histórico SQLite del VPS; no se ha ejecutado una prueba end-to-end con los Event IDs reales. Hay que inspeccionar al menos un SHORT y un LONG con snapshot `READY` y comparar sus timestamps/markers con la vista original antes de considerar el visor verificado en producción.

**Commit sugerido:** `research(reaction): add causal multi-swing viewer to continuous event structure`
