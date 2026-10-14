# Runner TP/SL Matrix — auditoría de solo lectura (14-oct-2026)

Esta es una auditoría independiente del dashboard y del bot. No cambia configuraciones, SQLite, Redis, PM2, estrategias ni el `Deep Research` de producción.

## Entrada congelada

`runner_matrix_cohort_2026_10_14.csv` contiene las 1.311 REACTIONs con `path_state=COMPLETE` del CSV proporcionado el 14-oct-2026. La selección exacta de cuatro factores conserva **72 Event IDs**. El resto con `opposing_room_1h_pct` disponible son **1.221**; otros **18** sin Room se reportan separados y NO forman parte de la comparación equivalente.

## Ejecutar en el VPS

Copiar el ZIP a `/root/runner_tp_sl_matrix_audit_20261014.zip` por el método habitual, y ejecutar:

```bash
mkdir -p /root/runner_matrix_audit
unzip -o /root/runner_tp_sl_matrix_audit_20261014.zip -d /root/runner_matrix_audit
python3 /root/runner_matrix_audit/run_runner_tp_sl_matrix_audit.py \
  --project /home/multiservices-1m \
  --out /root/runner_matrix_results_20261014
```

La ejecución exige las funciones ya existentes `reaction_lab_auto_paths.decode_path` y `reaction_lab_deep_metrics.forward_metrics`. Lee **sólo** `reports/reaction_lab/auto_paths_v1.sqlite3` con conexión SQLite `mode=ro`; no necesita Binance ni Redis y no corre collectors.

## Resultado

- `matrix_summary.csv`: por escenario y grupo (cuatro factores, controles con Room, dirección), conteos TP/SL, `AMBIGUOUS_SL_FIRST`, tiempo agotado, promedio de retorno neto por señal y PF.
- `per_event_results.csv`: un registro por Event ID y escenario, incluyendo minutos hasta salida y PnL.
- `parity_comparisons.csv`: contrasta los TP/SL que ya existían en el CSV contra resultados del simulador de producción.
- `issues.csv`: missing paths, diferencias de OPEN, MFE o resultados del motor; debe investigarse antes de interpretar rentabilidad.

Asegurarse de que el log de salida diga **72 candidatos**, **1.311 paths validados** y **0 divergencias** en `LEGACY PARITY`; si algunos paths son faltantes o cambiaron, no extrapolar denominadores.

## Contrato de simulación

- Entrada = `OPEN` de la primera vela 1m posterior al retest.
- Máximo 180 velas consecutivas 1m, con verificación de que el path archivado sea `COMPLETE` y conserve 360 velas.
- Para paridad con el motor actual: TP se considera alcanzado con **ganancia estrictamente > TP**; SL con pérdida **>= SL**. Comparaciones de coma flotante heredan la semántica de producción, incluso cerca de la igualdad exacta.
- Si ambas barreras cruzan dentro de la misma vela, se registra `AMBIGUOUS_SL_FIRST` y se contabiliza como pérdida. No conocemos el orden intravela.
- Si no se alcanza barrera, salida estimada al último `CLOSE` de los 180 minutos.
- PnL como % sobre nocional SIN apalancamiento; comisión por ida y vuelta configurable con `--fees 0.10` (0,05% en entrada y 0,05% salida).
- NO incluye spread, slippage, financiación, orden real, prioridad entre señales simultáneas ni limitación de un solo slot. PF de señales independientes NO representa la equity de un portfolio real.
- La selección de los cuatro factores fue elegida con esta misma muestra; sus diferencias son **exploratorias**, no validación fuera de muestra.

**Matriz:** TP 2% / SL 0,5%, 1%, 1,5%; TP 3% / SL 1%, 1,5%, 2%.

## Verificación técnica

En el entorno de desarrollo se ejecutaron cuatro pruebas sobre el simulador reconstruido: coherencia de la cohorte de 1.311/72, coincidencia con escenarios legacy en velas sintéticas LONG y SHORT, ambigüedad intravela y prueba integral sobre SQLite sintético. Las pruebas fueron satisfactorias; la paridad real en VPS depende de recuperar sus paths originales.
