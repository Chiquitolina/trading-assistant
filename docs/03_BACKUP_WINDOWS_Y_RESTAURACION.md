# Backup y apertura local — guía operativa Windows

## Qué entrega esta versión

- Un respaldo `.tar.gz` privado con rutas preservadas bajo `mirror/home/multiservices-1m/...` y `mirror/root/...`.
- CSV, JSON, SQLite (snapshot online consistente por base), OHLCV/cachés, monitores, Replay/Competition/Shadow/Selection/Policy, y opcionalmente código.
- Un `backup_manifest.json` que contiene SHA-256 y tamaño de **cada** archivo. El descargador vuelve a leer y verificar todo el `.tar.gz` antes de marcarlo como descargado correctamente.
- Un explorador histórico local independiente y de **solo lectura** que abre CSV, SQLite, JSON y parquet; **NO es todavía el mismo dashboard LIVE de la VPS**.

## 1. VPS: instalar solo un script de lectura

Subir `scripts/research_mirror_vps.py` a `/home/multiservices-1m/scripts/research_mirror_vps.py`.

```bash
cd /home/multiservices-1m
python3 -m py_compile scripts/research_mirror_vps.py
python3 scripts/research_mirror_vps.py list --include-code
```

Comprobar: `file_count`, `uncompressed_source_bytes`, `by_category` y `missing_expected`.
**El inventario previo no congela datos**; refleja el estado actual. El archivo de 290 símbolos puede estar todavía cambiando.

## 2. PC Windows: descargar por SSH, sin ZIP temporal en VPS

Necesitás Python 3, OpenSSH (`ssh -V`) y espacio libre en PC. Guardar `windows/descargar_research_pc.py` en `C:\ResearchTools\`.

En PowerShell:

```powershell
cd C:\ResearchTools
py -3 .\descargar_research_pc.py --host root@IP_VPS --dest "D:\ReactionResearch\Backups" --include-code
```

El programa muestra inventario y espacio disponible, pide `YES`, recibe un stream comprimido, comprueba SHA-256 por archivo y guarda `reaction_mirror_...tar.gz`, `.manifest.json`, `.verification.json`.
Si falla la transferencia o la validación, deja un archivo `.part` y **no declara éxito**. Se recomienda usar autenticación SSH por clave ya configurada.

**Privacidad:** el modo `--include-code` excluye archivos de credenciales comunes por nombre, pero no garantiza que un `.py`, `.json`, CSV o SQLite no incluya secretos embebidos. **El archivo debe mantenerse privado; revisarlo antes de compartir con terceros.** Nunca incluye deliberadamente `.env` ni credenciales SSH. Este espejo no copia apps ajenas a `/home/multiservices-1m` ni el árbol independiente `/home/multiservices`.

## 3. Verificar nuevamente en la PC

```powershell
py -3 .\descargar_research_pc.py --verify-only "D:\ReactionResearch\Backups\reaction_mirror_FECHA.tar.gz"
```

Los hashes verificados prueban la **integridad de los bytes transferidos**. No prueban que el proyecto estaba inactivo, que todas las SQLite se tomaron al mismo segundo, ni que 290 símbolos ya estaban completos. Si el manifiesto informa `COMPLETE_WITH_LIVE_CHANGES`, la copia es utilizable como corte histórico con modificaciones observadas, pero no representa un snapshot global atómico.

## 4. Extraer y navegar la copia

```powershell
mkdir "D:\ReactionResearch\Extraido"
tar -xzf "D:\ReactionResearch\Backups\reaction_mirror_FECHA.tar.gz" -C "D:\ReactionResearch\Extraido"
```

Quedará `D:\ReactionResearch\Extraido\mirror\home\multiservices-1m\...` y `...\mirror\root\runner_*\...`, manteniendo el parentesco entre carpetas. **No ejecutar ningún motor LIVE contra esas carpetas.**

Explorador local de datasets (necesita conexión solo la primera vez para instalar Streamlit):

```powershell
py -3 -m venv C:\ResearchTools\venv
C:\ResearchTools\venv\Scripts\python.exe -m pip install streamlit pandas pyarrow
C:\ResearchTools\venv\Scripts\python.exe -m streamlit run C:\ResearchTools\explorador_research_local.py -- --mirror "D:\ReactionResearch\Extraido\mirror"
```

Abrir el enlace `localhost` que muestre Streamlit. Se pueden filtrar nombres, inspeccionar tablas SQLite (solo SELECT), CSV, JSON y graficar columnas. El explorador no ejecuta `.pkl` ni intenta levantar Market Data, órdenes, PM2 o Redis.

## 5. ¿Cómo tener el dashboard original *idéntico* en Windows?

**Esta fase NO certifica compatibilidad ni identidad visual exacta.** El original (`dashboard/app.py`) importa servicios del proyecto, usa rutas absolutas `/home/multiservices-1m` y `/root`, carga `.env` y tiene vistas LIVE que consultan Redis. Trasladar código y archivos no recrea estados Redis/WS ni garantiza que algunas vistas se puedan abrir sin su infraestructura. El segundo dashboard PM2 usa otro repositorio (`/home/multiservices`) que no se auditó aquí.

Para lograr una réplica real, la **siguiente fase** debe implementar en el proyecto un `HISTORICAL_LOCAL_READONLY=1`: rutas reubicables, fuentes de datos solo desde el espejo, deshabilitación comprobada de escrituras/red/live/trading, pruebas de paridad de tablas y gráficos. Idealmente usar WSL2 Ubuntu y un entorno virtual separado, sin copiar secretos. No recomiendo ejecutar `main.py`, `engine.live.main` ni `pm2 resurrect` en la PC.

## 6. Política de copias

- **Copia ahora**: snapshot de los estudios 20 símbolos y estado parcial de 290.
- **Copia al terminar Replay 290**: esperar `summary.json`, validar gaps/descargas y generar Context/Portfolio/Competition/Shadow/Selection/Policy completos; luego respaldar otra vez.
- Conservar las dos copias, sus manifiestos y el SHA-256 externo del archivo completo.
- Si el disco de la VPS es limitado, no generar archives grandes localmente: los scripts transmiten por SSH. El backup SQLite usa un temporal por base (mayor fuente observada ~751 MiB) que se elimina después de transmitir cada DB; requiere margen libre y puede competir con el replay por I/O.
