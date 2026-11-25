# Finora: ciclo de vida V1.2 y gráficos de hasta 60 días

## Ajuste visual V1.2.1: todas las franjas del período

El visor abre con **Franjas visibles → Todas las del período**. Al ampliar a 30 o 60 días incorpora automáticamente las franjas históricas de ese encuadre, incluidas las ya invalidadas o rellenadas. Las franjas vigentes que comenzaron antes también permanecen cuando su ciclo atraviesa el período. El contador informa cuántas se muestran. Las velas se dibujan sobre los rellenos para que sigan legibles cuando se superponen muchas franjas.

Para aislar una o varias, cambiar **Franjas visibles → Selección manual** y usar **Bandas en el gráfico**. Al regresar a **Todas las del período**, se recupera el histórico completo, aunque la selección manual tuviera solo ocho franjas o una sola. El journal sigue siendo V1.2 y los datos ya instalados son los mismos.

En BTC al corte 24 nov 2026, 14:00 UTC, doce días muestran **49 franjas** y sesenta días **135 franjas**: 91 de oferta/demanda y 44 huecos. El conteo incluye los ciclos que atraviesan el encuadre, aunque su detección sea anterior; los terminados antes quedan fuera.

Para actualizar desde una instalación V1.2 que ya tiene los datos, copiar las fuentes completas de este ZIP y registrar únicamente estos dos archivos:

```powershell
git add -- dashboard/views/reaction_multiswing_finora_lifecycle_v12_view.py LEEME_FINORA_CICLO_VIDA_V12.md
git diff --cached --stat
git commit -m "fix: muestra todas las franjas del periodo en el ciclo Finora"
git push origin microservices
```

En el VPS:

```bash
cd /home/multiservices-1m
git pull --ff-only origin microservices
pm2 restart dash-micro1m
```

Recargar con Ctrl+F5. El encabezado debe decir **Bandas y ciclo de vida · V1.2.1**. Elegir BTC, corte 24 nov 14:00 y 60 días: debe indicar **Mostrando 135 de 135 franjas del período**. Las franjas de septiembre y octubre deben quedar distribuidas sobre su propia historia. En selección manual se pueden elegir menos; al volver a Todas se deben recuperar las 135. La carpeta de reports incluida se conserva sin cambios.

Esta entrega conserva la primera detección de cada franja y sus contactos y terminaciones, aunque el selector posteriormente deje de elegirla. El gráfico diferencia el origen retrospectivo de la franja disponible y termina cada franja en su invalidación o llenado observado.

El selector geométrico sigue siendo la hipótesis V1.1, con ventana de cálculo de doce días. El ciclo V1.2 observa sus decisiones en cada cierre horario y las conserva en un journal. Extender el encuadre a sesenta días no modifica los nacimientos ni las reglas geométricas. Confirmación significa primera selección por este detector; no confirma una entrada ni reproduce por sí misma la lógica interna de Finora.

## Qué muestra el gráfico

| Elemento | Significado |
|---|---|
| Rectángulo gris punteado | Origen anterior a la detección, reconstruido retrospectivamente. La banda todavía no estaba disponible. |
| Franja roja o verde continua | Oferta o demanda a partir de su primera detección. |
| Franja gris continua | Hueco bajista que el selector incorporó a la vista. |
| Rombo amarillo | Primera detección por el modelo. Su fecha queda fija. |
| Círculo azul | Inicio de un episodio de contacto posterior a la detección. |
| Cruz roja | Primera invalidación por cierre de una banda de oferta o demanda. La franja termina ahí. |
| Cuadrado gris | Un hueco se rellenó completamente por una mecha. La franja gris termina ahí. |

Las horas de los eventos corresponden al cierre de una vela de 1h. Un contacto registrado a las 08:00 UTC ocurrió en la vela abierta a las 07:00; su minuto exacto no se deduce de OHLC. Varias velas consecutivas que intersectan la misma franja forman un episodio de contacto. Si una misma vela toca e invalida, se conservan ambos eventos con el mismo cierre; no se inventa el orden intravela. La posición vertical del círculo sirve para ubicar el contacto en la franja y no representa un precio de ejecución.

