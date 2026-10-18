# Mapa verificado de servicios — /home/multiservices-1m

**Fuente:** `reaction_project_audit_safe.zip` recibido 18/10/2026; snapshot de `pm2 jlist` sanitizado y código Python del mismo paquete. Estado ONLINE es un dato **puntual**, no prueba frescura, cobertura ni salud del servicio.

## Los 9 procesos REACTION (todos marcados online al auditar)

| Nombre PM2 | Ejecución (`/home/multiservices-1m`) | Lee principalmente | Persiste principalmente |
|---|---|---|---|
| `reaction-lab-scanner` | `python3 -u reaction_lab_continuous_scanner.py --project /home/multiservices-1m --loop --poll-seconds 60` | Redis 1m/15m + universo fuente; no reconstruye arbitrariamente un mercado anterior | `reports/reaction_lab/scanner_events_v1.sqlite3` |
| `reaction-lab-auto-paths` | `python3 -u reaction_lab_auto_paths.py --project /home/multiservices-1m --loop --poll-seconds 60` | Scanner DB, monitores V1/V2, velas cerradas Redis | `reports/reaction_lab/auto_paths_v1.sqlite3` |
| `reaction-lab-context` | `python3 -u reaction_lab_context.py --project /home/multiservices-1m --loop --poll-seconds 60 --limit 50` | Scanner, paths, contexto histórico disponible | `reports/reaction_lab/causal_context_v1.sqlite3` |
| `reaction-lab-runners` | `python3 -u reaction_lab_runners.py --project /home/multiservices-1m --loop --poll-seconds 60` | Scanner + paths + causal context | `reports/reaction_lab/runners_v1.sqlite3` |
| `reaction-lab-btc-context` | `python3 -u reaction_lab_btc_context.py --project /home/multiservices-1m --loop --poll-seconds 60 --limit 50` | Scanner, contexto BTC disponible | `reports/reaction_lab/btc_context_v1.sqlite3` |
| `reaction-lab-deep-research` | `python3 -u reaction_lab_deep_collector.py --project /home/multiservices-1m --loop --poll-seconds 60 --limit 100` | Scanner + context + paths + telemetry | `reports/reaction_lab/deep_research_v1.sqlite3` |
| `reaction-lab-liquidations` | `python3 -u reaction_lab_liquidations.py --project /home/multiservices-1m --loop` | Datos externos de liquidaciones | `reports/reaction_lab/telemetry_v1.sqlite3` |
| `reaction-lab-market-telemetry` | `python3 -u reaction_lab_market_telemetry.py --project /home/multiservices-1m --loop --poll-seconds 45 --max-symbols 20 --priority-enabled --priority-scan-symbols 60 --max-priority 10` | Fuentes de mercado y watches de prioridad | `reports/reaction_lab/telemetry_v1.sqlite3` |
| `reaction-lab-telemetry-link` | `python3 -u reaction_lab_telemetry_link.py --project /home/multiservices-1m --loop --poll-seconds 60 --limit 100` | Scanner + telemetry | `reports/reaction_lab/telemetry_link_v1.sqlite3` |

**Dependencias generales (simplificadas):** `Market Data/Redis → Scanner → Auto Paths → Context → Runners/Deep`; en paralelo `Market Telemetry + Liquidations → Telemetry Link → Deep`. BTC Context consume scanner y contexto BTC. Los timings de origen pueden diferir; esta relación **no** implica simultaneidad entre todas las bases.

## Servicios de infraestructura del mismo proyecto

| Nombre PM2 | Ejecución verificada | Estado auditoría | Precaución |
|---|---|---|---|
| `market-data-test` | `python3 -u -m engine.live.data.market_data_service` | online | WebSockets y Redis; datos LIVE |
| `multiservices-1m` | `python3 -u -m engine.live.main --strategy compression` | online | **Motor de trading: no iniciar en PC local** |
| `micro-flow` | `python3 -u -m engine.live.research.micro_flow_collector` | online | Dashboard anterior indicó DEGRADED |
| `micro-flow-ws-0` a `micro-flow-ws-5` | `python3 -u -m engine.live.data.micro_flow_shard_service --shard-id N --shard-count 6 --chunk-size 60 --stale-after 45` | online en PM2 | La captura anterior decía `0/6` saludables: **investigar el dato**, no asumir cobertura |
| `dash-micro1m` | `python3 -m streamlit run dashboard/app.py --server.port 8501` | online | Dashboard del proyecto auditado |
| `dash-multiservices` | `python3 -m streamlit run dashboard/app.py --server.port 8503` | online | Corre en `/home/multiservices`, **otro árbol no incluido** en esta auditoría |

Hay **26 entradas PM2** en el snapshot, incluidas aplicaciones fuera de este proyecto y procesos detenidos. Para evitar confusiones, `reaction-lab-*` son nueve procesos, no nueve componentes independientes de Market Data.

## Operación y verificaciones (SOLO VPS)

```bash
cd /home/multiservices-1m
pm2 list
pm2 show reaction-lab-scanner
pm2 logs reaction-lab-scanner --lines 50 --nostream
pm2 show reaction-lab-auto-paths
pm2 show market-data-test
pm2 show micro-flow-ws-0
```

Para **reanudar un proceso que efectivamente figura detenido**, y solo después de verificar que no haya una segunda instancia ni un problema de datos:

```bash
pm2 restart reaction-lab-scanner
# Equivalente, cambiando el nombre, para cada worker ya registrado.
```

Para el resto, **no publicar comandos nuevos de `pm2 start` sin resolver antes la configuración de ejecución y las dependencias**. `pm2 restart multiservices-1m` reiniciaría trading LIVE: evitarlo en cualquier procedimiento de investigación/backups. No reiniciar un servicio solo porque el dashboard diga OFFLINE: contrastar PM2, heartbeats, estado de shards y cobertura de datos.

## Versiones de herramientas detectadas

Python 3.10.12 · Node v18.20.8 · PM2 5.4.3 · Streamlit 1.55.0 · Redis CLI 6.0.16.

## No incluido en este ZIP de auditoría

Archivos `.env` o variables de entorno, unidad systemd de Redis, datos residentes de Redis, permisos Nginx, configuración SSL/DNS, contenido del proyecto `/home/multiservices` y scheduler externo. En consecuencia **no puede prometerse levantar todo el sistema LIVE en Windows** usando solamente el archivo de código.
