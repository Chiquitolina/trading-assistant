# HLL · Diccionario de Huella Estructural PRE V1

**Objetivo:** describir matemáticamente cualquier corte PRE seleccionado de un intento de `CROSS_UP`, para luego buscar semejanzas entre mecanismos diferentes. Una huella no es una señal, un clasificador A/B/C ni una predicción de ruptura.

## Contrato causal e identidad

- **Unidad:** una huella por `Attempt ID` y `pre_closed_ts`; el `Episode ID` identifica el agrupamiento retrospectivo, pero no participa en los cálculos.
- **Velas:** 30m con `timestamp` de apertura; únicamente `open_ts < PRE` y `close_ts <= PRE`; ventanas 96 y 288 calculadas independientemente.
- **Swings:** `close_reversal_pivots` existente con reversión de cierre, 1 ATR por defecto; `pivot_open_ts` es el extremo y `confirmed_known_closed_ts` indica cuándo se conoció.
- **Normalización:** distancias/amplitudes en ATR vigente en el corte; duraciones en velas; ratios y fracciones sin dimensión.
- **Dato ausente:** `null`/CSV vacío si faltan pivotes, el denominador es cero o no hay cobertura histórica; no se sustituye por 0.
- **Sin lookahead:** no se usan candles POST, respuestas del mercado, notas visuales, ni `episode_attempt_count` final como variable.
- **Cobertura inicial:** 15 episodios y 40 cortes, incluidos los primeros intentos y revisiones posteriores. La calificación original no se reasigna al intento #1.
- **Universo de aplicación previsto:** episodios históricos condicionados a haber presentado `CROSS_UP`; una búsqueda anticipada *antes* del cruce requerirá ampliar la cohorte.

## Catálogo medible (35 variables · 6 familias)

El campo **Estado** identifica si la definición se reutiliza de Anatomy V1 (`reused_anatomy`), se deriva de pivotes/cierres confirmados (`derived_*`) o es un descriptor **experimental** (`experimental_*`). Los experimentales NO deben tratarse como evidencia de calidad sin contrastación.

### Swings y geometria

| ID | Unidad | Ventana | Definición / limitación | Estado |
|---|---|---:|---|---|
| `geo_low_up_share_96` | fraccion | 96 | Proporcion de cambios positivos entre LOW confirmados consecutivos en PRE 96; minimo 2 LOW. | `derived_confirmed_pivots` |
| `geo_high_up_share_96` | fraccion | 96 | Proporcion de cambios positivos entre HIGH confirmados consecutivos en PRE 96; minimo 2 HIGH. | `derived_confirmed_pivots` |
| `geo_low_progress_last3_atr_96` | ATR | 96 | (Ultimo LOW menos antepenultimo LOW)/ATR del corte; requiere 3 LOW confirmados. | `reused_anatomy` |
| `geo_high_progress_last3_atr_96` | ATR | 96 | (Ultimo HIGH menos antepenultimo HIGH)/ATR del corte; requiere 3 HIGH confirmados. | `reused_anatomy` |
| `geo_confirmed_pivots_96` | cantidad | 96 | Numero de pivotes confirmados por reversion de cierre en 96 velas. | `reused_anatomy` |
| `geo_leg_amplitude_cv_96` | ratio | 96 | Desvio poblacional/amplitud media ATR de tramos entre pivotes confirmados; minimo 3 tramos. | `derived_confirmed_legs` |

### Reconstruccion

| ID | Unidad | Ventana | Definición / limitación | Estado |
|---|---|---:|---|---|
| `rebuild_last_low_step_atr_96` | ATR | 96 | LOW2 menos LOW1 en ultima cadena L-H-L-H plenamente confirmada, en ATR. | `reused_anatomy` |
| `rebuild_last_high_step_atr_96` | ATR | 96 | HIGH2 menos HIGH1 en ultima cadena L-H-L-H plenamente confirmada, en ATR. | `reused_anatomy` |
| `rebuild_last_correction_fraction_96` | fraccion | 96 | (H1-L2)/(H1-L1) de ultima cadena confirmada; puede exceder 1. | `derived_confirmed_chain` |
| `rebuild_second_over_first_rise_96` | ratio | 96 | (H2-L2)/(H1-L1) de ultima cadena confirmada; no implica ruptura. | `derived_confirmed_chain` |
| `rebuild_chain_count_96` | cantidad | 96 | Cantidad de cadenas L-H-L-H completamente confirmadas dentro de 96 PRE. | `reused_anatomy` |