La oferta se invalida con el primer cierre de 1h estrictamente por encima de su borde superior. La demanda, con el primer cierre estrictamente por debajo del inferior. Una mecha o un cierre exactamente sobre el borde no invalidan una banda de oferta/demanda. Un hueco bajista termina cuando una mecha posterior alcanza su borde superior, aunque la vela cierre por debajo de ese borde.

Los contactos empiezan en la vela que abre a partir de la detección: la vela cuyo cierre permitió detectar la banda ya terminó y no se cuenta como retorno posterior. Los cambios de selección y de contexto se registran aparte y no invalidan ni reinician una franja. Una franja invalidada es terminal para esa identidad. El lado original se conserva; esta versión no crea cambios automáticos de función.

Las franjas vigentes se prolongan hasta el corte elegido: su resultado posterior sigue sin observarse. Las invalidadas permanecen en el catálogo y terminan en el cierre correspondiente. Elegir otra banda o cambiar el encuadre solo modifica lo que se ve.

## Fechas comprobadas para recorrer el visor

Todos los horarios son UTC y las fechas corresponden a 2026.

| Mercado y franja | Origen | Geometría | Primera detección | Primer contacto posterior | Terminación |
|---|---|---|---|---|---|
| SHELL oferta 0,02682–0,02729 | 18 nov 17:00 | 18 nov 20:00 | 19 nov 04:00 | 24 nov 08:00 | Vigente al corte 24 nov 14:00 |
| SHELL oferta 0,02479–0,02551 | 22 nov 17:00 | 22 nov 20:00 | 23 nov 08:00 | 23 nov 15:00 | Invalidada 24 nov 06:00 |
| SHELL demanda 0,02400–0,02429 | 23 nov 09:00 | 24 nov 00:00 | 24 nov 00:00 | Sin contacto al corte 24 nov 14:00 | Vigente al corte |
| BTC oferta 65.335,1–65.776,5 | 15 nov 13:00 | 15 nov 16:00 | 16 nov 00:00 | 16 nov 01:00 | Vigente al corte 24 nov 14:00 |
| BTC oferta 62.603,2–63.637,7 | 23 nov 15:00 | 23 nov 18:00 | 24 nov 04:00 | Sin contacto al corte 24 nov 14:00 | Vigente al corte |

La confirmación de un contexto posterior puede cambiar en el corte aislado V1.1. En el ciclo V1.2 esto produce un evento `CONTEXT_UPDATED`; la fecha de primera detección permanece fija. Por ejemplo, la oferta de SHELL 0,02682–0,02729 se detecta el 19 a las 04:00 y actualiza su contexto el 19 a las 20:00.

Antes de su primera detección, esa franja está ausente del replay y de sus exportaciones. En un corte posterior sí puede reconstruirse su origen con trazos punteados. Los eventos futuros y las terminaciones posteriores al corte permanecen ocultos en el gráfico, las tablas y las descargas del visor.

## Datos incluidos

Se agregan datos en **`reports/reaction_lab/finora_lifecycle_v12/`**. Los datos anteriores de `finora_reconstruction_v1/` se conservan.

Cada mercado tiene 1.843 velas cerradas de 1h, desde el **9 de septiembre de 2026, 00:00 UTC**, hasta el **24 de noviembre de 2026, 19:00 UTC** como cierre final. El corte inicial del visor es el 24 a las 14:00. En ese corte, sesenta días visibles comienzan el 25 de septiembre a las 14:00; las velas anteriores aportan contexto.

| Mercado | Bandas de oferta/demanda | Huecos | Franjas totales | Eventos del archivo completo |
|---|---:|---:|---:|---:|
| BTCUSDT Binance perpetuo | 101 | 47 | 148 | 1.142 |
| NVDAUSDT Binance perpetuo | 89 | 51 | 140 | 897 |
| SHELLUSDT Binance spot | 75 | 46 | 121 | 936 |

Los recuentos son observaciones de nuestra hipótesis, no coincidencias certificadas con Finora ni resultados de operaciones. El archivo completo contiene los eventos de toda la cobertura; el visor filtra por disponibilidad al corte. Los nacimientos registrados mientras faltaba una ventana inicial completa de cálculo se señalan como **Cobertura inicial limitada**. Su primera observación podría ser posterior a una existencia anterior al inicio de los datos.

