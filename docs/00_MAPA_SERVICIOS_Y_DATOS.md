# Reaction Lab — auditoría de servicios, persistencia y réplica local

**Estado:** mapa inicial basado en captura de conectividad, código de `dashboard/app.py` compartido y el catálogo de datos anterior. **No es todavía un runbook de arranque verificado en VPS.** Ese runbook requiere el ZIP de auditoría generado directamente desde la VPS actual.

## 1. Servicios que aparecen en la pantalla

Los nombres PM2 de los nueve procesos de Reaction Lab están definidos en la versión examinada de `dashboard/app.py`. **El indicador verde indica PM2 `online`; no confirma frescura de datos, escritura en disco ni consistencia del mercado.**

| Indicador visual | Nombre PM2 conocido | Responsabilidad a auditar | Arranque exacto |
|---|---|---|---|
| Scanner | `reaction-lab-scanner` | Captura/persistencia de REACTION | PENDIENTE: `pm_exec_path`, `args`, `cwd` |
| Auto paths | `reaction-lab-auto-paths` | Seguimiento de trayectorias de candidatos | PENDIENTE |
| Context | `reaction-lab-context` | Factores y contexto de mercado | PENDIENTE |
| Runners | `reaction-lab-runners` | Evaluación/simulación o seguimiento de estrategias | PENDIENTE |
| BTC context | `reaction-lab-btc-context` | Contexto específico BTC | PENDIENTE |
| Deep research | `reaction-lab-deep-research` | Investigación enriquecida | PENDIENTE |
| Liquidations | `reaction-lab-liquidations` | Información de liquidaciones | PENDIENTE |
| Market telemetry | `reaction-lab-market-telemetry` | Métricas de mercado | PENDIENTE |
| Telemetry link | `reaction-lab-telemetry-link` | Enlace/transformación de telemetría | PENDIENTE |

En la misma pantalla aparecen **ENGINE, MARKET DATA, Core Kline WS, Redis heartbeat y Strategy feed** online. También hay un problema a tratar separadamente: **Micro Flow shards 0/6 OFFLINE, collector DEGRADED**, con `transport gaps` y shards abiertos. No considerar Micro Flow sano solo porque el resto esté verde. Para esos indicadores todavía falta confirmar si hay procesos PM2 independientes, módulos del Market Data producer o señales Redis, y sus respectivos nombres de arranque. 

## 2. Tipos de persistencia que debemos respaldar

| Clase | Fuente de ejemplo / ruta conocida | Por qué copiarla |
|---|---|---|
| REACTION continua | `/home/multiservices-1m/reports/reaction_lab/scanner_events_v1.sqlite3` | Registro durable del scanner |
| Candidate V1/V2 | `/home/multiservices-1m/candidate_v1*_monitor.csv`, `candidate_v2_monitor.csv`, `reports/candidate_v1_analysis`, `reports/candidate_v2_analysis` | Cohortes, monitores y trayectorias |
| Replay histórico | `/root/runner_sep_14d20_merged/`, `/root/runner_sep_290_14d_v2/` | Eventos y estados de corridas |
| Contexto as-of | `/root/runner_sep_week20_context/`, `/root/runner_sep_holdout20_context/` | Factores, snapshots 4h, trazabilidad |
| Caché de mercado | `/root/runner_phase3_parity_cache/` | OHLCV necesario para reproducir ejecuciones |
| Portfolio | `reports/reaction_lab/runner_portfolio_v1/` | Órdenes, trades y curvas |
| Competition / Shadow | `/root/runner_competition_14d20_phase1/`, `...shadow_v2/` | Competencia, outcomes y SQLite de trayectorias |
| Selection | `/root/runner_competition_14d20_selection_v1/` | Factors snapshots y decisiones por política |
| Policy Replay | `/root/runner_policy_14d20_20sym_v1/` | Cartera completa por criterio de selección |
| Stream / Redis | Productor Market Data, Redis stream/canales/estado | NO se preserva con un backup de archivos; requiere procedimiento específico si se necesita replay exacto de un estado en memoria |
| Configuración de ejecución | Código de `engine`, `services`, `dashboard`, scripts, PM2 command lines y dependencias | Necesario para abrir el software, no solo leer CSV |