### Compresion

| ID | Unidad | Ventana | Definición / limitación | Estado |
|---|---|---:|---|---|
| `compress_residual_osc24_atr_96` | ATR | 96 | P90-P10 de residuos de cierres tras quitar deriva lineal por extremos en ultimas 24 velas; /ATR. | `reused_anatomy` |
| `compress_residual_ratio_24_96` | ratio | 96 | Oscilacion residual ultima 24 / oscilacion residual 24 anterior. No es rango total ni label de calidad. | `reused_anatomy` |
| `compress_downleg_ratio_new_old_96` | ratio | 96 | Mediana de ultimas 2 caidas confirmadas dividida por mediana de 2 caidas anteriores; requiere 4 DOWN. | `derived_confirmed_legs` |
| `compress_downleg_cv_96` | ratio | 96 | Coeficiente de variacion de amplitud de caidas confirmadas; requiere 3 DOWN. | `derived_confirmed_legs` |

### Relacion con resistencia

| ID | Unidad | Ventana | Definición / limitación | Estado |
|---|---|---:|---|---|
| `level_close_distance_atr_96` | ATR | 96 | (borde inferior de resistencia - cierre PRE)/ATR; negativo si cierre esta por encima. | `reused_anatomy` |
| `level_near_share_48_96` | fraccion | 96 | Fraccion de 48 cierres entre (zone_low-0.5 ATR) y zone_high. | `reused_anatomy` |
| `level_near_share_24_96` | fraccion | 96 | Fraccion de ultimos 24 cierres entre (zone_low-0.5 ATR) y zone_high. | `derived_closed_candles` |
| `level_approach_rearm_count_96` | cantidad | 96 | Entradas en banda cercana tras observar un cierre debajo de (zone_low-1 ATR); se rearma solo tras otra salida bajo -1 ATR. NO es Touch V2.6. | `experimental_heuristic` |
| `level_above_zone_share_48_96` | fraccion | 96 | Fraccion de ultimos 48 cierres por encima de zone_high; no es confirmacion de flip. | `derived_closed_candles` |
| `level_zone_position_288` | ratio | 288 | (centro de zona - minimo OHLC 288)/(maximo OHLC 288-minimo OHLC 288); puede estar fuera [0,1]. NO mide fuerza del nivel. | `reused_anatomy` |
| `level_max_depth_below_48_atr_96` | ATR | 96 | Maximo alejamiento por debajo de zone_low de LOW OHLC 48 en ATR. | `reused_anatomy` |

### Madurez temporal

| ID | Unidad | Ventana | Definición / limitación | Estado |
|---|---|---:|---|---|
| `maturity_first_low_known_age_96` | velas | 96 | Velas desde la CONFIRMACION del primer LOW que el reconstruidor de 96 detecta, hasta PRE. No es inicio real de formacion. | `experimental_window_bounded` |
| `maturity_first_low_known_age_288` | velas | 288 | Velas desde la CONFIRMACION del primer LOW del reconstruidor independiente de 288. Puede diferir de PRE96. | `experimental_window_bounded` |
| `maturity_last_low_known_age_96` | velas | 96 | Velas desde CONFIRMACION del ultimo LOW conocido en PRE96. | `reused_anatomy` |
| `maturity_last3_low_span_bars_96` | velas | 96 | Barras transcurridas entre vela extrema del primero y tercero de los ultimos 3 LOW confirmados. | `reused_anatomy` |
| `maturity_near_tail_streak_bars_96` | velas | 96 | Velas consecutivas al final de PRE con cierre en [zone_low-0.5 ATR, zone_high]. | `derived_closed_candles` |
| `maturity_last_chain_age_bars_96` | velas | 96 | Velas desde confirmacion de H2 en ultima cadena L-H-L-H confirmada; no incluye confirmaciones futuras. | `reused_anatomy` |

### Calidad del movimiento (descriptiva)

