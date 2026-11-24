# Reconstrucción Finora: ajuste de oferta V1.1

El ajuste cambia la generación y selección de las bandas rojas. Incluye bases casi neutras, agrupa la oferta por tramos de 4h y separa los huecos grises de los rectángulos de oferta. Mantiene la hipótesis V1 disponible para comparar desde el mismo visor.

Las reglas se desarrollaron revisando las tres capturas suministradas: NVDA perpetuo, BTC perpetuo y SHELL spot. Los resultados de esas capturas son ejemplos de ajuste, pendientes de validación independiente. El detector recibe exclusivamente las velas, el corte y parámetros comunes a los instrumentos. No recibe símbolos, imágenes ni precios objetivo de Finora.

## Resultado al 24 de noviembre de 2026, 14:00 UTC

| Mercado | Oferta V1.1 | Demanda |
|---|---|---|
| NVDAUSDT perpetuo | 170,03–171,63 y 165,08–167,35 | 130,58–133,22 |
| BTCUSDT perpetuo | 65.335,1–65.776,5 y 62.603,2–63.637,7 | 58.746,1–59.307,8 |
| SHELLUSDT spot | 0,02682–0,02729 | 0,02400–0,02429 |

Los huecos grises próximos que continúan sin llenarse completamente son NVDA 164,83–165,20 y 161,65–163,10; BTC 62.225,2–62.603,2; SHELL 0,02741–0,02784. Esto corrige la elección anterior de oferta de BTC y aproxima el rectángulo rojo y el gris de SHELL a la distribución de la captura.

Estos valores proceden de las velas. Los bordes de Finora solo pueden estimarse visualmente porque las imágenes no los rotulan. La hora UTC exacta de cada captura sigue pendiente. La coincidencia visual no verifica las reglas internas de Finora ni los precios de entrada de sus planes.

Las horizontales conservan la regla V1. En NVDA aparecen 124,95, 130,58, 149,22 y 171,60. En BTC siguen existiendo diferencias con las horizontales de la captura, incluido el nivel rotulado 68.130,4. El ajuste actual se concentra en las bandas de oferta.

## Qué calcula V1.1

1. Usa los pivotes confirmados 3×3 de 4h del módulo anterior. Un tramo de oferta comienza en un máximo y termina en el siguiente mínimo 4h confirmado disponible; cuando aún no existe ese mínimo, llega al corte. Conserva una base representativa por tramo.
2. Busca huecos bajistas de tres velas. La geometría queda disponible cuando cierra la tercera. Una base neutra es la primera vela del hueco con un cuerpo de hasta el 25 % de su rango, cualquiera sea el color. Prioriza la primera base neutra válida del tramo.
3. Si no encuentra una base neutra válida, busca un máximo horario compacto confirmado 2×2, dentro de las ocho horas anteriores al máximo 4h y a una distancia máxima de 0,20 % de su precio. Entre estos máximos elige la vela más estrecha. Como siguiente alternativa usa la primera última vela alcista anterior a un hueco bajista del tramo, con búsqueda hacia atrás de ocho velas.
4. El ancho de la oferta debe ser como máximo el 3 % de su borde superior y su borde inferior debe quedar sobre el último cierre. Un cierre posterior sobre su borde superior la invalida desde la formación de su geometría, incluso si el contexto de 4h confirma más tarde.
5. Selecciona hasta dos contextos: el máximo 4h de mayor precio entre los elegibles y el más reciente. Cuando coinciden o se solapan, completa con otros contextos por recencia, sin solapamiento de bandas. Esto reemplaza la prioridad global de V1 por las velas más angostas.
6. Muestra por separado los huecos de esos tramos que aún no han sido llenados completamente por una mecha posterior y cuya distancia a la banda seleccionada no supera el ancho de esa banda. El llenado completo de un hueco y la invalidación por cierre de una banda son eventos distintos.

Los límites de cuerpo, ancho, distancia, agrupación y selección son parámetros de esta hipótesis. No están confirmados como reglas de Finora. Las guías públicas de Finora distinguen bloques de origen y huecos y describen un indicador de estructura con otras reglas; este ajuste reproduce geometría experimental y no implementa íntegramente ese indicador.

## Horarios y reproducción causal

Las tablas separan la vela de origen, la formación de la geometría y la confirmación de todo el contexto utilizado. La hora del contexto es conservadora: incluye la confirmación del máximo 4h y, cuando se usa para delimitar el tramo, la del siguiente mínimo 4h. La selección se evalúa nuevamente al corte elegido.

| Oferta | Origen UTC | Geometría formada UTC | Disponible con este contexto UTC |
|---|---|---|---|
| NVDA 170,03–171,63 | 13 nov 11:00 | 13 nov 14:00 | 14 nov 08:00 |
| NVDA 165,08–167,35 | 24 nov 00:00 | 24 nov 04:00 | 24 nov 04:00 |
| BTC 65.335,1–65.776,5 | 15 nov 13:00 | 15 nov 16:00 | 17 nov 04:00 |
| BTC 62.603,2–63.637,7 | 23 nov 15:00 | 23 nov 18:00 | 24 nov 04:00 |
| SHELL 0,02682–0,02729 | 18 nov 17:00 | 18 nov 20:00 | 19 nov 20:00 |

Un rectángulo se dibuja desde su origen para poder revisar la vela. Su extremo izquierdo no implica que la banda se conociera en ese instante. Por ejemplo, el contexto completo de la oferta reciente de BTC confirma a las 04:00 UTC del 24; este visor no acredita que pudiera elegirse antes de la caída del día 23. Los triángulos identifican bordes candidatos del modelo.

