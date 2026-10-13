# Continuous V1 — archivo histórico permanente de runners >1%

## Objetivo

Conservar, sin depender de los lotes/ventanas del selector Streamlit, **cada REACTION Base de Continuous V1 cuyo recorrido FAVORABLE posterior al retest haya SUPERADO el 1%**. No requiere elegibilidad Candidate V1/V2. Los Event IDs que no alcancen el umbral siguen en el scanner normal; no se eliminan.

El proceso nuevo lee las SQLite **scanner_events_v1.sqlite3**, **auto_paths_v1.sqlite3** y **causal_context_v1.sqlite3**, y escribe **únicamente** `reports/reaction_lab/runners_v1.sqlite3`. Es un catálogo permanente por Event ID. Los tres colectores PM2 existentes no se tocan.

## Definición del run (idéntica al selector actual)

- Base: `retest_close` de la vela 1m donde ocurrió la REACTION.
- Solo barras 1m **cerradas, consecutivas**, a partir del minuto siguiente al retest.
- LONG: `(máximo high posterior / retest_close - 1) * 100`.
- SHORT: `(1 - mínimo low posterior / retest_close) * 100`.
- Máximo de 360 minutos por evento. Corta ante huecos. **Umbral estricto `>1%`**, no `>=1%`.
- Se registra el *primer minuto donde cruzó* 1%, el pico, instante del pico, excursión adversa, minutos observados, estado del path y la copia comprimida de las velas archivadas.
- Se guarda `scanner_causal_json` íntegro (swing/pivote/retest/volumen/etc.), contexto y velas as-of (`context_json`,`candles_json`), régimen V1 (`v1_regime_json`), followups y estados donde existan. Si faltan inicialmente, intenta incorporarlos cuando los otros servicios los produzcan. El archivo conserva explícitamente nulos en vez de inventar datos.
- Un runner **nunca se borra**, y su run máximo puede crecer hasta que el path deje de evolucionar.
- Es una etiqueta retrospectiva; no debe usarse como requisito de una entrada causal ni como resultado de una operación real.

## Archivos entregados

1. `reaction_lab_runners.py` (**nuevo**): worker del catálogo permanente.
2. `reaction_lab_runners_pm2.config.cjs` (**nuevo**): cuarto proceso PM2 independiente.
3. `reaction_lab_unified_adapter.py` (**modificado**): lector del archivo, sin escaneo de Redis.
4. `reaction_lab_research_view.py` (**modificado**): checkbox `Solo runners históricos persistidos (run > 1%)`, filtro ajustable de run mínimo, orden descendente y el MISMO inspector compartido de Legacy. No corta la tabla de runners al lote más reciente.
5. `test_reaction_lab_runners.py` (**nuevo**): tests para LONG/SHORT, cruce, MFE, no pérdida, metadata, corte de cohorte y duplicados.

**Sin cambios** a `dashboard/app.py`, `reaction_lab_scanner`, `reaction_lab_auto_paths`, `reaction_lab_context` ni a las estrategias, matrices o portfolios históricos.

## Paso 1 — Git desde Windows

Copiar los cinco archivos en la raíz del repositorio. El `.cjs` es archivo nuevo.

```powershell
python -m py_compile reaction_lab_runners.py reaction_lab_unified_adapter.py reaction_lab_research_view.py
# Los tests de SQLite/flock están pensados para correr en Linux.
git add reaction_lab_runners.py reaction_lab_runners_pm2.config.cjs reaction_lab_unified_adapter.py reaction_lab_research_view.py test_reaction_lab_runners.py
git diff --cached --check
git diff --cached --stat
git commit -m "feat(reaction-lab): permanently archive continuous runners above one percent"
git push origin microservices
```

## Paso 2 — Prueba en VPS, sin tocar bases existentes

```bash
cd /home/multiservices-1m
git pull --ff-only origin microservices
python3 -m unittest -v test_reaction_lab_runners.py test_reaction_lab_run_sort.py test_reaction_lab_unified_adapter.py test_reaction_lab_continuous_inspector.py
python3 reaction_lab_runners.py --check
python3 reaction_lab_runners.py --db /home/multiservices-1m/tmp/runner_archive_pilot.sqlite3 --once
python3 reaction_lab_runners.py --db /home/multiservices-1m/tmp/runner_archive_pilot.sqlite3 --status
```

El piloto hace backfill de **todos los paths 1m archivados** que tengan scanner Event ID perteneciente a Continuous V1. Si un evento anterior nunca tuvo path, no se le inventará el 1%.

### Auditar una muestra

```bash
python3 - <<'PY'
import sqlite3
p='/home/multiservices-1m/tmp/runner_archive_pilot.sqlite3'
with sqlite3.connect('file:'+p+'?mode=ro',uri=True) as c:
    print('Runners >1%:', c.execute('SELECT COUNT(*) FROM runners').fetchone()[0])
    print('Runners >=2%:',c.execute('SELECT COUNT(*) FROM runners WHERE peak_run_pct>=2').fetchone()[0])
    for row in c.execute('SELECT event_key,ROUND(peak_run_pct,3),observed_min,path_state,context_state,v1_regime_status,length(path_blob) FROM runners ORDER BY peak_run_pct DESC LIMIT 15'):
        print(row)
PY
```

Verificar que `path_errors=0`. Si hay errores, no continuar sin investigar.

## Paso 3 — Activación controlada

Después de validar el piloto, ejecutar una vez sobre el archivo definitivo y comprobar:

```bash
python3 reaction_lab_runners.py --once
python3 reaction_lab_runners.py --status
```

Luego levantar el worker:

```bash
pm2 start reaction_lab_runners_pm2.config.cjs --only reaction-lab-runners
pm2 show reaction-lab-runners
pm2 logs reaction-lab-runners --lines 40 --nostream
```

Cuando esté estable, reiniciar **solo** la interfaz Streamlit:

```bash
pm2 restart dash-micro1m
```

En `Continuous V1 -> REACTION / Retests` seleccionar `Solo runners históricos persistidos (run > 1%)` (encendido por defecto). Verás los Event IDs archivados aunque sean antiguos y ya no estén en el lote de análisis reciente. Podés subir el umbral visual a `2%`, `3%`, etc. sin perder registros. Legacy conserva el mismo inspector.

**No ejecutes `pm2 save` sin revisar la lista completa de procesos detenidos**, como venimos haciendo.

## Límites explícitos

- Solo define **Continuous V1** (scanner 15m 3x3), no todos los pivotes del dashboard ni las REACTIONs históricas Legacy previas al corte.
- Si el collector ya había perdido las velas 1m de un evento antes de comenzar, no se puede demostrar que superó el 1%: queda sin etiquetar hasta que haya datos reales.
- Si el path tuvo huecos, se usa únicamente el prefijo consecutivo.
- Clasificar resultados FUTUROS por MFE introduce sesgo de selección; no usar este filtro para estimar el win rate o construir entradas sin controles causales.