| ID | Unidad | Ventana | Definición / limitación | Estado |
|---|---|---:|---|---|
| `movement_signed_efficiency_24_96` | [-1,1] | 96 | (ultimo cierre-primer cierre)/suma de cambios absolutos, ultimas 24 velas. Nulo si no hubo cambios. | `reused_anatomy` |
| `movement_signed_efficiency_48_96` | [-1,1] | 96 | (ultimo cierre-primer cierre)/suma de cambios absolutos, ultimas 48 velas. Nulo si no hubo cambios. | `reused_anatomy` |
| `movement_net_advance_48_atr_96` | ATR | 96 | Ultimo cierre menos primer cierre de ultimas 48 velas, /ATR PRE. | `derived_closed_candles` |
| `movement_latest_lhl_retention_96` | fraccion | 96 | (LOW posterior - LOW previo)/(HIGH - LOW previo) de ultimo L-H-L confirmado. Puede salir de [0,1]. | `reused_anatomy` |
| `movement_median_lhl_retracement_96` | fraccion | 96 | Mediana de (HIGH - LOW posterior)/(HIGH - LOW anterior) para L-H-L confirmados. | `reused_anatomy` |
| `movement_max_close_drawdown_48_atr_96` | ATR | 96 | Maximo retroceso desde maximo acumulado de cierres durante ultimas 48 velas, /ATR. | `derived_closed_candles` |
| `movement_up_close_steps_48_96` | fraccion | 96 | Proporcion de incrementos positivos entre cierres consecutivos de ultimas 48 velas. | `derived_closed_candles` |

## Qué **no** es todavía la huella

- No incluye un puntaje general de calidad ni determina si una vela es de compra o venta.
- `level_zone_position_288` mide ubicación relativa dentro de un rango, **no** prueba que una resistencia sea fuerte o débil.
- `level_approach_rearm_count_96` es un conteo heurístico de cierres con rearmado en banda, **no** es el detector independiente Touch V2.6 ni el número de CROSS_UP.
- Las métricas por ventana 96 no se empalman con los pivotes del análisis 288: cada detector empieza en su propia ventana.
- Las métricas "maturity_first_low_known_age" son **acotadas por ventana**; no descubren de forma fiable dónde empezó toda la estructura.
- El `rebuild_*` usa la **última** cadena `LOW-HIGH-LOW-HIGH` confirmada, no la mejor escogida mirando un gráfico a posteriori.
- El buscador universal, los pesos configurables y el botón de Streamlit corresponden a **etapas 3 y 4**, no se fingen como implementados.

## Próxima implementación: motor universal de similitud

1. Un índice de huellas por corte; al buscar, se elige exactamente el `Attempt ID` que se está visualizando.
2. El usuario selecciona una o varias de las seis familias y (más adelante) pesos por variable.
3. Normalización robusta ajustada en una cohorte independiente; **misma escala/unidad/ventana** y política de faltantes explícita.
4. Distancia por familia, distancia combinada explicable y motivos de coincidencia/diferencia. Sin presentar porcentajes como probabilidades.
5. Deduplicación visual por `Episode ID`; se preserva ranking completo sin ocultar cortes alternativos.
6. Mostrar gráficos PRE de referencia y resultados, navegar del resultado a otra búsqueda y contrastar con episodios nuevos.

## Criterio de aceptación de esta V1

- Las 35 variables son calculables por corte y sus definiciones se pueden auditar.
- La función no recibe `post`, `grade` ni resultados futuros; se prueba invariancia a modificaciones de datos posteriores a PRE.
- Las huellas se guardan con identificadores, fecha PRE, escala ATR y estado de cobertura por familia.
- Se mantiene el corte real, incluido BAND #2, BICO #2 y AXL #5, sin reemplazar por #1.
- En referencias, `15` episodios y `40` cortes, con independencia de ventanas 96/288.

## Versionado

`HLL_STRUCTURAL_FINGERPRINT_PRE_V1` · zona de resistencia y ATR pertenecen al `Attempt ID` registrado. Si se cambia cualquier fórmula, detector de pivotes, escala o definición de banda, generar **otra versión** para no mezclar huellas incompatibles.