**No confundir `replay_events.csv` con la base SQLite del scanner en producción.** Son universos y cadenas de captura diferentes.

## 3. Dos objetivos distintos en Windows

**A. Espejo de datos y análisis offline (primera meta).** Descargar todos los CSV, SQLite, cachés, manifiestos, código y documentación. Montarlos en Windows/WSL2 y ejecutar los visores históricos en modo lectura, con rutas de datos remapeadas. **No enviar órdenes ni conectarse a Binance en esta modalidad.** Para que las páginas se vean como en VPS hay que verificar qué páginas dependen de Redis/servicios vivos y crear, si hace falta, un adaptador offline; copiar archivos no garantiza que el Streamlit actual funcione idéntico.

**B. Clonar el sistema LIVE (otra meta, opcional).** Requiere instalar e iniciar Redis, Market Data WS, PM2 y demás procesos, administrar claves de entorno de forma segura y aislar por completo ejecución de órdenes. No es una simple restauración de datasets y no debe arrancarse por accidente en el PC.

## 4. Fases para llegar a una réplica reproducible

1. **Auditoría de código y procesos:** ejecutar el script del paquete. Conservar `pm2_services_sanitized.json`, árbol fuente y `persistence_inventory.csv`.
2. **Mapa de propiedad de datos:** cada servicio con ruta ejecutable, argumentos, dependencias, inputs Redis/archivos, outputs, criterios de frescura y procedimiento seguro de arranque/parada. No inventar comandos; tomarlos de PM2 y el código auditado.
3. **Backup de los datos:** usar el `research_backup_vps.py` ya entregado, inspeccionando su `list` y agregando directorios extra que revele la auditoría. Hacer otra copia tras completar el replay de 290.
4. **Manifiesto de réplica:** guardar versión Git por repo, versiones de Python/Node/Redis, dependencias Python, rutas de origen, SHA256, tamaño, snapshot SQLite y estado de run. La auditoría NO copia dependencias ni secretos: solo aporta identificación.
5. **Visor offline en PC:** instalar dependencias, extraer respaldo conservando jerarquía (`vps/home/...`, `vps/root/...`), montar rutas vía WSL2 o configurar data root, deshabilitar escrituras/live y validar pantallas por dataset.
6. **Validación:** abrir REACTION V1/V2, replay 20 y 290, contexto, portfolio, competition, shadow, selection y policy. Comprobar registros y hashes; comparar capturas/contadores con VPS.

## 5. Cómo se conecta con los manuales existentes

- `docs/DATA_CATALOG_REACTION_RESEARCH.md`: claves y artefactos por fase.
- `docs/BACKUP_COMPLETO_REPLAY_REACTIONS_PC.md`: transferencia por SSH con manifiesto y SHA256, sin ZIP gigante en servidor.
- `docs/PLAYBOOK_REPLAY_CONTEXT_PORTFOLIO_COMPETITION.md`: ejecución de Replay → contexto → simulador → research.
- `docs/CANDIDATE_SELECTION_LAB.md`: factores/selección; `docs/PORTFOLIO_POLICY_REPLAY.md`: carteras experimentales; `docs/PORTFOLIO_POLICY_DASHBOARD.md`: visualización.

Los nombres anteriores refieren a documentos entregados en el proyecto; verificar qué versiones efectivamente están copiadas a la VPS actual.

## 6. Información que falta y será resuelta con la auditoría

- Comandos PM2 **exactos** de todos los procesos (incluidos dashboards), `cwd`, intérprete, puertos, definición de arranque tras reinicio (`pm2 startup` / systemd / scripts de arranque).
- Dependencias entre servicios: productor Market Data/Redis, shards Micro Flow, scanner, paths, contextos y runners.
- Todas las persistencias fuera de `reports/` y de los directorios `/root/runner_*`; cuáles son volátiles y cuáles canónicas.
- Qué datasets y widgets de Streamlit se pueden abrir en Windows sin Redis y cuáles necesitan adaptador offline.
- Criterios de recuperación ante apagado y un checklist de restauración que no arranque trading LIVE accidentalmente.