La carpeta contiene tres CSV de velas, tres de franjas, tres de eventos, el manifiesto con hashes/procedencia/configuración y `.gitattributes`. Ese archivo conserva los bytes verificados mediante `* -text`. El journal está ya generado: el dashboard lo lee y verifica; abrir el gráfico no vuelve a ejecutar el replay ni descarga datos.

## Archivos de código

- `finora_band_lifecycle_v12.py`: reproducción causal, identidades, eventos y proyección al corte.
- `test_finora_band_lifecycle_v12.py`: pruebas del ciclo y del histórico.
- `dashboard/views/reaction_multiswing_finora_lifecycle_v12_view.py`: visor nuevo.
- `dashboard/views/reaction_multiswing_finora_reconstruction_v1_view.py`: selector entre ciclo V1.2 y corte aislado V1.1.

El paquete también conserva las fuentes completas, pruebas, imágenes y datos de la entrega anterior. Se reutiliza `engine/live/research/swing_detector.py`, que ya pertenece al proyecto. Las vistas deben quedar en **`dashboard/views/`**.

## Subir desde tu PC: incluir explícitamente reports

Extraer el ZIP en la raíz del proyecto combinando carpetas. En PowerShell, desde esa raíz:

```powershell
git status --short
git add -- finora_band_lifecycle_v12.py test_finora_band_lifecycle_v12.py dashboard/views/reaction_multiswing_finora_lifecycle_v12_view.py dashboard/views/reaction_multiswing_finora_reconstruction_v1_view.py LEEME_FINORA_CICLO_VIDA_V12.md FINORA_CICLO_VIDA_V12.png
git add -f -- reports/reaction_lab/finora_lifecycle_v12/.gitattributes
git add -f -- reports/reaction_lab/finora_lifecycle_v12/
git diff --cached --stat
git commit -m "feat: agrega ciclo de vida de bandas Finora y vista de 60 dias"
git push origin microservices
```

Los dos `git add -f` apuntan a la carpeta nueva concreta, aunque `reports` esté ignorado. El diff debe incluir sus once archivos. Las fuentes y datos ya registrados de V1.1 pueden seguir sin cambios. El archivo antiguo `reports/reaction_lab/finora_reconstruction_v1/download` no interviene en estos cálculos ni se incluye en esta entrega.

## Actualizar y comprobar en el VPS

```bash
cd /home/multiservices-1m
git pull --ff-only origin microservices
ls -lh reports/reaction_lab/finora_lifecycle_v12/manifest.json
python3 -c "from finora_band_lifecycle_v12 import VERSION; print('Ciclo de vida:', VERSION)"
python3 -m unittest -v test_finora_band_lifecycle_v12 test_finora_chart_reconstruction_v1 test_finora_band_comparison_v1
```

El resultado esperado es **Ciclo de vida: 1.2** y **24 tests OK**. Las pruebas cubren primera detección inmutable, contexto posterior, salida de la selección, contactos consecutivos, velas posteriores al nacimiento, invalidaciones de ambos lados, terminalidad, relleno de huecos, ocultamiento de eventos futuros, ampliación a sesenta días, paridad del detector con su prefijo completo y las once regresiones anteriores.

Comprobar el proceso del dashboard y reiniciarlo para que cargue las fuentes nuevas:

```bash
pm2 show dash-micro1m
pm2 restart dash-micro1m
pm2 logs dash-micro1m --lines 30 --nostream
```

El proceso `dash-micro1m` debe corresponder al directorio `/home/multiservices-1m`. Luego recargar el navegador con Ctrl+F5.

## Prueba paso a paso del dashboard

En V1.2.1, para los pasos que piden elegir una franja en **Bandas en el gráfico**, primero cambiar **Franjas visibles → Selección manual**. Para el histórico completo usar **Todas las del período**.

