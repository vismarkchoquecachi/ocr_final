# Seguimiento del proyecto OCR de transferencias

Última actualización: 2026-11-17

Carpeta de trabajo: `D:\BIG DATA\ocr_facturas\ocr_final`
Repositorio: https://github.com/vismarkchoquecachi/ocr_final (privado, rama `main`)

## Estado actual

| Tema | Estado |
|---|---|
| Programa funcionando con Gemini, CSV y SQL Server | Listo |
| Toda la configuración y el prompt en `.env` | Listo |
| SQL Server solo consulta e inserta | Listo |
| Tabla creada a mano con `crear_tabla.sql` | Listo |
| Importar lo ya procesado sin llamar a la IA (`importar.py`) | Listo |
| Código reescrito en estilo simple | Listo |
| Índice por huella del PDF (`indice.json`) | Listo |
| Cambios subidos a GitHub | Listo, commit `2520ef4` |
| Prueba real con un PDF nuevo enviado a la IA | Pendiente |

## Cambios realizados

### 2026-11-16

1. **Variables en español y minúsculas.** Las secciones del prompt pasaron a `rol`, `paso_1` a `paso_4`, `reglas_finales` y `solicitud`.
2. **Configuración en `.env`.** Credenciales, servidor SQL, rutas, nombres de archivos, parámetros y el prompt se cambian solo ahí. Las claves quedaron en minúsculas (`gemini_clave`, `sql_servidor`, `carpeta_pdf`, etc.).
3. **Prompt en `.env`.** Variables `prompt_rol` a `prompt_solicitud`. Se comprobó que el texto que recibe la IA es idéntico al anterior.
4. **Sin emojis y con comentarios simples**, en minúsculas, sin acentos y solo con comas.
5. **Carpeta `ocr_final`** con solo lo necesario para funcionar.
6. **SQL Server sin DDL en el código.** El programa ya no crea base, tabla ni índice. El DDL está en `crear_tabla.sql`, para ejecutarlo a mano una vez.
7. **Solo INSERT, sin UPDATE.** Una página que ya está en la tabla no se modifica. Las páginas con error no se insertan.
8. **Se quitó la comprobación de que la tabla existe.** Si falta, el programa se detiene con el error de SQL Server.
9. **Subida a GitHub.** Repositorio privado, sin el `.env`, sin `pdfs` y sin `salida`. Commit `68221ca`.
10. **Clave quitada de `probar_gemini.py`** en la carpeta original. Ahora la lee del `.env`.

### 2026-11-17

1. **INSERT con columnas explícitas** en `sql.py`, una por una, con los valores en el mismo orden.
2. **`importar.py`.** Pasa a SQL Server lo que está en `resultados.jsonl`, sin llamar a la IA. Con `--prueba` muestra qué insertaría.
3. **Código en estilo simple.** Sin funciones lambda, sin listas ni diccionarios armados en una línea, sin funciones dentro de funciones, con bloques separados por líneas en blanco. `RepositorioSql` usa `conectar()` y `cerrar()`.
4. **Se detiene apenas la clave es inválida.** Antes seguía creando y enviando páginas del PDF hasta notarlo.
5. **Índice por huella del PDF.** Archivo nuevo `indice.py` y variable `archivo_indice=indice.json`. La decisión de enviar una página ya no depende de la ruta ni del nombre del PDF.
6. **Página del flujo completo:** https://claude.ai/artifact/3cQhratvJt1wXdTs9oYubF (la sección 4 todavía describe la regla anterior al índice).

## Cómo decide hoy si una página se envía a la IA

1. Se calcula la huella SHA-256 del contenido completo del PDF.
2. Se busca la página en `indice.json` por huella + número de página.
   - `ok`, `revisar` o `excluida`: no se envía.
   - `error` o sin estado: sigue al paso 3.
3. Si no está en el índice pero ya está en SQL Server con la misma ruta, no se envía y se avisa.
4. Si está en blanco o tiene el título OTE en el 20% superior, se excluye sin llamar a la IA.
5. Si no, se crea el PNG y se envía.

