# HLL — Huella Estructural PRE V1

**Esta entrega corresponde SOLO a etapas 1 y 2:** catálogo y cálculo de huellas. **No** incorpora aún `Buscar similares` al dashboard, ni modifica LIVE o los módulos anteriores.

## Instalación sin instalar paquetes

El ZIP para WinSCP contiene **dos `.py` nuevos**:

| Archivo | Destino en el VPS |
|---|---|
| `hll_structural_fingerprint_v1.py` | `/home/multiservices-1m/` |
| `test_hll_structural_fingerprint_v1.py` | `/home/multiservices-1m/` |

Los `.md` son documentación; podés guardarlos donde quieras. Se usan las dependencias de Anatomy V1 que ya subiste, en particular `hll_structure_anatomy_pre_v1.py` y `hll_structure_anatomy_first_pre_v1.py`, además de sus módulos de pivotes y OHLC. Se lee el paquete de fuentes previo `HLL_FUENTES_ANATOMIA_PRE_V2.zip` y el mapa ya presente en `reports/horizontal_level_structural_overlap_audit_v1/full290_v1/attempt_episode_map.csv`.

## Ejecutar

```bash
cd /home/multiservices-1m
python3 -m unittest -v test_hll_structural_fingerprint_v1
python3 hll_structural_fingerprint_v1.py --plan
python3 hll_structural_fingerprint_v1.py --run
```

Salida por defecto:

`reports/horizontal_level_structural_fingerprint_v1/reference_15_v1/`

- `fingerprints_pre.csv`: una fila por corte (`Attempt ID`), 35 variables y cobertura de las 6 familias.
- `fingerprints_preview_pre.html`: visor local de valores **reales**, por símbolo y corte; no es una galería de velas ni un score.
- `feature_catalog.json`: especificación estructurada, unidades y procedencia.
- `coverage_by_family.csv`: cobertura numérica, no porcentaje de calidad.
- `manifest.json`: versión, huellas de origen y protección de PRE.

El programa **se niega a sobrescribir** un directorio de resultados existente. Para una repetición, indicar otra ruta nueva con `--output` (no borrar resultados anteriores sin auditarlos).

## Validación local sobre los datos compartidos

- 17 tests aprobados.
- 15 episodios, 40 cortes, 35 variables, 6 familias.
- 1.395 celdas con valor de 1.400; 5 insuficientes conservadas en blanco.
- Contratos: últimos cierres PRE, 96/288 independientes, pivotes con fecha de confirmación, ninguna etiqueta A/B/C en variables.

## Cómo seguimos

**Etapa 3:** motor universal de similitud por familias seleccionables, pesos y ranking explicable. Primero offline sobre resultados nuevos; después **etapa 4**, integrar a la versión actual del dashboard sin pisar sus filtros. 

**Commit sugerido:** `research(hll): add causal six-family structural fingerprint v1`
