# guarda los resultados en un archivo csv, una fila por pagina y sin duplicados
# conserva las filas y columnas que ya estaban en el archivo
import csv
import io
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

COLUMNAS_CSV = (
    "Archivo", "NumPagina", "NombreOrdenante", "FechaTransferencia", "INVOICE", "Total",
    "NombreBeneficiario", "BancoBeneficiario", "Revisar", "MotivoRevision", "ProcesadoUTC", "ArchivoOrigen",
)
COLUMNAS_CLAVE = ("ArchivoOrigen", "NumPagina")
# nombres que se usan si no se indican otros en .env
NOMBRE_PREDETERMINADO = "resultados.csv"
HISTORIAL_PREDETERMINADO = "resultados.jsonl"
SEPARADOR = ";"

Clave = tuple[str, str]


class ResultadoCsv(NamedTuple):
    ruta: Path
    destino: Path
    nuevas: int
    actualizadas: int
    total: int

    def texto(self) -> str:
        detalle = f"{self.total} filas en total, {self.nuevas} nuevas, {self.actualizadas} actualizadas"
        if self.ruta == self.destino:
            return f"CSV guardado: {self.ruta} ({detalle})"
        return (
            f"El archivo {self.destino} está abierto en otro programa y no se pudo reemplazar. "
            f"Los datos se guardaron en {self.ruta} ({detalle}). "
            "Cierre el archivo y vuelva a ejecutar para actualizarlo."
        )


def resolver_ruta(ruta: Path | None, salida: Path, nombre: str = NOMBRE_PREDETERMINADO) -> Path:
    # sin ruta se guarda en la carpeta de salida, si la ruta es una carpeta se guarda dentro
    if ruta is None:
        return salida / nombre
    if ruta.is_dir() or not ruta.suffix:
        return ruta / nombre
    return ruta


def _clave(origen, pagina) -> Clave:
    return str(origen or "").strip().casefold(), str(pagina or "").strip()


def _orden(clave: Clave) -> tuple[str, int, str]:
    return clave[0], int(clave[1]) if clave[1].isdigit() else 0, clave[1]


def leer_historial(ruta: Path) -> dict[Clave, dict]:
    # ultimo resultado de cada pagina segun el historial
    filas: dict[Clave, dict] = {}
    if not ruta.exists():
        return filas
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        try:
            fila = json.loads(linea)
            filas[_clave(fila["ArchivoOrigen"], int(fila["NumPagina"]))] = fila
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
    return filas


def leer_csv(destino: Path) -> tuple[list[str], dict[Clave, dict]]:
    # columnas y filas que ya tiene el csv, rechaza archivos que no son de este programa
    if not destino.exists():
        return [], {}
    if destino.is_dir():
        raise ValueError(f"La ruta del CSV es una carpeta y no un archivo: {destino}")
    try:
        texto = destino.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        texto = destino.read_text(encoding="cp1252")
    if not texto.strip():
        return [], {}

    encabezado = texto.lstrip().splitlines()[0]
    separador = next(
        (s for s in (SEPARADOR, ",", "\t") if set(COLUMNAS_CLAVE) <= set(next(csv.reader([encabezado], delimiter=s)))),
        None,
    )
    if separador is None:
        raise ValueError(
            f"El archivo {destino} ya existe y no tiene el formato de este programa "
            f"(faltan las columnas {' y '.join(COLUMNAS_CLAVE)}). Para no dañarlo, elija otra ruta "
            "para el CSV o mueva ese archivo."
        )

    lector = csv.DictReader(io.StringIO(texto.lstrip(), newline=""), delimiter=separador)
    filas: dict[Clave, dict] = {}
    for indice, fila in enumerate(lector):
        fila.pop(None, None)
        fila = {columna: valor or "" for columna, valor in fila.items()}
        if not any(valor.strip() for valor in fila.values()):
            continue
        clave = _clave(fila["ArchivoOrigen"], fila["NumPagina"])
        filas[clave if clave[0] else ("", f"fila {indice}")] = fila
    return list(lector.fieldnames or []), filas


def comprobar_destino(destino: Path) -> None:
    # confirma que el csv se puede leer y escribir antes de gastar en ia
    try:
        destino.parent.mkdir(parents=True, exist_ok=True)
        leer_csv(destino)
        prueba = destino.with_name(f"{destino.name}.{os.getpid()}.prueba")
        prueba.write_bytes(b"")
        prueba.unlink()
    except OSError as error:
        raise ValueError(f"No se puede escribir el CSV en {destino}: {error}") from error


def _fila_csv(fila: dict) -> dict:
    valores = {**fila, "Archivo": Path(fila["ArchivoOrigen"]).name, "Revisar": "SI" if fila.get("Revisar") else "NO"}
    return {columna: "" if valores.get(columna) is None else str(valores[columna]) for columna in COLUMNAS_CSV}


def _reemplazar(temporal: Path, destino: Path) -> Path:
    for intento in range(3):
        try:
            os.replace(temporal, destino)
            return destino
        except PermissionError:
            if intento < 2:
                time.sleep(0.5)
    alternativo = destino.with_name(f"{destino.stem}_{datetime.now():%Y%m%d_%H%M%S}{destino.suffix}")
    os.replace(temporal, alternativo)
    return alternativo


def _escribir(destino: Path, columnas: list[str], filas: list[dict]) -> Path:
    destino.parent.mkdir(parents=True, exist_ok=True)
    temporal = destino.with_name(f"{destino.name}.{os.getpid()}.tmp")
    try:
        with temporal.open("w", encoding="utf-8-sig", newline="") as archivo:
            escritor = csv.DictWriter(archivo, fieldnames=columnas, delimiter=SEPARADOR, restval="", extrasaction="ignore")
            escritor.writeheader()
            escritor.writerows(filas)
        return _reemplazar(temporal, destino)
    finally:
        temporal.unlink(missing_ok=True)


def exportar_csv(
    salida: Path,
    destino: Path | None = None,
    archivo_resultados: str = HISTORIAL_PREDETERMINADO,
    nombre_csv: str = NOMBRE_PREDETERMINADO,
) -> ResultadoCsv | None:
    # integra en el csv los resultados del historial, devuelve none si aun no hay resultados
    historial = leer_historial(salida / archivo_resultados)
    if not historial:
        return None
    destino = resolver_ruta(destino, salida, nombre_csv)
    columnas, filas = leer_csv(destino)

    nuevas = actualizadas = 0
    for clave, fila in historial.items():
        existente = filas.get(clave)
        if existente is None:
            nuevas += 1
        elif str(fila.get("ProcesadoUTC") or "") > existente.get("ProcesadoUTC", ""):
            actualizadas += 1
        else:
            continue
        filas[clave] = {**(existente or {}), **_fila_csv(fila)}

    columnas = [*COLUMNAS_CSV, *(columna for columna in columnas if columna not in COLUMNAS_CSV)]
    ruta = _escribir(destino, columnas, [filas[clave] for clave in sorted(filas, key=_orden)])
    return ResultadoCsv(ruta, destino, nuevas, actualizadas, len(filas))
