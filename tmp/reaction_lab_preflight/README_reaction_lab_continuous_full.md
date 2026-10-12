# Reaction Lab · Continuous REACTION scanner + automatic 1m paths

## Alcance verificable

- Nuevo **proceso independiente de Streamlit**: vuelve a ejecutar, sobre Redis (velas cerradas), la detección estructural canónica de `dashboard/app.py` para **15m 3x3, prominencia 0, departure >=0,20%, tolerancia 0,10%, age 360m**. Conserva LONG y SHORT y su ID `SYMBOL|SIDE|retest_timestamp`.
- Extrae el AST de las funciones originales de `app.py` **sin importar ni ejecutar su código Streamlit**. Omite únicamente métricas de seguimiento posterior al retest (MFE, orden de recuperación, etc.) que no influyen en la identidad de la REACTION. No modifica ni invoca trading ni órdenes.
- Registra cada REACTION **una sola vez**, con el contexto causal disponible en la fila (sin outcomes), procedencia y fecha de primera observación, en `reports/reaction_lab/scanner_events_v1.sqlite3`. Conserva `RECOVERED_WINDOW` como distinto de `LIVE_OBSERVED`.
- La segunda tarea, `reaction_lab_auto_paths.py`, consume el registro del scanner **además** de V1 RAW/V1 SHORT/V1 LONG/V2 persistidos. Deduplica Event IDs, recupera OHLC 1m cerrado, guarda **360 minutos contiguos** en `reports/reaction_lab/auto_paths_v1.sqlite3`. No inventa velas ni salta gaps.
- `reaction_lab_scan_bridge.py`: permite leer los eventos persistidos y sumar en memoria los paths SQLite al almacén existente del dashboard. Solo fusiona al motor de Candidates eventos muy recientes (**últimos 300 minutos**) y únicamente si la selección es canónica (15m/3x3/0.10/0.20/360); el resto queda preservado en el journal. Así no se recalcula contexto histórico antiguo desde Redis actual.
- Panel *Continuous REACTION scanner · last 100 persisted* en la vista Fast de Reaction Lab, incluso si la matriz Fast está desactualizada.

## Cobertura y alcance precisos

- **No elimina estrategias, monitores o pickles anteriores.** Solo `install_reaction_lab_bridge.py --apply` modifica `dashboard/app.py` y previamente hace backup.
- El scanner continuo detecta el **universo estructural REACTION 15m / 3x3 / 0.20% / 0.10%** para los símbolos listados en `volume_exhaustion_events.csv` **al iniciar**; no equivale automáticamente a los 290 del replay ni a todos los pares de Binance. Las membresías V1 SHORT/LONG y variantes V2 siguen clasificándose por sus reglas originales.
- Los símbolos nuevos que aparezcan después de iniciar el proceso requieren reiniciarlo en esta versión (la lista se captura una vez en startup).
- Medir `symbols_requested`, `redis_1m_ok`, `redis_15m_ok` y latencia antes de afirmar cobertura completa. En ejecución real los historiales pueden presentar huecos y el primer lote incluye observaciones retrospectivas `RECOVERED_WINDOW`.
- La matriz Candidate no se recalcula sola; el bridge hace los paths nuevos consultables al recalcularla y exhibe un feed persistido.
- **Corrección del instalador:** la vista `_candidate_v1_matched_control_path_store` recibe `reaction_rows`, no `candidate_rows`. La prueba de integración comprueba ahora las tres firmas y detecta este error en futuros cambios.

## Limitaciones transparentes

1. **El scanner debe estar operativo** y Redis debe tener las velas al cerrar: interrupciones largas no se reconstruyen mágicamente; la retención de 1m sigue siendo de 400 velas. Inspeccionar cobertura, retardos, gaps y estados es obligatorio.
2. **V1 filtered y V2 variants** tienen clasificación/Strength/Flow adicional. El servicio registra el *universo común de REACTIONs* y sus paths, no certifica automáticamente el contexto histórico de Strength ni la pertenencia V1 filtrada de señales antiguas. Las señales anteriores a 300m siguen en SQLite, pero **no se incorporan silenciosamente como si tuvieran contexto histórico válido** a matrices viejas.
3. Las matrices y snapshots de Fast Candidate continúan siendo materializaciones específicas; el servicio **no simula operaciones ni recalcula automáticamente la equity**. El panel Fast permite ver que hay eventos nuevos, y el motor normal puede usar los paths recuperados sin pulsar el botón de guardado de paths. Actualizar el portfolio de un snapshot viejo es un paso aparte.
4. 290 símbolos con varios timeframes pueden tardar bastante. Si el scanner tarda más que el intervalo de polling o su cobertura 1m/15m baja de 95%, verás **WARNINGS**: no afirmar cobertura completa hasta auditar la operación real.
5. La concordancia live vs replay para nuevas señales **todavía debe medirse** en un período prospectivo. No reutilizar el 100% de Event IDs del replay de octubre como prueba de una cobertura continua aún no observada.