Resultado de la prueba:

| Caso | Llamadas a la IA |
|---|---|
| Segunda ejecución sin cambios | 0 |
| PDF movido a otra carpeta | 0 |
| PDF renombrado | 0 |
| Mismo nombre, contenido cambiado | Se procesa de nuevo |

## Pendientes

- [x] **Subir a GitHub los cambios del 2026-11-17.** Commit `2520ef4`.
- [ ] **Borrar las 3 filas de error en SQL Server** antes de reprocesar. Son las páginas 7, 10 y 12 de `CASOS DE TRANSFERENCIAS.pdf`. El índice las marca como `error` y las va a reenviar, pero el INSERT fallará mientras esas filas sigan en la tabla:

  ```sql
  DELETE FROM dbo.SeguimientoOCR
  WHERE JSON_VALUE(JsonCrudo, '$.tipo') = N'ERROR_PROCESAMIENTO';
  ```

- [ ] **Decidir qué carpeta de salida usar.** La de `ocr_final\salida` está vacía. El historial real está en `D:\BIG DATA\ocr_facturas\salida`. Para usarlo, en `.env`:

  ```
  carpeta_pdf=D:\BIG DATA\ocr_facturas\pdfs
  carpeta_salida=D:\BIG DATA\ocr_facturas\salida
  ```

- [ ] **Hacer una prueba real** con un PDF nuevo para confirmar el envío a la IA y el guardado en SQL Server.
- [ ] **Cambiar la clave de Gemini.** Estuvo escrita en `probar_gemini.py` y sigue en `asda\probar_gemini.py`.
- [ ] **Opcional: actualizar la sección 4 de la página del flujo** con la regla del índice.
- [ ] **Opcional: copiar las pruebas a `ocr_final`.** Están en `D:\BIG DATA\ocr_facturas\tests` y usan el nombre anterior `_post`, que ahora es `enviar`.

## Límites conocidos

- Si alguien vuelve a guardar o a escanear un PDF, aunque se vea igual la huella cambia y se procesa de nuevo.
- Si cambia el contenido de un PDF y mantiene el mismo nombre y ruta, no se envía a la IA, porque sus páginas ya están en SQL Server y solo se inserta. Hay que borrar sus filas o cambiar el nombre del archivo.
- Si el mismo PDF está en dos carpetas, en SQL Server queda una sola vez, con la ruta de la primera.
- `probar_gemini.py` de la carpeta original no funciona: importa `pdf_service`, que no existe.

## Comandos útiles

| Comando | Para qué |
|---|---|
| `python main.py` | Procesa los PDF con lo configurado en `.env` |
| `python main.py --ventana` | Abre la interfaz gráfica |
| `python main.py --destino csv` | Guarda solo en CSV, sin tocar `.env` |
| `python prompt.py` | Muestra el prompt completo que recibe la IA |
| `python importar.py --prueba` | Muestra qué se insertaría en SQL Server |
| `python importar.py` | Inserta en SQL Server lo ya procesado |

## Archivos del proyecto

| Archivo | Función |
|---|---|
| `.env` | Credenciales, rutas, nombres de archivos, parámetros y prompt (no se sube) |
| `.env.example` | Plantilla del `.env` |
| `main.py` | Punto de entrada, consola y ventana |
| `config.py` | Lee y valida el `.env` |
| `prompt.py` | Arma el prompt desde el `.env` |
| `procesamiento.py` | Recorre PDF y páginas, decide qué enviar, valida y guarda |
| `indice.py` | Índice de páginas ya procesadas por huella del PDF |
| `ia.py` | Conexión con la IA |
| `sql.py` | Consulta e inserta en SQL Server |
| `archivo_csv.py` | Arma el CSV |
| `importar.py` | Pasa lo ya procesado a SQL Server sin IA |
| `crear_tabla.sql` | DDL de la base y la tabla, se ejecuta a mano |
| `README.md` | Instrucciones de uso |
| `SEGUIMIENTO.md` | Este documento |