1. Abrir **Modo de investigación Multi-Swing → Mapa BTC 60D · 6 TF → Ámbito del laboratorio → Comparación con Finora · replay**.
2. En **Modo de trabajo**, elegir **Reconstrucción desde las velas**. En el nuevo selector **Vista de reconstrucción**, elegir **Ciclo de vida · V1.2**. El título debe ser **Bandas y ciclo de vida · V1.2**.
3. Elegir **SHELLUSDT · Binance spot**, fecha **2026/11/24**, hora **14:00 UTC** e historia **12 días**. En **Bandas en el gráfico**, dejar solo **Venta 0.02682–0.02729**. Se pueden borrar las otras etiquetas con su cruz. Deben verse el origen punteado, el rombo del 19 a las 04:00 y el círculo del 24 a las 08:00. La tabla debe mostrar detección 19 nov 04:00, primer contacto 24 nov 08:00 y estado vigente.
4. Manteniendo SHELL, cambiar el corte a **2026/11/19, 20:00 UTC**, y elegir nuevamente esa banda. Su detección debe seguir en **19 nov 04:00**, y el contacto del día 24 debe permanecer oculto. En **Eventos conocidos al corte** aparecen detección y actualización de contexto por separado.
5. Retroceder a **2026/11/19, 03:00 UTC**. La banda aún no debe aparecer en el catálogo ni entre las opciones. Avanzar a las **04:00**: ya puede aparecer con su rombo. El origen punteado anterior es una reconstrucción retrospectiva, no una detección anticipada.
6. Volver a **2026/11/24, 14:00 UTC**. Elegir solo **Venta 0.02479–0.02551**. Debe verse nacida el 23 a las 08:00 y terminada con cruz roja el 24 a las **06:00 UTC**. La franja no debe prolongarse después de esa cruz.
7. Cambiar el corte a **2026/11/24, 05:00 UTC** y seleccionar de nuevo esa banda: todavía figura vigente y sin invalidación. A las **06:00** aparece la invalidación y su dibujo termina. El cierre observado de esa vela es **0,02583**.
8. Elegir el **hueco 0.02467–0.02479** si se quiere revisar el gris asociado: se detecta el 23 a las 08:00 y queda rellenado el 23 a las **15:00 UTC**, con cuadrado gris.
9. Volver al corte **24 nov 14:00** y cambiar Historia visible a **30** y luego **60 días**. Repetir con BTCUSDT perpetuo y NVDAUSDT perpetuo. Las primeras detecciones de una misma identidad deben conservarse. En el gráfico de sesenta días hay 1.440 velas; se puede acercar con el zoom de Plotly y elegir otras franjas del catálogo.
10. Seleccionar una banda invalidada de fechas anteriores para ver su origen, detección, contactos y final. Las franjas anteriores que continúan vigentes se extienden desde la detección hasta el corte; las que terminaron antes del comienzo de la ventana quedan fuera de ese encuadre.
11. Descargar **ciclo de bandas al corte** y **eventos conocidos al corte**. Ambas descargas corresponden a las bandas seleccionadas y omiten los eventos posteriores al corte.
12. En **Vista de reconstrucción → Corte aislado · V1.1**, comprobar las bandas anteriores y el selector V1/V1.1. En **Modo de trabajo → Referencias publicadas**, abrir LINK y activar Revelar POST. Finalmente, regresar al mapa BTC 60D.

El gráfico muestra inicialmente todas las franjas cuyos ciclos atraviesan el período. Selección manual permite concentrarse en una sola o elegir un subconjunto; su selección inicial ofrece las franjas elegidas al corte y completa hasta ocho recientes. Ampliar los días cambia el encuadre y el catálogo; no reconstruye los nacimientos.

## Reproducción offline opcional

Los CSV del journal ya vienen incluidos. Para investigar una reproducción completa a partir de las mismas fuentes y del mismo selector:

```bash
python3 finora_band_lifecycle_v12.py --project /home/multiservices-1m --build
```

Este comando vuelve a escribir exclusivamente los journals y el manifiesto de `finora_lifecycle_v12/`; la vista normal no lo ejecuta. Un cambio del detector, sus parámetros o sus fuentes requiere una versión de investigación separada para comparar resultados. Las franjas y sus eventos son herramientas de auditoría histórica, no órdenes de compra o venta.
