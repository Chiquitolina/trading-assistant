# Auditoría de entrada por retroceso — Runner Lab (14 oct 2026)

## Pregunta

¿Podemos capturar los runners que tocan SL 1% antes de alcanzar +2% colocando órdenes de compra/venta LIMIT a retrocesos **predeterminados**, sin elegir retrospectivamente el MAE mínimo?

**Solo lectura** sobre `/home/multiservices-1m/reports/reaction_lab/auto_paths_v1.sqlite3`. No cambia estrategias, posiciones, PM2 ni SQLite de producción. Usa cohorte congelada 1.311 REACTIONs con paths completos de 360m; 72 cumplen 4 factores. Audita paridad original TP2_SL1 con 18 TP, 49 SL, 5 NO_HIT.

## Modelo

- Antes de ver velas futuras, se fija el precio base: OPEN siguiente al retest.
- LONG: BUY LIMIT al `open_original × (1 - profundidad/100)`.
- SHORT: SELL LIMIT al `open_original × (1 + profundidad/100)`.
- Profundidades: 0,5 / 1 / 1,5 / 2 / 2,5 / 3 %.
- Orden vigente por 5 / 15 / 30 / 60 minutos. No rellenar una orden que no toca el nivel.
- Si toca, se asume fill ideal al **precio límite** (sin mejora por gaps). Tiempo desde fill de **180 velas 1m**, posible porque cada path tiene 360m.
- TP 2%/3% relativo al precio límite, SL 0,5%/1%/1,5%/2% (las seis combinaciones del estudio anterior). Comisiones round-trip 0,10% nocional, sin leverage.
- Mismas condiciones que el simulador Legacy: gain **> TP**, adverse **>= SL**. Cruces simultáneos TP+SL después del fill = stop first conservador.
- **En la vela del fill nunca se acredita un TP:** el HIGH/LOW puede haberse registrado antes de que la LIMIT ejecutara. Se cuenta el SL de la vela del fill si se cruza y se marca `fill_bar_tp_possible_before_fill` para inspección. Esto proporciona resultados conservadores, no una determinación exacta del orden intrabar.
- Matriz por 4 factores, resto comparable y subgrupos de los 72 (`RUNNER_TP_FIRST`, `RUNNER_SL_FIRST`, `SELECTED_NO_RUNNER`). Estos subgrupos se usan **solo para diagnóstico**, nunca para decidir la orden.
- Se comparan los resultados con la entrada original **para los mismos Event IDs que hubieran activado la LIMIT**, con su propio entry original y TP/SL.
- Los no-fills no se cuentan como pérdidas. El reporte incluye rentabilidad por fill y por señal original (no-fill = 0).

## Limitaciones

**Exploratorio in sample:** las 72 señales se eligieron en esta muestra y los retrocesos se investigaron después de ver sus resultados. No representa una validación prospectiva. La asunción de fill ideal ignora spread, slippage, cola de órdenes, rechazos, fees maker/taker específicos y latencia. No hay posición simultánea/slot de portfolio ni funding. Una vela que toca la entrada y también TP no informa el orden intrabar: se excluye el TP de esa vela y se informa la cantidad marcada como incierta. Los primeros instantes de vida de una LIMIT requerirían medir el tiempo efectivo de publicación en real; esta auditoría considera que estaba activa desde el timestamp `known_ts`.

## Ejecutar en VPS

Primero subir ZIP como `/root/pullback_limit_audit_20261014.zip`. Luego:

```bash
mkdir -p /root/pullback_limit_audit_20261014
unzip -o /root/pullback_limit_audit_20261014.zip -d /root/pullback_limit_audit_20261014
cd /root/pullback_limit_audit_20261014
python3 -m unittest -v test_pullback_limit_audit.py
python3 run_pullback_limit_audit.py \
  --project /home/multiservices-1m \
  --out /root/pullback_entry_results_20261014
```

Resultados:

- `limit_matrix_summary.csv`: matriz de tasas de fill, TP/SL, retorno medio neto, PF y comparación emparejada.
- `selected_72_limit_entries.csv`: detalle por evento seleccionado y escenario.
- `README.txt`: supuestos y advertencias.

Comprimir para compartir:

```bash
cd /root/pullback_entry_results_20261014
zip -j /root/pullback_entry_results_20261014.zip \
  limit_matrix_summary.csv selected_72_limit_entries.csv README.txt
```

No usar resultados para modificar bot real sin revisar fills inciertos y validación Forward.
