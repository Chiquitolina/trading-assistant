# Estado, frescura y límites de la auditoría

1. **PM2 ONLINE ≠ datos sanos.** La captura del usuario muestra 9/9 procesos Reaction Lab ONLINE, pero `Micro Flow shards 0/6 OFFLINE`, `collector DEGRADED`, `transport gaps 2.869`. El inventario de PM2, tomado en otro instante, muestra los seis `micro-flow-ws-*` online. Ambas observaciones son compatibles con procesos vivos sin datos frescos; no hay diagnóstico de causa confirmado.
2. La auditoría incluye código bajo `SOURCE/project_0/`, un inventario de rutas/tamaños y procesos sanitizados. **No incluye** la base SQLite real ni Redis. No se inspeccionaron tablas y conteos reales dentro de esos ocho SQLite desde la VPS; el esquema de escritores sí se pudo revisar desde el código.
3. Los recursos de `/root/runner_*` tienen artefactos derivados históricos, pero `runner_sep_290_14d_v2` estaba incompleto al momento del inventario (sin resumen final). No certificar 290 symbols con este archivo.
4. El backup tiene integridad **por archivo**; no una transacción distribuida entre Replay/CSV/SQLite/Redis. No sustituye una copia final en momento de quietud para reproducibilidad absoluta.
5. El scanner V1 puede persistir REACTIONs `RECOVERED_WINDOW` que no tienen idéntica procedencia que `LIVE_OBSERVED`; un backup no corrige cobertura previa. Runners y Deep incluyen **outcomes posteriores**; nunca usarlos en tiempo de decisión en replay causal.
6. No reiniciar para respaldar. Una SQLite online se guarda con `sqlite3.Connection.backup`, evitando copiar un `.db` + WAL parcialmente desincronizados. Si SQLite está bloqueado, el respaldo informa error y no debe tratarse como verificado.
7. Tras extraer, controlar: presencia de los ocho SQLite principales; monitores; `replay_events.csv`; cache histórica; `manifest.json` / `summary.json`; `shadow_paths_1m.sqlite3` y `policy_events.csv`. Verificar relación de hashes entre etapas usando sus manifests propios, además del manifiesto del backup.

### Diagnósticos de lectura en VPS

```bash
pm2 list
pm2 logs market-data-test --lines 40 --nostream
pm2 logs micro-flow --lines 40 --nostream
pm2 logs micro-flow-ws-0 --lines 40 --nostream
python3 reaction_lab_continuous_scanner.py --db reports/reaction_lab/scanner_events_v1.sqlite3 --status
python3 reaction_lab_auto_paths.py --db reports/reaction_lab/auto_paths_v1.sqlite3 --status
```

Consultar un SQLite en modo read-only (sin escribir en la original):

```bash
python3 - <<'PY'
from pathlib import Path
import sqlite3
p=Path('/home/multiservices-1m/reports/reaction_lab/scanner_events_v1.sqlite3')
with sqlite3.connect(p.resolve().as_uri()+'?mode=ro',uri=True) as con:
    print([r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")])
PY
```

No ejecutar un `VACUUM` ni limpiezas de caché mientras los workers escriben.
