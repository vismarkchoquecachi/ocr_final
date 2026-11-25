# OCR de comprobantes de transferencia

Lee los PDF de la carpeta `pdfs` y de todas sus subcarpetas (`año-mes/año-mes-día/código`), envía a un modelo de IA con visión todas las páginas de cada PDF en una sola llamada y guarda por cada documento de pago que encuentra:

| Campo | Formato |
|---|---|
| NombreOrdenante | texto |
| FechaTransferencia | YYYY-MM-DD |
| INVOICE | texto |
| Total | decimal con dos cifras |
| NombreBeneficiario | texto |
| BancoBeneficiario | texto |

Los datos ausentes, ilegibles o ambiguos quedan vacíos con `Revisar = SI` y el motivo en `MotivoRevision`.

## Uso

Todo se ejecuta con Python, sin archivos `.bat`.

Una sola vez, instale las dependencias:

```
python -m pip install -r requirements.txt
```

Está configurado para Claude Haiku 5.5 (`ia_proveedor=anthropic`, `ia_modelo=claude-haiku-5-5`, `ia_esfuerzo=low`). Lo único que hay que completar en `.env` es `anthropic_clave`, con una clave de console.anthropic.com.

Después:

1. Copie las carpetas con los PDF en `pdfs` (o indique otra carpeta en `carpeta_pdf`), con la estructura `año-mes\año-mes-día\código\`, por ejemplo `pdfs\2026-10\2026-10-09\TRE00726053874\OTE.pdf`. Se buscan los PDF en todas las subcarpetas, no hace falta que estén todos los días, y los demás archivos se ignoran.
2. Ejecute `python main.py`: por cada PDF crea en la salida una carpeta con la misma ruta y el nombre del PDF, guarda ahí las imágenes recortadas, las envía a Claude todas juntas en una sola llamada con un solo prompt y deja un JSON con la respuesta, además del CSV.
3. Ejecute `python cargar_sql.py`: pasa las respuestas a SQL Server.
4. Abra el CSV en Excel o consulte la tabla en SQL Server.

La primera vez, si no existe `.env`, el programa lo crea a partir de `.env.example` y avisa para que complete las credenciales.

| Comando | Qué hace |
|---|---|
| `python main.py` | Procesa los PDF con lo configurado en `.env` |
| `python main.py --ventana` | Abre la interfaz gráfica |
| `python main.py --destino csv` | Guarda solo en el archivo CSV |
| `python main.py --destino sql` | Guarda solo en SQL Server |
| `python main.py --destino ambos` | Guarda en SQL Server y en el CSV |
| `python main.py --csv "D:\reportes\transferencias.csv"` | Elige en qué archivo CSV se guarda |
| `python main.py --entrada CARPETA --salida CARPETA` | Cambia las carpetas de PDF y de resultados |
| `python prompt.py` | Muestra el prompt completo que recibe la IA |
| `python cargar_sql.py` | Pasa a SQL Server las respuestas de `salida\respuestas_ia` |
| `python cargar_sql.py --prueba` | Muestra qué insertaría en SQL Server, sin insertar nada |

El programa devuelve 0 si todo salió bien y 1 si hubo errores, para usarlo en tareas programadas.

## El prompt

El texto que se le envía a la IA está en **`.env`**, en las variables que empiezan con `prompt_`. Es lo único que hay que editar para mejorar la extracción.

Está dividido en secciones, en el orden en que la IA las lee:

| Variable | Para qué sirve |
|---|---|
| `prompt_rol` | Presenta la tarea y qué puede venir en la página |
| `prompt_paso_1` | Formularios que se descartan (OTE) |
| `prompt_paso_2` | Cuándo una página se excluye y cuándo se extrae |
| `prompt_paso_3` | Qué no es un dato (títulos, etiquetas) |
| `prompt_paso_4` | Definición de cada campo: de dónde se toma y de dónde no |
| `prompt_reglas_finales` | Reglas generales y formato de la respuesta |
| `prompt_solicitud` | La frase que acompaña a cada imagen |

Cada sección va entre comillas simples y puede ocupar varias líneas. Lo que se escribe dentro es exactamente lo que recibe la IA. Se puede cambiar la redacción, agregar ejemplos o reglas, con dos cuidados:

- Dentro del texto no use comillas simples (`'`), porque cierran la sección. Las comillas dobles sí se pueden usar.
- No cambie los nombres de los campos, porque el CSV y SQL Server los usan.

