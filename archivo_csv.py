# guarda los resultados en un archivo csv, una fila por pagina y sin duplicados
# conserva las filas y columnas que ya estaban en el archivo
import csv
import io
import json
import os
import time
from datetime import datetime
from pathlib import Path


# columnas del csv en el orden en que se escriben
COLUMNAS_CSV = (
    "Archivo",
    "NumPagina",
    "NombreOrdenante",
    "FechaTransferencia",
    "INVOICE",
    "Total",
    "NombreBeneficiario",
    "BancoBeneficiario",
    "Revisar",
    "MotivoRevision",
    "ProcesadoUTC",
    "ArchivoOrigen",
)

# columnas que identifican una pagina
COLUMNAS_CLAVE = ("ArchivoOrigen", "NumPagina")

# nombres que se usan si no se indican otros en .env
NOMBRE_PREDETERMINADO = "resultados.csv"
HISTORIAL_PREDETERMINADO = "resultados.jsonl"

# punto y coma para que excel lo abra bien en configuracion regional en español
SEPARADOR = ";"


class ResultadoCsv:
    # resumen de lo que se escribio en el csv

    def __init__(self, ruta, destino, nuevas, actualizadas, total):
        self.ruta = ruta
        self.destino = destino
        self.nuevas = nuevas
        self.actualizadas = actualizadas
        self.total = total

    def texto(self):
        detalle = f"{self.total} filas en total, {self.nuevas} nuevas, {self.actualizadas} actualizadas"

        if self.ruta == self.destino:
            return f"CSV guardado: {self.ruta} ({detalle})"

        return (
            f"El archivo {self.destino} está abierto en otro programa y no se pudo reemplazar. "
            f"Los datos se guardaron en {self.ruta} ({detalle}). "
            "Cierre el archivo y vuelva a ejecutar para actualizarlo."
        )


def resolver_ruta(ruta, salida, nombre=NOMBRE_PREDETERMINADO):
    # sin ruta se guarda en la carpeta de salida, si la ruta es una carpeta se guarda dentro
    if ruta is None:
        return salida / nombre

    if ruta.is_dir() or ruta.suffix == "":
        return ruta / nombre

    return ruta


def crear_clave(origen, pagina):
    # clave de una pagina, la ruta va en minusculas para que no importe como se escribio
    texto_origen = str(origen or "").strip().lower()
    texto_pagina = str(pagina or "").strip()
    return (texto_origen, texto_pagina)


def orden_de_fila(clave):
    # para ordenar las filas por archivo y despues por numero de pagina
    origen = clave[0]
    pagina = clave[1]

    numero = 0
    if pagina.isdigit():
        numero = int(pagina)

    return (origen, numero, pagina)


def leer_historial(ruta):
    # ultimo resultado de cada pagina segun el historial
    filas = {}

    if not ruta.exists():
        return filas

    for linea in ruta.read_text(encoding="utf-8").splitlines():
        try:
            fila = json.loads(linea)
            clave = crear_clave(fila["ArchivoOrigen"], int(fila["NumPagina"]))
            filas[clave] = fila
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue

    return filas


def detectar_separador(encabezado):
    # busca con que separador estan las columnas clave, devuelve none si no es un csv de este programa
    for separador in (SEPARADOR, ",", "\t"):
        columnas = next(csv.reader([encabezado], delimiter=separador))

        tiene_todas = True
        for columna in COLUMNAS_CLAVE:
            if columna not in columnas:
                tiene_todas = False

        if tiene_todas:
            return separador

    return None


def leer_csv(destino):
    # columnas y filas que ya tiene el csv, rechaza archivos que no son de este programa
    if not destino.exists():
        return [], {}

    if destino.is_dir():
        raise ValueError(f"La ruta del CSV es una carpeta y no un archivo: {destino}")

    try:
        texto = destino.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        texto = destino.read_text(encoding="cp1252")

    if texto.strip() == "":
        return [], {}

    texto = texto.lstrip()
    encabezado = texto.splitlines()[0]
    separador = detectar_separador(encabezado)

    if separador is None:
        raise ValueError(
            f"El archivo {destino} ya existe y no tiene el formato de este programa "
            f"(faltan las columnas {' y '.join(COLUMNAS_CLAVE)}). Para no dañarlo, elija otra ruta "
            "para el CSV o mueva ese archivo."
        )

    lector = csv.DictReader(io.StringIO(texto, newline=""), delimiter=separador)
    filas = {}
    indice = 0

    for fila_leida in lector:
        # quita las celdas sobrantes y cambia los vacios por texto vacio
        fila = {}
        hay_datos = False
        for columna in fila_leida:
            if columna is None:
                continue
            valor = fila_leida[columna] or ""
            fila[columna] = valor
            if valor.strip() != "":
                hay_datos = True

        if hay_datos:
            clave = crear_clave(fila["ArchivoOrigen"], fila["NumPagina"])

            # una fila sin archivo de origen se conserva igual, con una clave propia
            if clave[0] == "":
                clave = ("", f"fila {indice}")

            filas[clave] = fila

        indice = indice + 1

    columnas = list(lector.fieldnames or [])
    return columnas, filas


