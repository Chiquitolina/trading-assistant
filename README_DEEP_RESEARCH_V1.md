# Continuous V1 · Deep Runner Research — fase prospectiva

**Esta entrega no cambia ninguna detección de REACTION Base, trade, configuración de riesgo, ni ningún proceso PM2 existente.**

## Qué se persiste para *todas* las nuevas REACTIONs

El nuevo `reaction_lab_deep_collector.py` es un sexto consumidor de solo lectura de las fuentes originales, con **SQLite sidecar nueva** `reports/reaction_lab/deep_research_v1.sqlite3`:

1. **Causal e INMUTABLE por Event ID** (`event_features`): duración, velocidad, eficiencia y volumen del departure, porcentaje de velas direccionales, magnitud en ATR; mechas, fuerza del cierre, velocidad/eficiencia de aproximación, penetración y volumen del retest; ATR 1m y percentil, volatilidad realizada 5/15/30/60m, shock de volatilidad; niveles Room/Strength/Breadth/Flow 1h/4h as-of, aceleración y pendiente Breadth (si existen 3 snapshots completos); concentración/densidad de REACTIONs 15/60m, mismo símbolo y sector cuando estaba documentado oportunamente. Metadatos as-of, calidad y digest de evidencia (candelas originales ya archivadas en `causal_context_v1.sqlite3`).
2. **POSTERIOR y separado** (`event_outcomes`): OPEN de la primera vela 1m posterior, MFE/MAE por horizontes 30/60/120/180/360m, tiempo hasta +1/+2/+3/+5%, MAE estrictamente *antes* de la vela que alcanza TP, MAE incluyendo esa vela como escenario conservador, pruebas TP/SL 1/0,5; 2/1; 3/1; 5/2, empate intravela `AMBIGUOUS_SL_FIRST`. Los horizontes incompletos no se consideran perdedores. Sin ejecución real, comisiones ni slippage.
3. **Prospectivo OPCIONAL si hay una fuente externa realmente observada ANTES del known_ts**: Open Interest (spot de contratos y delta 5m/15m sólo con dos muestras temporales suficientes), funding indicativo, taker-buy/taker-sell 5m/15m y CVD agregado de velas 1m, libro top5 (spread, imbalance y profundidad), liquidaciones USD observadas por snapshots 15m, con cobertura de conexión verificada. Si falta esa fuente o hay datos viejos se guarda `null`, no `0` ni valores inferidos. La marca `microstructure_status` diferencia cobertura parcial de total.

Los factores causales se suman al **Runner Analyzer** y al buscador 1D/2D/3D. El inspector original compartido ahora contiene el expander `Deep Research` para cada Event ID. El explorador consolida reglas que seleccionan **los mismos Event IDs de exploración**; sigue requiriendo grupos de control y validación cronológica por días.

## Limitaciones importantes

- El recolector NO usa Redis actual para reescribir REACTIONs antiguas. Su `feature_start_ms` se fija una vez y persiste entre reinicios. No hay backfill de contexto *fingido*.
- Las fuentes `reaction_lab_context` y `reaction_lab_auto_paths` deben haber archivado una muestra íntegra; de lo contrario una métrica derivada queda ausente.
- Las velas 1m registran **taker-buy acumulado**, no transacciones individuales. `cvd_15m` es una aproximación agregada en unidades base, NO tick-CVD. No se debe comparar el valor bruto entre monedas sin normalización.
- Los streams `!forceOrder@arr` de Binance publican **snapshots de liquidaciones**, no todas las liquidaciones: las sumas son un *mínimo observado*, no un total exhaustivo. Una desconexión da `null`, no cero.
- Un snapshot de libro tomado después del evento **nunca** se considera contexto causal. REST y WebSocket adicionales van en procesos independientes, deshabilitados hasta verificarlos. La edad del snapshot se expone como `telemetry_age_sec`.
- Los movimientos desde el OPEN son *resultados indicativos*, no operaciones reales: un STOP y TP tocados dentro del mismo OHLC 1m se clasifican conservadoramente, sin suponer qué vino primero.
- Las mejores combinaciones surgen de una búsqueda múltiple sobre señales dependientes. **No constituyen validación predictiva ni un sistema rentable**.

## Archivo por reemplazar en tu repositorio

- **Nuevos**: `reaction_lab_deep_metrics.py`, `reaction_lab_deep_collector.py`, `audit_reaction_lab_deep_research.py`, `reaction_lab_market_telemetry.py`, `reaction_lab_liquidations.py`, tres `.config.cjs`, `test_reaction_lab_deep_research.py`.
- **Actualizados**: `reaction_lab_runner_analyzer.py`, `reaction_lab_runner_patterns.py`, `reaction_lab_research_view.py`.
- Incluye por conveniencia los tests de regresión `test_reaction_lab_runner_analyzer.py`, `test_reaction_lab_runner_patterns.py`, `test_reaction_lab_no_scipy.py`.
- **Sin cambios**: `dashboard/app.py`, scanner, auto_paths, core context, runners, BTC Context y cinco PM2 de producción.