Después de editar, `python prompt.py` muestra cómo quedó el prompt completo. Si falta una sección, el programa se detiene y avisa cuál es; el texto original está en `.env.example`.

Las páginas ya procesadas no se vuelven a enviar a la IA. Para probar un prompt nuevo sobre las mismas páginas use `omitir_existentes=false` en `.env` (o desmarque "Omitir páginas ya procesadas" en la ventana).

## Dónde se guardan los datos

Se elige con `destino` en `.env`, con `--destino` al ejecutar o en la ventana:

| destino | Guarda en |
|---|---|
| `csv` | Solo el archivo CSV |
| `sql` | Solo SQL Server |
| `ambos` | SQL Server y el archivo CSV |
| vacío | Automático: `ambos` si hay `sql_servidor`; si no, `csv` |

### Archivo CSV

- La ruta se indica en `csv_ruta` (o con `--csv`, o en la ventana). Si queda vacía se usa el nombre de `archivo_csv` (`resultados.csv`) en la carpeta de salida. Si se indica una carpeta, se guarda ese archivo dentro de ella. Las carpetas que falten se crean.
- En `.env` la ruta se escribe sin comillas, aunque tenga espacios: `csv_ruta=D:\mis reportes\transferencias.csv`.

### SQL Server

- El programa no crea, modifica ni borra nada en SQL Server: solo consulta e inserta.
- Una página que ya está en la tabla no se vuelve a enviar a la IA ni se modifica. Para procesarla de nuevo, borre antes su fila en SQL Server.
- Las páginas que fallan no se insertan; quedan en `resultados.jsonl` y en el CSV, y se reintentan en la siguiente ejecución. Antes de la primera ejecución, ejecute una vez `crear_tabla.sql` en SQL Server (por ejemplo en SQL Server Management Studio); crea la base `FacturasOCR` y la tabla `dbo.SeguimientoOCR` si no existen. Si cambia `sql_base_datos` en `.env`, cambie también el nombre de la base en ese archivo.
- El programa no comprueba que la tabla exista: da por hecho que ya fue creada. Si falta, se detiene con el error de SQL Server antes de llamar a la IA.
- Con `sql_usuario` vacío usa la autenticación de Windows.
- Para pasar a SQL Server las respuestas de la IA de la carpeta `respuestas_ia`, use `python cargar_sql.py`. Lee un JSON por página, valida los campos e inserta solo las páginas que no están en la tabla. Las páginas excluidas y las que tuvieron error de la IA no se cargan. Con `--prueba` muestra qué insertaría sin insertar nada, y con `--carpeta RUTA` lee otra carpeta.- Si la contraseña tiene espacios o `#`, escríbala entre comillas: `sql_clave="mi clave#1"`.

### Reglas de guardado

- Cada página se identifica por archivo de origen y número de página. No quedan duplicados. En el CSV, si la página ya existe se actualiza; en SQL Server solo se inserta y lo que ya está no se toca.
- Lo que ya estaba guardado y no pertenece a la ejecución actual se conserva.
- El destino se comprueba antes de procesar: si el CSV no se puede escribir o SQL Server no responde, el programa se detiene sin gastar en IA.
- En el CSV, una fila existente solo se reemplaza si la página se volvió a procesar; las correcciones hechas a mano y las columnas agregadas en Excel se mantienen.
- El CSV se escribe completo en un archivo temporal y se reemplaza de una vez, así que nunca queda a medias. Si el proceso se interrumpe, la siguiente ejecución lo completa a partir de `resultados.jsonl`.
- Si el CSV está abierto en Excel, los datos se guardan en una copia con fecha y hora en la misma carpeta, y se integran al archivo principal en la siguiente ejecución.
- Si la ruta elegida apunta a un archivo que no es de este programa, no se toca y se avisa.

## Configuración (.env)

Todo lo que se puede cambiar está en `.env`: credenciales, carpetas, nombres de archivos, parámetros y el prompt. Basta con editar ese archivo; no hay que tocar el código.

