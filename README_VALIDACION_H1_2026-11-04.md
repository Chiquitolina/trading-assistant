# Reaction Lab — H1, validación fuera de muestra (freeze 2026-11-04)

## Hipótesis exacta

Entre las **REACTIONs originales SHORT de swings HIGH 15m**, aquellas cuyo swing tiene una resistencia HIGH 30m confirmada, **ACTIVA** y **CERCANA <=0,20%** y además una resistencia HIGH 1h confirmada y **ACTIVA** (sin exigir cercanía), deberían tener mayor probabilidad de alcanzar un movimiento SHORT de **-0,5%** antes de invalidar por **mecha** el swing HIGH, en los **30 minutos posteriores** a la señal.

Referencia: todas las demás REACTIONs SHORT originales de 15m con contexto HTF conocido y resultado 30m completo. Incluye ausencia de candidatos y candidatos invalidados como comparador, pero **no `UNKNOWN_HISTORY`**. No se seleccionan los grupos ni los horizontes después de observar holdout.

**Data pre-reg**: `REACTION_H1_FROZEN_V1.json`. El validador verifica su SHA-256 exacto **incluido en el código**, y el SHA del plan original: `5f6bccb690dc5f7a69de459888d1d3a7a5128dd1bc590de9633f746cb8a24aca`.

## Resultado de discovery (no usar como confirmación)

30 símbolos / 60 casos, fechas 2026-09-14 y 2026-09-19 14:00Z:

- H1 respaldada: **26/73 = 35,62%**.
- Comparador: **23/126 = 18,25%**.
- Diferencia: **+17,36 puntos porcentuales**.
- Chequeo por símbolo: 26 con ambos grupos; 13 diferencias positivas, 6 negativas, 7 empates.
- Un retest simultáneo 1m fue ambiguo en el comparador, no se contó como favorable (misma convención que discovery).

**No se ha corrido holdout todavía.** Estos valores son solo discovery.

## Fases reservadas y métricas

- Holdout 1: corte PRE **2026-09-25 14:00 UTC**.
- Holdout 2: corte PRE **2026-09-30 14:00 UTC**.
- Exactamente los **mismos 30 símbolos**, misma lógica, mismos históricos/plan.
- Primaria: `original_30m_race_0_5 == FAVORABLE_FIRST`, `original_30m_coverage == COMPLETE`.
- Descriptivas secundarias: favorable primero bajo pérdida por cierre fuera de ±0,10%; ruptura LONG contraria confirmada dentro de 60m.
- Incertidumbre: bootstrap agrupado **por símbolo** (los dos cortes viajan juntos), 10.000 repeticiones, semilla 20261104. También se presenta diferencia de tasas por símbolo en los casos con ambos grupos.
- Criterio congelado: H1 tiene respaldo descriptivo si diferencia agrupada >0 y el límite inferior del intervalo bootstrap por símbolo 95% >0. Diferencia no positiva implica H1 no replicada; diferencia positiva pero intervalo que toca/corta cero es inconclusa.

**Precaución**: los mercados comparten tendencias, 30 símbolos no equivalen a 30 experimentos independientes. Un intervalo bootstrap por símbolo no modela toda la correlación temporal/macroeconómica. No es prueba de una estrategia operable. No modificar LIVE.

## Instalación por el flujo Git habitual

Agregar los tres archivos a la **raíz del repositorio**:

- `validate_reaction_h1_holdout_v1.py`
- `test_validate_reaction_h1_holdout_v1.py`
- `REACTION_H1_FROZEN_V1.json`

Confirmar un commit Git **antes de ejecutar el holdout**. No modificar los scripts originales auditados ni regenerar `cohort_plan.json`.

## Comandos en VPS — ejecutar en orden

```bash
cd /home/multiservices-1m
python3 -m unittest -v test_validate_reaction_h1_holdout_v1 test_reaction_multitf_cohort_v2

# Prueba documental: congelar y registrar hash del pre-registro.
sha256sum REACTION_H1_FROZEN_V1.json reports/reaction_multitf_cohort_v2/cohort_plan.json

# Ejecutar solo las fechas de holdout ya fijadas en el plan:
python3 reaction_multitf_cohort_v2.py run \
  --root /home/multiservices-1m \
  --plan reports/reaction_multitf_cohort_v2/cohort_plan.json \
  --stage holdout --allow-holdout

# Exigir 60/60 casos íntegros: no generar estadísticas finales si falta alguno.
python3 reaction_multitf_cohort_v2.py pool \
  --root /home/multiservices-1m \
  --plan reports/reaction_multitf_cohort_v2/cohort_plan.json \
  --stage holdout --allow-holdout --require-all \
  --out reports/reaction_multitf_cohort_v2/holdout_v2

# Aceptar únicamente el plan y el preregistro congelados.
python3 validate_reaction_h1_holdout_v1.py \
  --plan reports/reaction_multitf_cohort_v2/cohort_plan.json \
  --prereg REACTION_H1_FROZEN_V1.json \
  --coverage reports/reaction_multitf_cohort_v2/holdout_v2_coverage.csv \
  --events reports/reaction_multitf_cohort_v2/holdout_v2_htf_events.csv \
  --manifest reports/reaction_multitf_cohort_v2/holdout_v2_manifest.json \
  --out-prefix reports/reaction_multitf_cohort_v2/h1_holdout_v1
```

### Enviar al agente

Copiar el `REACTION_H1_HOLDOUT` de terminal y empaquetar los outputs + datos de verificación:

```bash
zip -j reports/reaction_multitf_cohort_v2/H1_VALIDACION_HOLDOUT.zip \
  reports/reaction_multitf_cohort_v2/holdout_v2_coverage.csv \
  reports/reaction_multitf_cohort_v2/holdout_v2_htf_events.csv \
  reports/reaction_multitf_cohort_v2/holdout_v2_htf_pair_summary.csv \
  reports/reaction_multitf_cohort_v2/holdout_v2_manifest.json \
  reports/reaction_multitf_cohort_v2/h1_holdout_v1.json \
  reports/reaction_multitf_cohort_v2/h1_holdout_v1_groups.csv \
  reports/reaction_multitf_cohort_v2/h1_holdout_v1_by_symbol.csv \
  reports/reaction_multitf_cohort_v2/h1_holdout_v1.md \
  REACTION_H1_FROZEN_V1.json
```

Si `run` termina parcialmente por cobertura, **no reemplazar símbolos, no recalcular fechas, no sobrescribir datos**. Enviar el error y revisar cobertura primero. Si un archivo ya existe, el generador se niega a sobrescribir y preserva el resultado.

Commit sugerido: `research(reaction): preregister and validate HTF short H1 holdout`
