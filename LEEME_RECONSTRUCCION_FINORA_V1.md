# Reconstrucción de líneas y bandas desde las velas

Esta entrega incorpora un modelo que genera un gráfico a partir del símbolo, el mercado, las velas cerradas y una hora de corte. La primera prueba usa NVDAUSDT de Binance, perpetuo, en 1h. También se incluyen SHELLUSDT spot y BTCUSDT perpetuo con sus capturas originales.

## Qué se construye

El detector produce las líneas estructurales, los rectángulos de oferta y demanda y sus bordes candidatos de venta y compra. Recibe las velas y el corte como entradas. Las imágenes sirven para cotejar posteriormente la distribución y los precios. Los cálculos se realizan al abrir el visor.

El modelo usa estas reglas experimentales.

1. Confirma pivotes de 4h con tres velas a cada lado. Conserva el máximo del tramo, las bases anteriores y posteriores, el mínimo y los últimos extremos confirmados. Agrupa líneas del mismo lado que estén a un máximo de 0,20 % y usa la confirmación más reciente.
2. Para la demanda, recorre los mínimos principales desde el más reciente. Usa el rango completo de la vela horaria que contiene ese mínimo, siempre que quede por debajo del precio y su borde inferior siga sin un cierre de invalidación posterior.
3. Para la oferta, obtiene candidatos desde máximos horarios confirmados 2×2 y desde la última vela alcista anterior a un hueco bajista. Prioriza dos rangos estrechos, situados sobre el precio, con su geometría vigente por cierre al corte y sin solapamiento entre sí.
4. El borde superior de la demanda y el inferior de la oferta se muestran como bordes candidatos del modelo. La validación de entradas declaradas por Finora necesita los niveles de entrada y la hora de sus planes publicados.

La regla de selección se desarrolló revisando NVDA. Su generalización se debe comprobar en otras fechas e instrumentos. El código reutiliza el detector de pivotes del proyecto, `engine/live/research/swing_detector.py`. Los parámetros de esta hipótesis corresponden al módulo nuevo.

## Resultado inicial de NVDA

Con corte en el 24 de noviembre de 2026 a las 14:00 UTC se generan estas cuatro líneas.

| Precio | Origen estructural |
|---:|---|
| 124,95 | Mínimo del tramo |
| 130,58 | Último mínimo principal confirmado |
| 149,22 | Base anterior al máximo |
| 171,60 | Nivel agrupado de los máximos de 171,88 y 171,60 |

Las tres bandas generadas son las siguientes.

| Lado del modelo | Inferior | Superior | Vela de origen UTC | Conocida por el modelo UTC |
|---|---:|---:|---|---|
| Venta candidata | 170,03 | 171,63 | 13 nov 11:00 | 13 nov 14:00 |
| Venta candidata | 165,08 | 167,35 | 24 nov 00:00 | 24 nov 04:00 |
| Compra candidata | 130,58 | 133,22 | 23 nov 02:00 | 23 nov 16:00 |

Las bandas y líneas tienen correspondencia visual con la captura original suministrada. La captura permite contrastar la disposición, los extremos y la escala logarítmica. Los valores numéricos exactos de cada borde y la hora UTC de generación de esa imagen están pendientes de confirmación. Esta revisión sigue siendo una reconstrucción visual de una hipótesis.

El corte inicial de las 14:00 UTC es una elección de esta prueba. El visor permite elegir otra fecha y hora dentro de la cobertura. Los orígenes de las figuras y sus horas de confirmación aparecen por separado en las tablas.

## SHELL y BTC

En SHELL, el mercado de la imagen es spot. El modelo identifica como demanda la vela del 23 de noviembre a las 09:00 UTC, con rango 0,02400–0,02429. La oferta dibujada por esta hipótesis todavía difiere de la captura.

En BTC, la demanda generada abarca 58.746,1–59.307,8 y procede de la vela del 22 de noviembre a las 05:00 UTC. La selección de las líneas superiores y de las bandas de oferta sigue pendiente de reconstrucción.

## Archivos y acceso

La entrega contiene fuentes completas.

- `finora_chart_reconstruction_v1.py` calcula las figuras a partir de las velas.
- `dashboard/views/reaction_multiswing_finora_reconstruction_v1_view.py` presenta el gráfico, las tablas y la captura original.
- `dashboard/views/reaction_multiswing_finora_v1_view.py` agrega el selector **Modo de trabajo** al acceso actual de Finora.
- `test_finora_chart_reconstruction_v1.py` verifica la causalidad de los cálculos, la invalidación por cierre y las velas de origen.
- `reports/reaction_lab/finora_reconstruction_v1/` contiene las velas, los manifiestos y las tres imágenes. Su `.gitattributes` conserva los bytes que verifican los hashes.

Copiá las rutas al proyecto combinando las carpetas. Desde la raíz, registrá las fuentes y la carpeta de datos de esta entrega.

```powershell
git add -- finora_chart_reconstruction_v1.py test_finora_chart_reconstruction_v1.py dashboard/views/reaction_multiswing_finora_v1_view.py dashboard/views/reaction_multiswing_finora_reconstruction_v1_view.py
git add -f -- reports/reaction_lab/finora_reconstruction_v1/
git commit -m "feat: reconstruye líneas y bandas desde las velas"
git push origin microservices
```

En el VPS, actualizá y ejecutá las pruebas.

```bash
cd /home/multiservices-1m
git pull --ff-only origin microservices
python3 -m unittest -v test_finora_chart_reconstruction_v1
```

En el dashboard, abrí **Ámbito del laboratorio → Comparación con Finora · replay → Modo de trabajo → Reconstrucción desde las velas**. El símbolo inicial es NVDAUSDT perpetuo. El modo **Referencias publicadas** abre el comparador anterior.

La comprobación local cubrió los tres símbolos, las tablas y el acceso al modo anterior. Truncar el histórico en el corte y cambiar todos los precios futuros produjo las mismas líneas y bandas conocidas.
