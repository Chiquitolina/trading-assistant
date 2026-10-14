# Continuous V1 — Telemetry Priority + Event Link V2 (research only)

## Qué cambia

* `reaction_lab_structural_priority.py` **nuevo**. Explora cerradas 15m/1m de Redis, heurística independiente: pivot estricto confirmado 3x3, departure ≥0,20 %, sin primer retest observado dentro del 0,10 %. **Solo ordena consultas REST, NO crea ni filtra REACTIONs canónicas.** Rechaza ventanas 1m con huecos. Exploración incremental limitada a 60 símbolos por ciclo, sin WebSockets adicionales ni históricos REST por símbolo para hacer prioridad.
* `reaction_lab_market_telemetry.py` **reemplazar**. Respeta cursor rotativo y reserva al menos la mitad del lote para cobertura normal; lote limitado a 25 símbolos por ciclo (20 predeterminado), hasta 100 solicitudes REST si se configura 25, 80 solicitudes con 20. Detecta errores HTTP 418/429 y aplica cooldown ≥10 minutos. Registra disponibilidad real de cada endpoint y timestamp por fuente. Prioridad optativa; si falla Redis hace fallback a rotación.
* `reaction_lab_market_telemetry_pm2.config.cjs` **reemplazar**. Nuevo perfil **todavía NO activado**, 20 símbolos por ciclo, descanso 45s, 10 plazas prioritarias y escaneo Redis de 60 símbolos por ciclo. La frecuencia real incluye la duración REST + lectura Redis + descanso. **20/290 NO significa cobertura de los 290 cada minuto**; sin prioridad una ronda de 290 demoraría decenas de minutos. Requiere medir la frescura real en VPS.
* `reaction_lab_telemetry_link.py` y su `reaction_lab_telemetry_link_pm2.config.cjs` **nuevos**. Sidecar SQLite `telemetry_link_v1.sqlite3`: un registro INMUTABLE por `event_key` posterior a su propio corte. Lee scanner y telemetría sin escribirles. Valida `observed_ms` y `received_ms` individuales (OI ≤15m; funding ≤60m; order book ≤90s; taker ≤180s). Calcula OI 5m/15m solo desde snapshots realmente anteriores. Cuenta liquidaciones observadas 15m y permite valor **cero** solo con evidencia de cobertura 15m de heartbeats (distancia entre ellos ≤90s); no se confunde ausencia de stream con cero liquidaciones. Conserva evidencia por evento y digest de integridad. Espera 120s tras known_ts para tolerar latencia de persistencia, **sin admitir observaciones posteriores**.
* `reaction_lab_deep_collector.py` **reemplazar**: cambia solamente `load_deep_frame()`, que incorpora lectura del sidecar causal al ser invocado por el dashboard/Analyzer. No modifica ni reescribe las filas congeladas de `deep_research_v1.sqlite3`. Si la base lateral no existe, funciona como antes. Al desplegar, la instancia ya activa del recolector seguirá con su código cargado; **no requiere reiniciarla**. Reiniciar solamente `dash-micro1m` tras las pruebas si se quieren ver las columnas nuevas.
* `test_reaction_lab_telemetry_priority.py` **nuevo**. Tests con red y Redis simulados: sin lookahead, gaps, preferencia, rotación, timestamps, liquidaciones, inmutabilidad, merge y cooldown.

## Instalación sin activar nuevos servicios

En PC: copiar los archivos de arriba a la raíz de la rama `microservices`, `git add`, `git commit`, `git push`.

En VPS:

```bash
cd /home/multiservices-1m
git pull --ff-only origin microservices
python3 -m unittest -v test_reaction_lab_telemetry_priority.py
python3 -m py_compile reaction_lab_market_telemetry.py reaction_lab_structural_priority.py reaction_lab_telemetry_link.py reaction_lab_deep_collector.py
python3 reaction_lab_market_telemetry.py --project /home/multiservices-1m --check --priority-enabled --max-symbols 20
python3 reaction_lab_telemetry_link.py --project /home/multiservices-1m --check
```

