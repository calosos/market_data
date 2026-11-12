# Datos Dukascopy

Esta carpeta guarda las descargas y una auditoría sencilla de CSV M1, BID y ASK, de Dukascopy/JForex.

- `raw/` contiene los CSV. No se versionan.
- `scripts/audit_download.py` solo lee los archivos. No los modifica, no rellena huecos y no genera otro CSV.
- `catalog/audits/` guarda los informes. En el repositorio queda un ejemplo.

El script usa la biblioteca estándar de Python.

## Ejecución

Desde esta carpeta:

```bash
python3 scripts/audit_download.py ruta/al/BID.csv ruta/al/ASK.csv
```

El primer argumento es BID y el segundo es ASK. Si falta un archivo, no se puede abrir o el encabezado no coincide, el script avisa y termina sin escribir informe.

Formato de entrada:

```text
Time (UTC);Open;High;Low;Close;Volume
2024.01.02 13:04:00;1,23456;1,23480;1,23440;1,23470;12,5
```

Separador de punto y coma, decimales con coma y fecha `YYYY.MM.DD HH:MM:SS`. La lectura prueba primero UTF-8 con BOM y, si falla, cp1252.

## Qué revisa

- Filas ilegibles, duplicados y fechas fuera de orden.
- OHLC incoherente, precios no positivos y volumen no finito, negativo o cero. Las barras de volumen cero consecutivas se agrupan en tramos.
- Huecos contra un minuto fijo. La mediana del intervalo observado es solo un diagnóstico. Un hueco puede ser un cierre de mercado; la etiqueta de fin de semana es orientativa.
- Fechas presentes en un archivo y ausentes en el otro.
- Diferencias de OHLC entre BID y ASK. Una diferencia negativa queda para revisar. Superar 10 veces la mediana es una observación, no un error.

Las marcas duplicadas no entran en la comparación y no se elige ninguna fila. Las filas con precios inválidos tampoco entran en las diferencias ni en las medianas. Esta revisión no establece cobertura completa: no hay calendario histórico ni un rango pedido por terminal.

La diferencia entre Open, High, Low y Close de una barra M1 no es el spread instantáneo.

## Informe

Muestra un resumen en terminal y guarda un Markdown en `catalog/audits/`. El nombre lleva la hora UTC de la ejecución, con microsegundos. Cada tipo de incidencia incluye el total y, como máximo, 200 fechas de detalle.