| Variable | Para qué sirve | Si queda vacía |
|---|---|---|
| `gemini_clave`, `openai_clave`, `anthropic_clave` | Clave de la IA; basta con una | - |
| `ia_proveedor` | `gemini`, `openai`, `anthropic` o `compatible` | Se detecta por la clave |
| `ia_modelo` | Modelo de IA | El modelo base del proveedor |
| `ia_url_base`, `ia_clave` | Servicio compatible con OpenAI (Ollama, OpenRouter, Groq, DeepSeek, Azure) | No se usa |
| `sql_servidor`, `sql_base_datos`, `sql_usuario`, `sql_clave` | Conexión con SQL Server | Sin servidor no se usa SQL |
| `destino` | `csv`, `sql` o `ambos` | Automático |
| `carpeta_pdf` | Carpeta donde están los PDF | `pdfs` |
| `carpeta_salida` | Carpeta de resultados | `salida` |
| `csv_ruta` | Ruta completa del CSV | Carpeta de salida |
| `archivo_csv` | Nombre del CSV | `resultados.csv` |
| `archivo_resultados` | Nombre del historial | `resultados.jsonl` |
| `archivo_excluidos` | Nombre del registro de páginas excluidas | `excluidos.jsonl` |
| `archivo_errores` | Nombre del registro de errores | `errores.jsonl` |
| `archivo_indice` | Nombre del índice de páginas ya procesadas | `indice.json` |
| `imagen_dpi` | Calidad de la imagen, entre 72 y 450 | `200` |
| `recorte_activo` | `true` recorta los márgenes en blanco antes de enviar la imagen | `true` |
| `recorte_diferencia`, `recorte_minimo_pixeles`, `recorte_margen_porcentaje`, `recorte_dpi_deteccion` | Sensibilidad del recorte y margen de seguridad | `40`, `2`, `2`, `50` |
| `carpeta_imagenes` | Carpeta, dentro de la carpeta de cada PDF en la salida, con las imágenes que se envían a la IA | `imagenes` |
| `carpeta_respuestas` | Carpeta, dentro de la carpeta de cada PDF en la salida, con el JSON de la respuesta de la IA | `respuestas_ia` |
| `ia_paginas_por_envio` | Máximo de páginas de un PDF en una sola llamada, entre 1 y 50; un PDF más largo se envía en partes | `20` |
| `ia_concurrencia` | Llamadas a la IA al mismo tiempo (cada una es un PDF), entre 1 y 16 | `4` |
| `ia_tiempo_espera` | Segundos de espera por respuesta, entre 10 y 600 | `120` |
| `ia_reintentos` | Reintentos si la IA falla, entre 0 y 10 | `5` |
| `omitir_existentes` | `true` para no reenviar páginas ya procesadas | `true` |
| `prompt_...` | Las secciones del prompt | El programa avisa cuál falta |

Si cambia `archivo_resultados` o `archivo_excluidos` después de haber procesado páginas, renombre también los archivos que ya existen en la carpeta de salida; si no, el programa no los encuentra y vuelve a enviar esas páginas a la IA.

## Páginas excluidas

No se extraen ni se guardan datos de:

- Formularios **ORDEN DE TRANSFERENCIAS AL EXTERIOR (OTE)**, estén solos o dentro de una página.
- Páginas en blanco o que solo tienen texto narrativo, sin un documento de pago.

Si la página tiene además una proforma, factura o comprobante, se extraen solo los datos de ese documento. Las páginas excluidas quedan anotadas en `excluidos.jsonl` (archivo, página y motivo) y en `indice.json`, para no volver a cobrarlas. Para revisar de nuevo una, borre su número de página en `indice.json`.

## Resultados