La ventana de cálculo es de doce días. El selector Historia visible solo modifica el encuadre entre siete, diez y doce días; las bandas calculadas al mismo corte se mantienen.

## Archivos completos y actualización

El ZIP conserva los datos de la reconstrucción anterior y sus hashes. Las fuentes se copian combinando las carpetas con el proyecto. Las vistas deben quedar dentro de `dashboard/views/`.

- `finora_chart_reconstruction_v1.py`
- `dashboard/views/reaction_multiswing_finora_reconstruction_v1_view.py`
- `dashboard/views/reaction_multiswing_finora_v1_view.py`
- `test_finora_chart_reconstruction_v1.py`
- `LEEME_RECONSTRUCCION_FINORA_V1.md`
- `AUDITORIA_FINORA_OFERTA_V11.csv`
- `FINORA_OFERTA_V11_TRES_SIMBOLOS.png`

Se conserva la dependencia existente `engine/live/research/swing_detector.py`. Los archivos históricos de `reports/reaction_lab/finora_reconstruction_v1/` son los mismos de la entrega anterior y siguen teniendo su `.gitattributes` con `* -text`.

En PowerShell, desde la raíz del proyecto, después de copiar los archivos:

```powershell
git status --short
git add -- finora_chart_reconstruction_v1.py test_finora_chart_reconstruction_v1.py dashboard/views/reaction_multiswing_finora_reconstruction_v1_view.py dashboard/views/reaction_multiswing_finora_v1_view.py LEEME_RECONSTRUCCION_FINORA_V1.md AUDITORIA_FINORA_OFERTA_V11.csv FINORA_OFERTA_V11_TRES_SIMBOLOS.png
git diff --cached --stat
git commit -m "feat: ajusta oferta Finora con bases neutras y contexto 4h"
git push origin microservices
```

Los datos ya registrados en Git en la entrega anterior no requieren volver a forzarse. Si esta es una instalación nueva, primero registrar exclusivamente esta carpeta de datos con `git add -f -- reports/reaction_lab/finora_reconstruction_v1/`.

En el VPS:

```bash
cd /home/multiservices-1m
git pull --ff-only origin microservices
python3 -m unittest -v test_finora_chart_reconstruction_v1 test_finora_band_comparison_v1
```

El resultado esperado es **11 tests OK**: ocho de reconstrucción y tres del comparador anterior. Las pruebas verifican los ejemplos de geometría, los huecos, la invalidación anterior a la confirmación tardía del contexto, las velas de origen, la conservación de V1 y la invariancia al truncar o modificar precios futuros en seis cortes de los tres mercados. Esta verificación no mide rentabilidad ni constituye validación independiente de la selección.

## Prueba completa en el dashboard

1. Recargar el dashboard y abrir **Modo de investigación Multi-Swing → Mapa BTC 60D · 6 TF → Ámbito del laboratorio → Comparación con Finora · replay**.
2. En **Modo de trabajo**, elegir **Reconstrucción desde las velas**. Debe aparecer el nuevo selector **Selección de oferta** con **V1.1 · bases y tramos de 4h** como opción inicial.
3. Elegir fecha **2026/11/24**, hora **14:00 UTC** e historia visible **12 días**. Revisar NVDAUSDT perpetuo: cuatro horizontales, dos bandas rojas y una verde; contrastar los valores de las tablas con la primera tabla de este documento.
4. Cambiar a BTCUSDT perpetuo con el mismo corte. Deben aparecer las ofertas 65.335,1–65.776,5 y 62.603,2–63.637,7, con la demanda 58.746,1–59.307,8. En **Oferta considerada por tramo de 4h** se ven las bases intermedias consideradas y cuáles se muestran.
5. Cambiar a **V1 anterior · bandas más angostas**. En BTC deben regresar las ofertas 65.260,5–65.500,2 y 68.841,4–69.097,8. Volver a V1.1 para comprobar el cambio con las mismas velas.
6. Elegir SHELLUSDT spot. Debe verse una banda roja 0,02682–0,02729, el hueco gris superior 0,02741–0,02784 y la demanda 0,02400–0,02429.
7. Abrir **Captura original de Finora** en cada mercado. Comparar posición y bordes; el precio final de las capturas puede diferir porque su hora exacta no está identificada.
8. Cambiar Historia visible a **7 días** y volver a 12. Las tablas de bandas deben conservar los valores. Las bandas anteriores al encuadre continúan extendidas hasta el corte.
9. Cambiar a un corte anterior, por ejemplo **2026/11/23, 14:00 UTC** en BTC. La oferta del día 23 a las 15:00 aún no puede aparecer. Revisar los horarios de las tablas antes de regresar al corte inicial.
10. Descargar las tablas de líneas y bandas para verificar precios y horarios. Cambiar **Modo de trabajo → Referencias publicadas**, abrir LINK, activar **Revelar POST** y comprobar que siguen disponibles sus contactos posteriores. Finalmente, volver al mapa BTC 60D.

La comprobación local también recorre la entrada normal del laboratorio, los tres símbolos, el cambio V1/V1.1, el encuadre de siete días, un corte anterior y el comparador LINK con POST.

## Referencias del contraste

Las capturas originales suministradas por el usuario y las velas congeladas son la referencia de esta revisión. Las explicaciones generales de Finora se consultaron en https://finorabot.com/about/, https://finorabot.com/ai-crypto-chart-analysis/ y https://finorabot.com/blog/order-block-vs-fair-value-gap/. No contienen los parámetros de selección utilizados aquí.
