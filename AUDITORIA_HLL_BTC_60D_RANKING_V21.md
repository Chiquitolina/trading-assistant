# Auditoría de importancia HLL V2.1 · BTC 60 días

**Fecha:** 2026-11-08 · **Alcance:** offline/read-only; BTCUSDT · **Fuente original:** `btc_60d_hierarchy_v1` · **Subzonas originales:** `btc_60d_structural_v2`. Esta auditoría no cambia ninguna corrida congelada ni operaciones LIVE.

## Fuentes y control de integridad

- El paquete aportado contiene 10 archivos: 6 de Hierarchy V1 y 4 de Structural Zones V2.
- Los hash SHA-256 de los 8 CSV coinciden con los manifiestos originales.
- 3.602 velas 30m verificadas, del 25/08 al 08/11 (75,04 días), con 60 días visibles y ~15 de calentamiento.
- 93 horizontales V1, 6.988 eventos registrados; 58 subzonas al último corte; 83 vínculos confirmados y 35 fusiones de linaje.

## Diagnóstico reproducible del problema

- Horizontales V1: 61 de 93 en A (65,6%).
- Subzonas V2: 42 de 58 en A (72,4%); **4** subzonas con score heredado 100.
- La V2 actual utiliza `score_method=MAX_MEMBER_ASOF_NOT_ZONE_CALIBRATED`: la importancia de una subzona es el máximo puntaje entre sus líneas integrantes, no una puntuación propia.
- El Dashboard anterior selecciona las primeras diez por `importance_score` y `members` y no diversifica las franjas de precio ni los momentos de actividad.
- De las diez primeras, 4 centros se encuentran entre 76.000 y 77.000 USDT; no son necesariamente las estructuras dominantes de todo el período.
- Hay 29 subzonas con una sola horizontal y 29 subzonas multilínea; V2 no debe tratar una única línea como estructuralmente inválida.
- Estados al corte de las 58 subzonas: 40 `ACTIVE_TTL`, 13 `EXPIRED`, 5 `PARTIAL_EXPIRED`. TTL **no equivale** a aceptación/defensa actual; se conserva por separado.

## Experimento V2.1: evidencia propia y puntuación relativa

V2.1 es una capa de lectura **sin cambiar** `hll_hierarchical_levels_v1.py` o `hll_structural_zones_v2.py`. Utiliza episodios ya deduplicados por subzona desde su nacimiento (`tests_since_zone_birth`, `defenses_since_birth`, `losses_since_birth`), su edad y su número de integrantes con rendimientos decrecientes:

`E = 16 ln(1+tests) + 6 ln(1+defensas+pérdidas) + 4 ln(1+min(edad,60)/7) + 2.5 ln(1+integrantes)`

**E** es un índice *sin techo de 100* que expresa evidencia acumulada, no rentabilidad, probabilidad de defensa ni una fuerza validada. Las pérdidas cuentan como interacción con la zona y su estado se muestra en otro campo. La clase se asigna por percentiles respecto de las subzonas que ya existían al corte: A si percentil ≥85 y ≥3 tests; B si ≥60 y ≥2 tests; C si ≥30 y ≥1 test; D en los demás casos. Estos umbrales son experimentales y no calibrados por resultados posteriores.

Resultado BTC al corte del 08/11/2026 01:00 UTC: **A=9, B=15, C=17, D=17**. No significa que esas nueve zonas sean estadísticamente buenas; son las más destacadas por este criterio relativo.

### Mapa de referencia: solo categoría A, 0,70% de separación

Este mapa elige hasta diez subzonas y deja de agregar líneas si se superponen según su separación de centros. Prioriza las tres etapas históricas de la ventana (septiembre/octubre/noviembre) y luego completa por mayor evidencia sin duplicar bandas. **Selecciona 5 zonas, de nueve A elegibles; las otras cuatro permanecen disponibles en el inspector.**

