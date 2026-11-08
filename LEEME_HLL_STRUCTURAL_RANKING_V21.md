# HLL Structural Ranking V2.1 · lectura y cambios

Este paquete agrega una capa experimental de importancia relativa por SUBZONA al dashboard HLL existente. NO vuelve a procesar velas ni altera el detector HLL V2.1 / Structural Zones V2; trabaja sobre corridas congeladas ya existentes.

## Copiar al VPS (rutas originales)

- `hll_structural_zone_ranking_v21.py` → `/home/multiservices-1m/hll_structural_zone_ranking_v21.py`
- `dashboard/views/hll_hierarchical_levels_view.py` → `/home/multiservices-1m/dashboard/views/hll_hierarchical_levels_view.py`
- `test_hll_structural_zone_ranking_v21.py` → `/home/multiservices-1m/test_hll_structural_zone_ranking_v21.py`

El `git pull` anterior YA incorporó Structural Zones V2; estos archivos son una versión posterior y no deben reemplazarse por el ZIP V2 anterior.

## Pruebas (solo pruebas, no instaladores)

```bash
cd /home/multiservices-1m
python3 -m unittest -v \
  test_hll_structural_zone_ranking_v21 \
  test_hll_structural_zones_v2 \
  test_hll_hierarchical_levels_v1 \
  test_horizontal_level_lifecycle_v21
```

## Abrir vista

Horizontal Breakout Lab → Jerarquía de horizontales:
- Corrida HLL: `btc_60d_hierarchy_v1`;
- Modo: `Subzonas estructurales V2 · causales`;
- Corrida V2: `btc_60d_structural_v2`;
- Nuevo criterio de importancia: `V2.1 · evidencia propia y ranking relativo`;
- Nuevo modo visual: `Equilibrar períodos históricos y franjas de precio`;
- Importancia mínima: `Solo A`, días: `60`, velas: `4h`, separación: `0.70%`, máximo: `10`.

Con esos valores, auditoría local reproduce 9 A elegibles y 5 zonas A suficientemente separadas en el mapa; todas las A pueden inspeccionarse desde el selector. Cambiar a A+B puede mostrar 10 zonas y sirve para evaluar cobertura del histórico.

El modo `V2 original · máxima puntuación de integrante` permite ver la versión anterior sin cambios para comparación. El score relativo `E` NO es probabilidad ni confianza estadística, y la clasificación A/B/C/D de V2.1 sigue sin validación fuera de muestra. No hay cambios en LIVE.

**Commit sugerido:** `feat(hll): add relative structural zone ranking and balanced 60d map v2.1`
