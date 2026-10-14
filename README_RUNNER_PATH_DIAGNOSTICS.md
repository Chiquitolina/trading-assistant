# Runner TP/SL 2%/1% — segunda auditoría del recorrido

## Objetivo

Explicar los resultados de la matriz sobre la cohorte inmutable de **1.311 REACTIONs COMPLETE** (72 con los cuatro factores). Diagnóstico individual de los 180 minutos siguientes al retest, desde el OPEN siguiente.

No se modifican el bot, el scanner, las SQLite ni los procesos PM2. SQLite se abre en modo `ro`. Los CSV se escriben en un directorio de salida separado.

## Contenido del ZIP

- `analyze_runner_path_diagnostics.py`: analizador en modo lectura, compatible con Python 3.10.
- `runner_matrix_cohort_2026_10_14.csv`: cohorte fija del primer análisis, 1311 filas.
- `test_runner_path_diagnostics.py`: pruebas de cruces temporales, LONG/SHORT, velas ambiguas y semántica de límites.

## Instalación

Subir ZIP a `/root/runner_path_diagnostics_20261014.zip` y ejecutar:

```bash
mkdir -p /root/runner_path_diagnostics_tool
unzip -o /root/runner_path_diagnostics_20261014.zip -d /root/runner_path_diagnostics_tool
cd /root/runner_path_diagnostics_tool
python3 -m unittest -v test_runner_path_diagnostics.py
python3 analyze_runner_path_diagnostics.py \
  --project /home/multiservices-1m \
  --out /root/runner_path_diagnostics_results_20261014 \
  --prior /root/runner_matrix_results_20261014/per_event_results.csv
```

Los outcomes se contrastan contra `reaction_lab_deep_metrics.forward_metrics()` de la instalación real. Opcionalmente se comparan contra el primer CSV `per_event_results.csv`, si está disponible.

## Archivos de resultado

- `report.md`: resumen legible.
- `diagnostic_summary.csv`: medianas por clase y dirección.
- `selected_72_diagnostics.csv`: 72 señales con clasificación, orden y tiempos de cruces.
- `all_events_diagnostics.csv`: cohorte completa para comparar con controles.
- `issues.csv` / `mismatches.csv`: diagnósticos y fidelidad.

Para transferir los archivos:

```bash
cd /root/runner_path_diagnostics_results_20261014
zip -j /root/runner_path_diagnostics_results_20261014.zip \
  report.md diagnostic_summary.csv selected_72_diagnostics.csv \
  all_events_diagnostics.csv issues.csv mismatches.csv
```

## Cómo se interpretan los campos

- `RUNNER_TP_FIRST`: alcanzó más de 2% antes de SL 1% (entrada inmediata).
- `RUNNER_SL_FIRST`: MFE >=2% durante 180m, pero el SL 1% se tocó primero; no constituye una operación rentable.
- `SELECTED_NO_RUNNER`: no alcanzó MFE 2% en 180m.
- `first_sl_1_min` y `first_tp_2_min`: instantes desde el OPEN siguiente al retest (se comienzan a contar en 1).
- `sl1_then_tp2`: cruces en velas **distintas** y orden comprobable; `tp2_and_sl1_same_min` resguarda orden intravela indeterminado.
- `initial_5m_close_pct`, `initial_10m_close_pct`: datos POSentrada para entender el comportamiento, **no** señales disponibles en el instante inicial.

La salida NO es una recomendación operativa, ni valida los cuatro factores fuera de muestra. Los thresholds fueron encontrados usando este mismo archivo (~39h), y las señales entre símbolos pueden estar altamente correlacionadas. Tampoco modela slots, slippage ni latencia de entrada.

**Commit:** ninguno. Investigación de solo lectura.