| Centro BTC (USDT) | Tier | Evidencia E | Percentil | Líneas | Tercio histórico | TTL |
|---:|:---:|---:|---:|---:|---:|---|
| 78,306 | A | 81.7 | P93 | 1 | 1 | EXPIRED |
| 83,778 | A | 87.6 | P100 | 1 | 2 | ACTIVE_TTL |
| 81,157 | A | 86.3 | P98 | 1 | 3 | ACTIVE_TTL |
| 80,586 | A | 83.4 | P97 | 3 | 3 | PARTIAL_EXPIRED |
| 78,962 | A | 79.5 | P91 | 1 | 1 | EXPIRED |

Con **A+B** hay 10 zonas visibles sobre 24 elegibles con la misma separación espacial.

### Precisiones causales

- Cada score se calcula *as of* del corte utilizando solo las zonas existentes; los eventos cuya fecha es posterior al corte se excluyen.
- Una subzona fusionada no se dibuja antes de su `created_at`.
- La selección de qué zonas mostrar en un gráfico de 60 días utiliza información conocida en el **corte final**: es un mapa retrospectivo, **no** debe presentarse como una predicción disponible al comienzo de septiembre.
- Los percentiles cambian cuando se incorporan nuevas estructuras; esto es evolución causal, no necesariamente repainting indebido.
- El modo legado V2 queda accesible en el mismo Dashboard para comparación y auditoría. No se alteran los `level_id`, `zone_id`, vínculos, linajes, CSV, manifiestos ni SHA anteriores.
- Los niveles vencidos no se prolongan más allá del último cierre observado de sus miembros; se distingue estado TTL y respuesta estructural.

## Archivos fuente V2.1 entregados

1. `hll_structural_zone_ranking_v21.py` — nuevo: puntuación propia, grado relativo, segmentación de época y selección espacial.
2. `dashboard/views/hll_hierarchical_levels_view.py` — archivo completo: integrar modo V2.1, ranking histórico equilibrado, inspección de todas las subzonas elegibles, mantener modo V2 anterior.
3. `test_hll_structural_zone_ranking_v21.py` — nuevo: 7 tests de jerarquía, deduplicación, exclusión de eventos futuros, agrupación y gráfico.
4. `LEEME_HLL_STRUCTURAL_RANKING_V21.md` — guía.

**Tests locales:** 44/44 aprobados (37 existentes + 7 nuevos). Aún no ejecutados en el VPS.

## Cómo validar en el VPS

Subir solamente los tres `.py` completos a sus rutas originales mediante WinSCP o integrar por Git; **no instalar scripts**. Luego:

```bash
cd /home/multiservices-1m
python3 -m unittest -v \
  test_hll_structural_zone_ranking_v21 \
  test_hll_structural_zones_v2 \
  test_hll_hierarchical_levels_v1 \
  test_horizontal_level_lifecycle_v21
```

En **Horizontal Breakout Lab → Jerarquía de horizontales**, elegir `btc_60d_hierarchy_v1`, 60 días, 4h, `Subzonas estructurales V2`, la escala **V2.1**, **Equilibrar períodos históricos y franjas de precio**, filtro **Solo A**, separación 0,70% y máximo 10. Comparar con el modo **V2 original**.

## Próxima investigación

1. Verificar visualmente las 5 A seleccionadas y 4 A no dibujadas, y comparar con A+B y el modo legado.
2. Inspeccionar zonas individuales vs. multilínea, sus `level_id` y linajes.
3. Hacer cortes históricos anteriores para comprobar cambios de pertenencia y ranking (sin conocimiento futuro).
4. Validar con otros símbolos y ventanas temporales. Solo después ajustar pesos/umbrales y cruzar con First Touch/REACTION; no calibrar con el mismo BTC usado para diseñar.

**Commit propuesto:** `feat(hll): add relative structural zone ranking and balanced 60d map v2.1`.
