import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

import prompt

BASE = Path(__file__).resolve().parent

# variable de la clave, modelo base y url base de cada proveedor
PROVEEDORES = {
    "openai": ("openai_clave", "gpt-6-astra", "https://api.openai.com/v1"),
    "anthropic": ("anthropic_clave", "claude-sonnet-5-5", "https://api.anthropic.com/v1"),
    "gemini": ("gemini_clave", "gemini-3-flash-preview", "https://generativelanguage.googleapis.com/v1beta"),
    "compatible": ("ia_clave", "", ""),
}

# donde se pueden guardar los resultados
DESTINOS = ("csv", "sql", "ambos")


@dataclass(frozen=True)
class Configuracion:
    proveedor: str
    modelo: str
    api_key: str
    url_base: str
    destino: str
    csv_ruta: Path | None
    sql_servidor: str
    sql_base_datos: str
    sql_usuario: str
    sql_clave: str
    entrada: Path
    salida: Path
    dpi: int
    concurrencia: int
    omitir_existentes: bool
    archivo_csv: str = "resultados.csv"
    archivo_resultados: str = "resultados.jsonl"
    archivo_excluidos: str = "excluidos.jsonl"
    archivo_errores: str = "errores.jsonl"
    tiempo_espera: int = 120
    reintentos: int = 5
    instrucciones: str = ""
    solicitud: str = ""

    @property
    def usa_csv(self) -> bool:
        return self.destino in ("csv", "ambos")

    @property
    def usa_sql(self) -> bool:
        return self.destino in ("sql", "ambos")

    @property
    def descripcion(self) -> str:
        destinos = []
        if self.usa_sql:
            destinos.append(f"SQL Server {self.sql_servidor or '(sin servidor)'}/{self.sql_base_datos}")
        if self.usa_csv:
            destinos.append("CSV")
        return (
            f"Proveedor: {self.proveedor or 'sin configurar'} | Modelo: {self.modelo or '-'} | "
            f"Destino: {' + '.join(destinos)}"
        )


def _texto(nombre: str, predeterminado: str = "") -> str:
    return os.getenv(nombre, "").strip() or predeterminado


def _ruta(nombre: str, predeterminada: str) -> Path:
    ruta = Path(_texto(nombre, predeterminada)).expanduser()
    return (ruta if ruta.is_absolute() else BASE / ruta).resolve()


def _archivo(nombre: str, predeterminado: str) -> str:
    texto = _texto(nombre, predeterminado)
    extension = Path(predeterminado).suffix
    if Path(texto).name != texto or Path(texto).suffix.lower() != extension:
        raise ValueError(f"{nombre} debe ser un nombre de archivo {extension}, sin carpetas.")
    return texto


def _entero(nombre: str, predeterminado: int, minimo: int, maximo: int) -> int:
    texto = _texto(nombre, str(predeterminado))
    if not texto.isdigit() or not minimo <= int(texto) <= maximo:
        raise ValueError(f"{nombre} debe ser un entero entre {minimo} y {maximo}.")
    return int(texto)


def _booleano(nombre: str, predeterminado: bool) -> bool:
    texto = _texto(nombre).lower()
    if not texto:
        return predeterminado
    if texto in {"1", "true", "si", "sí", "yes"}:
        return True
    if texto in {"0", "false", "no"}:
        return False
    raise ValueError(f"{nombre} debe ser true o false.")


def _detectar_proveedor() -> str:
    if _texto("ia_url_base"):
        return "compatible"
    for nombre, (variable, _, _) in PROVEEDORES.items():
        if nombre != "compatible" and _texto(variable):
            return nombre
    return ""


def cargar() -> Configuracion:
    load_dotenv(BASE / ".env", override=True, encoding="utf-8-sig")

    proveedor = _texto("ia_proveedor").lower() or _detectar_proveedor()
    if proveedor and proveedor not in PROVEEDORES:
        raise ValueError(f"ia_proveedor debe ser uno de: {', '.join(PROVEEDORES)}.")
    variable, modelo_base, url_base = PROVEEDORES.get(proveedor, ("", "", ""))

    # sin destino se guarda en ambos si hay servidor sql, si no solo en csv
    sql_servidor = _texto("sql_servidor")
    destino = _texto("destino").lower() or ("ambos" if sql_servidor else "csv")
    if destino not in DESTINOS:
        raise ValueError(f"destino debe ser uno de: {', '.join(DESTINOS)}.")

    archivos = {
        "archivo_csv": _archivo("archivo_csv", "resultados.csv"),
        "archivo_resultados": _archivo("archivo_resultados", "resultados.jsonl"),
        "archivo_excluidos": _archivo("archivo_excluidos", "excluidos.jsonl"),
        "archivo_errores": _archivo("archivo_errores", "errores.jsonl"),
    }
    if len({nombre.casefold() for nombre in archivos.values()}) < len(archivos):
        raise ValueError(f"Los nombres de {', '.join(archivos)} deben ser distintos entre sí.")

    instrucciones, solicitud = prompt.cargar()

    return Configuracion(
        proveedor=proveedor,
        modelo=_texto("ia_modelo", modelo_base),
        api_key=_texto("ia_clave") or (_texto(variable) if variable else ""),
        url_base=_texto("ia_url_base", url_base).rstrip("/"),
        destino=destino,
        csv_ruta=_ruta("csv_ruta", "") if _texto("csv_ruta") else None,
        sql_servidor=sql_servidor,
        sql_base_datos=_texto("sql_base_datos", "FacturasOCR"),
        sql_usuario=_texto("sql_usuario"),
        sql_clave=os.getenv("sql_clave", ""),
        entrada=_ruta("carpeta_pdf", "pdfs"),
        salida=_ruta("carpeta_salida", "salida"),
        dpi=_entero("imagen_dpi", 300, 72, 450),
        concurrencia=_entero("ia_concurrencia", 4, 1, 16),
        omitir_existentes=_booleano("omitir_existentes", True),
        tiempo_espera=_entero("ia_tiempo_espera", 120, 10, 600),
        reintentos=_entero("ia_reintentos", 5, 0, 10),
        instrucciones=instrucciones,
        solicitud=solicitud,
        **archivos,
    )


def validar(config: Configuracion) -> None:
    if not config.proveedor:
        raise ValueError("Falta la credencial de IA. Complete en .env una clave, por ejemplo gemini_clave.")
    if not config.modelo:
        raise ValueError("Falta ia_modelo en .env.")
    if not config.url_base:
        raise ValueError("Falta ia_url_base en .env para el proveedor compatible.")
    if not config.instrucciones or not config.solicitud:
        raise ValueError("Falta el prompt en .env. Copie las variables prompt_ desde .env.example.")
    if config.proveedor != "compatible" and not config.api_key:
        raise ValueError(f"Falta {PROVEEDORES[config.proveedor][0]} en .env.")
    if config.usa_sql and not config.sql_servidor:
        raise ValueError("Para guardar en SQL Server falta sql_servidor en .env (o elija el destino csv).")
    if config.usa_sql and config.sql_usuario and not config.sql_clave:
        raise ValueError("Falta sql_clave en .env para el usuario de SQL Server.")