def comprobar_destino(destino):
    # confirma que el csv se puede leer y escribir antes de gastar en ia
    try:
        destino.parent.mkdir(parents=True, exist_ok=True)
        leer_csv(destino)

        prueba = destino.with_name(f"{destino.name}.{os.getpid()}.prueba")
        prueba.write_bytes(b"")
        prueba.unlink()

    except OSError as error:
        raise ValueError(f"No se puede escribir el CSV en {destino}: {error}") from error


def fila_para_csv(fila):
    # pasa una linea del historial al formato de columnas del csv
    valores = dict(fila)
    valores["Archivo"] = Path(fila["ArchivoOrigen"]).name

    if fila.get("Revisar"):
        valores["Revisar"] = "SI"
    else:
        valores["Revisar"] = "NO"

    resultado = {}
    for columna in COLUMNAS_CSV:
        valor = valores.get(columna)
        if valor is None:
            resultado[columna] = ""
        else:
            resultado[columna] = str(valor)

    return resultado


def reemplazar(temporal, destino):
    # reemplaza el csv, si esta abierto en excel lo intenta tres veces y despues guarda una copia
    for intento in range(3):
        try:
            os.replace(temporal, destino)
            return destino
        except PermissionError:
            if intento < 2:
                time.sleep(0.5)

    marca = datetime.now().strftime("%Y%m%d_%H%M%S")
    alternativo = destino.with_name(f"{destino.stem}_{marca}{destino.suffix}")
    os.replace(temporal, alternativo)
    return alternativo


def escribir(destino, columnas, filas):
    # escribe primero un archivo temporal y despues lo pone en lugar del csv, asi nunca queda a medias
    destino.parent.mkdir(parents=True, exist_ok=True)
    temporal = destino.with_name(f"{destino.name}.{os.getpid()}.tmp")

    try:
        with temporal.open("w", encoding="utf-8-sig", newline="") as archivo:
            escritor = csv.DictWriter(
                archivo,
                fieldnames=columnas,
                delimiter=SEPARADOR,
                restval="",
                extrasaction="ignore",
            )
            escritor.writeheader()
            escritor.writerows(filas)

        return reemplazar(temporal, destino)

    finally:
        temporal.unlink(missing_ok=True)


def exportar_csv(salida, destino=None, archivo_resultados=HISTORIAL_PREDETERMINADO, nombre_csv=NOMBRE_PREDETERMINADO):
    # integra en el csv los resultados del historial, devuelve none si aun no hay resultados
    historial = leer_historial(salida / archivo_resultados)

    if len(historial) == 0:
        return None

    destino = resolver_ruta(destino, salida, nombre_csv)
    columnas_existentes, filas = leer_csv(destino)

    nuevas = 0
    actualizadas = 0

    for clave in historial:
        fila = historial[clave]
        existente = filas.get(clave)

        if existente is None:
            # pagina nueva
            nuevas = nuevas + 1
            filas[clave] = fila_para_csv(fila)
            continue

        # solo se reemplaza si la pagina se proceso despues, asi se conservan las correcciones a mano
        procesado_historial = str(fila.get("ProcesadoUTC") or "")
        procesado_csv = existente.get("ProcesadoUTC", "")

        if procesado_historial > procesado_csv:
            actualizadas = actualizadas + 1
            combinada = dict(existente)
            combinada.update(fila_para_csv(fila))
            filas[clave] = combinada

    # columnas del programa primero y despues las que se hayan agregado a mano
    columnas = list(COLUMNAS_CSV)
    for columna in columnas_existentes:
        if columna not in columnas:
            columnas.append(columna)

    # filas ordenadas por archivo y pagina
    claves_ordenadas = sorted(filas, key=orden_de_fila)
    filas_ordenadas = []
    for clave in claves_ordenadas:
        filas_ordenadas.append(filas[clave])

    ruta = escribir(destino, columnas, filas_ordenadas)
    return ResultadoCsv(ruta, destino, nuevas, actualizadas, len(filas))