| Archivo | Contenido |
|---|---|
| CSV (`csv_ruta` o `salida\resultados.csv`) | Tabla para Excel, una fila por página |
| SQL `dbo.SeguimientoOCR` | Los mismos datos, más la respuesta original de la IA |
| `salida\resultados.jsonl` | Historial completo con la respuesta original de la IA |
| `salida\excluidos.jsonl` | Páginas excluidas y su motivo |
| `salida\indice.json` | Qué páginas ya se procesaron, por huella del PDF |
| `salida\errores.jsonl` | PDF que no se pudieron abrir y fallos al guardar |
| `salida\<año-mes>\<año-mes-día>\<código>\<pdf>\imagenes\` | PNG recortado de cada página enviada a la IA, `<pdf>_pagina_<n>.png` |
| `salida\<año-mes>\<año-mes-día>\<código>\<pdf>\respuestas_ia\` | Un JSON por llamada, `<pdf>.json` (o `<pdf>_parte_<n>.json` si el PDF se envió en partes), con la respuesta de la IA y sus métricas |

La salida repite la ruta de cada PDF dentro de `carpeta_pdf`. Por ejemplo, `pdfs\2026-10\2026-10-09\TRE00726053874\OTE.pdf` deja sus archivos en `salida\2026-10\2026-10-09\TRE00726053874\OTE\`. Solo se crea la carpeta de un PDF si alguna de sus páginas se envía a la IA.

## Cómo se envía cada PDF a la IA

Todas las páginas de un PDF van en una sola llamada: cada imagen precedida por su número ("Página 1", "Página 2"...) y el prompt una sola vez. Las páginas en blanco o con un formulario OTE con texto se descartan antes, sin llamar a la IA. Si el PDF tiene más páginas que `ia_paginas_por_envio`, o sus imágenes pesan más de 20 MB, se envía en partes.

La IA devuelve dos listas:

- `Documentos`: un elemento por documento de pago, con `Paginas` (las páginas donde está) y los campos. Un documento puede ocupar varias páginas, y un PDF puede tener varios documentos.
- `PaginasExcluidas`: las páginas sin documento de pago, con su motivo.

Cada documento es una fila en el CSV y en SQL Server; `NumPagina` es su primera página. Las páginas en `JsonCrudo` indican todas las que ocupa.

Cada JSON de `respuestas_ia` tiene `paginas` (las enviadas), `estado` (`extraida`, `excluida` o `error`), `respuesta` (las dos listas), `error` y `metricas` (tokens del prompt, de las imágenes, de entrada y de salida, segundos y datos de cada imagen). El nombre del PDF se acorta a 60 caracteres en la salida, porque Windows no acepta rutas de más de 260. Si en una misma carpeta dos PDF quedan con el mismo nombre, se les agrega una marca de 8 caracteres para que no se pisen.

## Cómo decide si una página se envía a la IA

Cada PDF se identifica por su huella (hash SHA-256 de su contenido completo), no por su nombre ni por su ruta. `indice.json` guarda, por cada huella, el estado de cada página: `ok`, `revisar`, `excluida` o `error`.

- `ok`, `revisar` o `excluida`: la página no se vuelve a enviar.
- `error` o sin estado: la página se envía.
- Si mueve, copia o renombra un PDF, la huella es la misma y no se vuelve a cobrar.
- Si cambia el contenido del PDF, la huella es otra y sus páginas se procesan de nuevo.
- Para reprocesar una página a propósito, borre su número en `indice.json`. Para reprocesar un PDF completo, borre su bloque.
- Con `omitir_existentes=false` se reenvían también las `ok` y `revisar`; las excluidas no.
- Si `indice.json` no existe, se arma solo a partir de `resultados.jsonl` y `excluidos.jsonl`.
- Una página que no está en el índice pero ya está en SQL Server con la misma ruta no se envía, porque como el programa solo inserta no se podría guardar. Si el PDF cambió, borre sus filas en SQL Server para procesarlo de nuevo.

## Archivos

| Archivo | Función |
|---|---|
| `main.py` | Punto de entrada: consola o ventana |
| `.env` | Credenciales, rutas, nombres de archivos, parámetros y el prompt |
| `prompt.py` | Arma el prompt con las secciones de `.env` |
| `ia.py` | Conexión con los proveedores de IA |
| `indice.py` | Índice de páginas ya procesadas, por huella del PDF |
| `procesamiento.py` | PDF a imagen, validación y exclusiones |
| `archivo_csv.py` | Guardado en CSV |
| `sql.py` | Guardado en SQL Server (solo consulta e inserta) |
| `cargar_sql.py` | Pasa a SQL Server las respuestas de `respuestas_ia`, sin llamar a la IA || `crear_tabla.sql` | Crea la base y la tabla; se ejecuta una sola vez en SQL Server |
| `config.py` | Lectura de `.env` |
| `requirements.txt` | Paquetes de Python que se instalan una sola vez |

Requisitos: Windows, Python 3.11+, internet y, para SQL Server, Microsoft ODBC Driver 17 o 18.