## Instalación — solo subir un ZIP, NO reemplaza `app.py` hasta `--apply`

```bash
cd /home/multiservices-1m
unzip -o reaction_lab_continuous_full.zip
python3 -m py_compile \
  reaction_lab_continuous_scanner.py reaction_lab_auto_paths.py \
  reaction_lab_scan_bridge.py install_reaction_lab_bridge.py
python3 -m unittest -v test_reaction_lab_continuous.py test_reaction_lab_auto_paths.py
python3 reaction_lab_continuous_scanner.py --project /home/multiservices-1m --check
python3 reaction_lab_auto_paths.py --project /home/multiservices-1m --check
python3 install_reaction_lab_bridge.py --project /home/multiservices-1m --check
```

El `--check` del scanner y el del bridge no modifican archivos. Validar el resultado antes de continuar.

## Piloto real, sin tocar monitores anteriores

Crear una lista de 1 símbolo; por ejemplo `AWEUSDT`:

```bash
printf 'AWEUSDT\n' > /tmp/reaction_lab_pilot.txt
python3 reaction_lab_continuous_scanner.py \
  --project /home/multiservices-1m \
  --symbols-file /tmp/reaction_lab_pilot.txt --dry-scan
```

`--dry-scan` **consulta Redis** pero NO crea journal. Si confirma lectura de 1m/15m y muestra eventos plausibles, ejecutar una sola escritura aislada y luego el colector:

```bash
python3 -u reaction_lab_continuous_scanner.py \
  --project /home/multiservices-1m \
  --symbols-file /tmp/reaction_lab_pilot.txt --scan-once
python3 -u reaction_lab_auto_paths.py \
  --project /home/multiservices-1m --run-once
python3 reaction_lab_continuous_scanner.py --status
python3 reaction_lab_auto_paths.py --status
```

La primera carga puede encontrar muchos eventos antiguos y clasificarlos como `STALE_BEFORE_COLLECTOR`/`MISSING_ENTRY`. Eso es evidencia explícita, no recuperación sintética. No usar el primer lote de arranque como muestra prospectiva live.

## Integración segura de Streamlit

```bash
python3 install_reaction_lab_bridge.py --project /home/multiservices-1m --check
# SOLO si el diff se corresponde con la versión instalada:
python3 install_reaction_lab_bridge.py --project /home/multiservices-1m --apply
python3 -m py_compile dashboard/app.py
```

El instalador **rechaza firmas que no coinciden**, no aplica parcialmente, hace backup del `app.py` original (normalmente `/root/app.py.before_continuous_reactions_...`) y respeta CRLF. No borra ni sobreescribe `paths_1m.pkl`, monitores, benchmarks, replays o reportes congelados.

## Producción PM2 — activar solo después de comprobar el piloto

Confirmar que no existe ya un scanner/collector duplicado:

```bash
pm2 list
pgrep -af 'reaction_lab_continuous_scanner.py|reaction_lab_auto_paths.py'
```

Luego iniciar dos servicios independientes:

```bash
cd /home/multiservices-1m
pm2 start /usr/bin/python3 --name reaction-lab-scanner -- \
  -u /home/multiservices-1m/reaction_lab_continuous_scanner.py \
  --project /home/multiservices-1m --loop --poll-seconds 60
pm2 start /usr/bin/python3 --name reaction-lab-auto-paths -- \
  -u /home/multiservices-1m/reaction_lab_auto_paths.py \
  --project /home/multiservices-1m --loop --poll-seconds 60
pm2 logs reaction-lab-scanner --lines 40
pm2 logs reaction-lab-auto-paths --lines 40
```

Esperar varias velas y auditar `--status`, latencia, `redis_1m_ok/redis_15m_ok`, cambios en `new_events`, `saved_bars`, `ACTIVE`, `COMPLETE`, `MISSING_ENTRY` y `GAP_EXPIRED`. Si la duración del scanner supera 60s habitualmente, no declararlo cubierto; revisar carga y sharding antes de confiar en live.

**No ejecutar `pm2 save` sin revisar todos los demás procesos administrados por PM2.**

## Recuperación y reversión

```bash
pm2 stop reaction-lab-scanner reaction-lab-auto-paths
# Para revertir solo la integración de dashboard:
# cp -a /root/app.py.before_continuous_reactions_<TIMESTAMP> \
#   /home/multiservices-1m/dashboard/app.py
```

Las bases SQLite independientes quedan guardadas como evidencia; no borrarlas por reflejo. Para un nuevo despliegue revisar `git status --short`, evitar versionar `*.sqlite3`, `*-wal`, `*-shm`, `*.lock`, backups, `.env` y reportes de producción.

## Commit sugerido

`feat(reaction-lab): persist continuous causal reactions and 1m paths`
