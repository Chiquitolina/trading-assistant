# Continuous V1 — Historical Replay & Gap Audit (read-only)

## Fuente canónica auditada

Recibidos desde commit declarado `3f848b8`: `reaction_lab_continuous_scanner.py`, `dashboard/app.py`, `engine/live/research/swing_detector.py` y servicio Redis. Esta entrega **no copia ni reemplaza esos archivos**. Importa `compile_canonical_scanner` del proyecto en uso, que compila funciones seleccionadas por AST (sin ejecutar la app Streamlit). No utiliza el replay general de compression/Candidate V2.

- Reproduce `15m/3x3`, prominencia 0, alejamiento 0.20%, tolerancia retest 0.10%, edad 360m, primera REACTION, `normalize_reactions` y Event ID `SYMBOL|SIDE|RETEST_TIMESTAMP`.
- Descarga OHLC de Binance USD-M Futures **1m y 15m nativos**, con paginación, caché JSON y verificación de OHLC. Excluye velas todavía abiertas según el **tiempo simulado**, no el reloj actual.
- A cada checkpoint inyecta por símbolo solamente las **últimas 400 velas cerradas** por timeframe, tal como `scan_once` del scanner real. Por defecto cada 15m; modo más exigente `--step-minutes 1` disponible para diagnosticar paridad. La ventana 400m excede la edad 360m en 40m.
- Registro de eventos: `first_replay_observed_ms` es tiempo histórico virtual; **NO** es el `first_seen_ms` de producción. `observation_type` del CSV `HISTORICAL_REPLAY_UNVERIFIED` no puede convertirse en `LIVE_OBSERVED`.
- Solo consulta la SQLite original con `mode=ro`. Jamás escribe en Redis, SQLite de producción, registros del scanner o servicios PM2. La caché y CSVs van a `--output`/`--cache-dir`.

## Instalación

Copia el ZIP a `/root/continuous_v1_replay_audit.zip` y ejecuta:

```bash
mkdir -p /root/continuous_v1_replay_tool
unzip -o /root/continuous_v1_replay_audit.zip -d /root/continuous_v1_replay_tool
cd /root/continuous_v1_replay_tool
python3 -m unittest -v test_replay_continuous_v1.py
```

## ETAPA 1: paridad contra un día observado (obligatoria)

```bash
python3 -u replay_continuous_v1.py \
  --project /home/multiservices-1m \
  --mode parity \
  --start '2026-10-13T00:00:00-03:00' \
  --end '2026-10-14T00:00:00-03:00' \
  --step-minutes 15 \
  --output /root/continuous_v1_parity_20261013 \
  --cache-dir /root/continuous_v1_klines_cache \
  2>&1 | tee /root/continuous_v1_parity_20261013.log
```

Puede descargarse un volumen importante de klines. Si ocurre un corte, reanuda exactamente con **los mismos** argumentos + `--resume`; los símbolos completados se guardan en `progress.json`. Si se cambia el hash del detector, las fechas o el universo, rehacer en otro directorio. No creas que `--max-symbols` prueba cobertura del universo completo: esa opción es exclusivamente de smoke test.

**Bloqueo de seguridad:** `mode=parity` retorna error cuando difieren Event IDs, campos numéricos, `source_hash` del journal o cuando hay fallas/gaps. Comprobar `summary.json`, `comparison.csv`, `download_errors.csv`. Una prueba por solo Event ID no certifica paridad numérica; el reporte las distingue.

Si Binance restringe HTTP al VPS, no se inventan datos ni se declara paridad: se reporta el error. Los datos del mercado reales no se pudieron descargar en el entorno de desarrollo local.

## ETAPA 2: reconstrucción (solo tras paridad completa)

```bash
python3 -u replay_continuous_v1.py \
  --project /home/multiservices-1m \
  --mode gap \
  --start '2026-10-14T12:40:00-03:00' \
  --end '2026-10-15T05:58:00-03:00' \
  --step-minutes 15 \
  --parity-summary /root/continuous_v1_parity_20261013/summary.json \
  --output /root/continuous_v1_gap_20261014 \
  --cache-dir /root/continuous_v1_klines_cache \
  2>&1 | tee /root/continuous_v1_gap_20261014.log
```

El modo gap **se niega a comenzar sin una paridad completa** del mismo hash, misma cadencia y universo completo. Comparará los Event IDs de ese intervalo contra `scanner_events_v1.sqlite3` en modo lectura. `not_in_journal.csv` muestra eventos detectados que no estaban guardados; **no los inserta**.

## Qué significan los archivos

- `summary.json`: conteos, paridad, hash canónico, errores, cobertura.
- `replay_events.csv`: Event IDs y campos estructurales causales, con procedencia `HISTORICAL_REPLAY_UNVERIFIED`.
- `comparison.csv`: `MATCHED`, `REPLAY_ONLY`, `JOURNAL_ONLY`, diferencias numéricas y de hash.
- `not_in_journal.csv`: eventos potencialmente faltantes; **solo confiables después de paridad satisfactoria y descargas íntegras**.
- `history_gaps.csv` y `download_errors.csv`: faltantes en datos fuente. No se interpretan como REACTIONs ausentes.
- `progress.json`: manifiesto para reanudación reproducible. No es salida de producción.

## Limitaciones metodológicas explícitas

1. La cadencia de 15m funciona con el margen de 40m entre 400 velas y edad máxima de 360m, pero **no sustituye la prueba empírica de paridad**. Si se encuentran diferencias, probar una franja más corta con `--step-minutes 1` y **nuevo directorio de salida**. No relajar el control hasta entender la causa.
2. El universo de símbolos proviene de `volume_exhaustion_events.csv` actual. No hay snapshots históricos del universo por día, por lo que cambios de listings requieren revisión.
3. El OHLC nativo puede diferir de datos históricamente recibidos por Redis. La prueba de paridad permite detectarlo.
4. No se recuperan de manera prospectiva `Strength`, `Market Flow`, rank de volumen/retorno 4h, telemetría ni snapshots BTC faltantes durante la caída. La reconstrucción es **solo del evento estructural V1**. Calcular la condición de cuatro factores exige una auditoría causal independiente y distinta procedencia.
5. No valida si un trade hipotético se podría haber ejecutado en vivo. Esta versión no simula TP/SL, ni envía órdenes, ni backfillea SQLite originales.
6. Si la ventana de gap no tiene ningún evento en el journal, un `REPLAY_ONLY` significa candidato reconstruido, no prueba de cobertura histórica absoluta hasta que se valide la paridad y la completitud de cada símbolo.

## Commit propuesto

`feat(reaction-lab): add read-only continuous-v1 historical parity and gap replay`