## Paso A: tests y prueba aislada sin ninguna red externa

Primero `git push origin microservices` desde Windows y en VPS:

```bash
cd /home/multiservices-1m
git pull --ff-only origin microservices
python3 -m unittest -v test_reaction_lab_deep_research.py test_reaction_lab_runner_analyzer.py test_reaction_lab_runner_patterns.py test_reaction_lab_no_scipy.py
python3 reaction_lab_deep_collector.py --check --project /home/multiservices-1m
python3 reaction_lab_market_telemetry.py --check --project /home/multiservices-1m
python3 reaction_lab_liquidations.py --check --project /home/multiservices-1m
```

El primer `--once` aislado fija SU PROPIO corte prospectivo en `tmp/deep_research_pilot.sqlite3`:

```bash
mkdir -p /home/multiservices-1m/tmp
python3 -u reaction_lab_deep_collector.py --project /home/multiservices-1m --db /home/multiservices-1m/tmp/deep_research_pilot.sqlite3 --once --limit 10
python3 reaction_lab_deep_collector.py --project /home/multiservices-1m --db /home/multiservices-1m/tmp/deep_research_pilot.sqlite3 --status
python3 audit_reaction_lab_deep_research.py --project /home/multiservices-1m --db /home/multiservices-1m/tmp/deep_research_pilot.sqlite3 --limit 10
```

**Si `causal_new=0` es normal:** el corte es nuevo y aún no hay REACTION posterior. No uses un `--db` arbitrario para simular inserción histórica en producción.

### Paso B: activar SOLO Deep Research derivado, después de revisar la salida del piloto

```bash
python3 -u reaction_lab_deep_collector.py --project /home/multiservices-1m --once --limit 20
python3 reaction_lab_deep_collector.py --project /home/multiservices-1m --status
pm2 start reaction_lab_deep_research_pm2.config.cjs --only reaction-lab-deep-research
pm2 logs reaction-lab-deep-research --lines 30 --nostream
```

Reiniciar solamente el panel después de los tests: `pm2 restart dash-micro1m`. Mantener los cinco colectores originales sin reiniciar.

### Paso C: fuente REST de microestructura — PRIMERO piloto, activación opcional

**No la actives junto con Paso B.** Esta fuente usa endpoints públicos `openInterest`, `premiumIndex`, `depth?limit=5`, `klines?interval=1m` de Binance Futures; son 4 requests por símbolo en cada barrido, no requiere API key y NO envía órdenes. Comprobar antes `df -h`, conectividad, cuotas y cobertura:

```bash
python3 -u reaction_lab_market_telemetry.py --project /home/multiservices-1m --once --max-symbols 3
python3 - <<'PY'
import sqlite3
p='reports/reaction_lab/telemetry_v1.sqlite3'
c=sqlite3.connect(f'file:{p}?mode=ro',uri=True)
print(c.execute('SELECT COUNT(*),MIN(received_ms),MAX(received_ms) FROM snapshots').fetchone())
print(c.execute('SELECT symbol,quality_json FROM snapshots ORDER BY received_ms DESC LIMIT 3').fetchall())
c.close()
PY
```

**Después de validar cuotas**, el archivo `reaction_lab_market_telemetry_pm2.config.cjs` trae un barrido de hasta 290 símbolos por ciclo con descanso 300s, lo que puede consumir ~1160 requests por ciclo. Modificar `--max-symbols` y `--poll-seconds` a los límites realmente seguros según el servidor. Ejecutar `pm2 start reaction_lab_market_telemetry_pm2.config.cjs --only reaction-lab-market-telemetry` **solo si aprueban los ensayos**. Si nunca activás REST, el núcleo causal derivado funciona igual y los factores OI/funding/book/CVD quedan faltantes.

### Paso D: liquidaciones — conexión extra independiente OPCIONAL

Requiere librería `websockets` instalada en el mismo Python usado por PM2 y salida WSS a `fstream.binance.com`. Primero ejecutar `--check` (sin red) y verificar uso de CPU/red/disco. Después, **solo con tu decisión explícita**:

```bash
pm2 start reaction_lab_liquidations_pm2.config.cjs --only reaction-lab-liquidations
pm2 logs reaction-lab-liquidations --lines 30 --nostream
```

La fuente se une sólo si hubo un registro de conexión válido durante toda la ventana anterior al retest. Una REACTION ya congelada no se reescribe para incorporar un feed iniciado tarde.

## Ver los datos

`Reaction & Swing Lab` → `Continuous V1` → `REACTION / Retests` → `Runner Analyzer` para buckets/minería; seleccioná cualquier Event ID en el inspector y abrí `Deep Research` para ver su estado causal, microestructura observada y resultados posteriores.

**Commit sugerido:** `feat(reaction-lab): persist deep causal runner features and optional microstructure`