## Piloto aislado con REST real y Redis de solo lectura

La prueba utiliza exactamente **3 símbolos** y a lo sumo 12 llamadas REST más una inspección Redis de 10 símbolos. No toca `telemetry_v1.sqlite3` ni procesos PM2 existentes:

```bash
cd /home/multiservices-1m
mkdir -p tmp
python3 -u reaction_lab_market_telemetry.py \
  --project /home/multiservices-1m \
  --db /home/multiservices-1m/tmp/telemetry_priority_pilot_v2.sqlite3 \
  --once --max-symbols 3 --priority-enabled --priority-scan-symbols 10 --max-priority 1
```

Comprobar en resultado `priority_scan` (error debe ser `null`), `symbols_attempted`, `snapshots`, `rate_limited`. Que `watches=0` en esa muestra **no es un fallo**: quizá los símbolos escaneados no tienen estructuras en vigilancia. Si `priority_scan.error` no es `null`, dejar PM2 apagado y revisar configuración Redis antes de avanzar.

## Piloto aislado del enlace causal

```bash
python3 reaction_lab_telemetry_link.py \
  --project /home/multiservices-1m \
  --db /home/multiservices-1m/tmp/telemetry_link_pilot_v2.sqlite3 \
  --once --limit 20
python3 reaction_lab_telemetry_link.py \
  --project /home/multiservices-1m \
  --db /home/multiservices-1m/tmp/telemetry_link_pilot_v2.sqlite3 \
  --status
python3 reaction_lab_telemetry_link.py \
  --project /home/multiservices-1m \
  --db /home/multiservices-1m/tmp/telemetry_link_pilot_v2.sqlite3 \
  --audit --limit 20
```

Primer `inserted=0` es esperable (inicio prospectivo y `settle-seconds=120`). Esperar nuevas REACTIONs. La auditoría `--audit` verifica inmutabilidad/digest/timestamps guardados, **no demuestra paridad numérica de todos los proveedores externos**.

## Solo después de validar pilotos: PM2 nuevos

```bash
pm2 start reaction_lab_market_telemetry_pm2.config.cjs --only reaction-lab-market-telemetry
pm2 start reaction_lab_telemetry_link_pm2.config.cjs --only reaction-lab-telemetry-link
pm2 logs reaction-lab-market-telemetry --lines 25 --nostream
pm2 logs reaction-lab-telemetry-link --lines 25 --nostream
python3 reaction_lab_telemetry_link.py --status
```

No iniciar estos nuevos PM2 hasta validar ambos pilotos. No reiniciar scanner, auto-paths, context, runners, BTC context, Deep Research ni Liquidations. No ejecutar `pm2 save` automáticamente: revisar antes procesos `stopped`.

## Limitaciones importantes

* **No se garantiza captura de Order Book fresca en las 290 REACTIONs**: los límites REST + latencia lo hacen imposible sin fuentes/arquitectura adicionales. Los faltantes quedan `null`.
* La prioridad estructural es un **heurístico de observación**, no una segunda definición de REACTION ni señal de trade. El scanner canónico continúa intacto.
* El `known_ts` es el corte causal; los campos de mercado solo se unen cuando su `received_ms` y observación son anteriores. Nunca se imputan snapshots posteriores.
* Para disponer de liquidaciones históricas auditables antes de la poda de 3 horas del WebSocket, el linker persiste evidencia de los snapshots y heartbeats en la base lateral. El valor 0 significa cero **snapshots observados por el stream**, NO cero liquidaciones totales de Binance.
* La integración `load_deep_frame()` expone columnas al dashboard que utilice esa función. **No se editó `dashboard/app.py` ni se garantizan tablas nuevas en cada pestaña** sin revisar esos archivos (no venían en el ZIP).

Commit sugerido: `feat(reaction-lab): prioritize pre-retest telemetry and freeze asof market context`
