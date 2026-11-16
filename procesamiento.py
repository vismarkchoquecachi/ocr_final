import hashlib
import json
import math
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Callable

import pymupdf

from ia import PATRON_EXCLUIDO, ErrorIA

TEXTOS = {"NombreOrdenante": 200, "INVOICE": 100, "NombreBeneficiario": 200, "BancoBeneficiario": 200}
OBLIGATORIOS = ("NombreOrdenante", "FechaTransferencia", "INVOICE", "Total", "NombreBeneficiario", "BancoBeneficiario")
VALORES_NULOS = {
    "", "-", "—", "n/a", "na", "none", "null", "desconocido", "ilegible", "no aplica",
    "no disponible", "no identificado", "no se identifica",
}
TITULO_DOCUMENTO = re.compile(
    r"^(orden(es)?|comprobante|constancia|solicitud|formulario|datos)\b.*\b(transferencia|ordenante|beneficiario|operaci[oó]n)"
    r"|^(ote|swift|mt\s?103)$|\(ote\)",
    re.IGNORECASE,
)
TOTAL_MAXIMO = Decimal("9999999999999999.99")
MAXIMO_PARCHES = 20_000
CARACTERES_INVALIDOS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
TIPO_ERROR = "ERROR_PROCESAMIENTO"


@dataclass
class Registro:
    ArchivoOrigen: str
    NumPagina: int
    NombreOrdenante: str | None = None
    FechaTransferencia: date | None = None
    INVOICE: str | None = None
    Total: Decimal | None = None
    NombreBeneficiario: str | None = None
    BancoBeneficiario: str | None = None
    Revisar: bool = True
    MotivoRevision: str | None = None
    JsonCrudo: dict = field(default_factory=dict)

    @property
    def es_error(self) -> bool:
        return self.JsonCrudo.get("tipo") == TIPO_ERROR

    def como_json(self) -> dict:
        datos = asdict(self)
        datos["FechaTransferencia"] = self.FechaTransferencia.isoformat() if self.FechaTransferencia else None
        datos["Total"] = str(self.Total) if self.Total is not None else None
        return datos


@dataclass
class Resumen:
    pdf: int = 0
    paginas: int = 0
    analizadas: int = 0
    omitidas: int = 0
    excluidas: int = 0
    guardadas: int = 0
    revisar: int = 0
    errores: int = 0

    def texto(self) -> str:
        return (
            f"PDF encontrados: {self.pdf}\n"
            f"Páginas totales: {self.paginas}\n"
            f"Páginas analizadas por IA: {self.analizadas}\n"
            f"Páginas omitidas (ya procesadas): {self.omitidas}\n"
            f"Páginas excluidas (OTE, en blanco o sin datos): {self.excluidas}\n"
            f"Registros guardados: {self.guardadas}\n"
            f"Registros para revisar: {self.revisar}\n"
            f"Errores: {self.errores}"
        )


def _texto(valor, maximo: int, campo: str, motivos: list[str]) -> str | None:
    if valor is None or isinstance(valor, (dict, list)):
        return None
    texto = " ".join(str(valor).split())
    if texto.casefold().strip(" .:") in VALORES_NULOS:
        return None
    if TITULO_DOCUMENTO.search(texto.strip(" .:")):
        motivos.append(f"{campo} contenía el título del documento y fue descartado")
        return None
    if len(texto) > maximo:
        motivos.append(f"{campo} excedía {maximo} caracteres y fue recortado")
        return texto[:maximo]
    return texto


def _fecha(valor, motivos: list[str]) -> date | None:
    if valor in (None, ""):
        return None
    texto = str(valor).strip()
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", texto):
            raise ValueError
        return date.fromisoformat(texto)
    except ValueError:
        motivos.append("FechaTransferencia no tiene el formato YYYY-MM-DD")
        return None


def convertir_importe(valor) -> Decimal:
    if isinstance(valor, bool):
        raise InvalidOperation
    if isinstance(valor, (int, float)):
        if not math.isfinite(valor):
            raise InvalidOperation
        return Decimal(str(valor))
    texto = re.sub(r"[^\d,.\-]", "", str(valor))
    if "," in texto and "." in texto:
        if texto.rfind(",") > texto.rfind("."):
            texto = texto.replace(".", "").replace(",", ".")
        else:
            texto = texto.replace(",", "")
    elif "," in texto:
        partes = texto.split(",")
        texto = texto.replace(",", "." if len(partes) == 2 and len(partes[1]) <= 2 else "")
    return Decimal(texto)


def _total(valor, motivos: list[str]) -> Decimal | None:
    if valor in (None, ""):
        return None
    try:
        total = convertir_importe(valor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        motivos.append("Total no es un número válido")
        return None
    if total < 0:
        motivos.append("Total no puede ser negativo")
        return None
    if total > TOTAL_MAXIMO:
        motivos.append("Total excede el límite de DECIMAL(18,2)")
        return None
    return total


def _crudo(respuesta: str | None) -> object:
    if not respuesta:
        return None
    try:
        return json.loads(respuesta)
    except json.JSONDecodeError:
        return respuesta


def crear_registro(origen: str, pagina: int, datos: dict, respuesta: str, auditoria: dict) -> Registro:
    motivos: list[str] = []
    valores = {campo: _texto(datos.get(campo), maximo, campo, motivos) for campo, maximo in TEXTOS.items()}
    valores["FechaTransferencia"] = _fecha(datos.get("FechaTransferencia"), motivos)
    valores["Total"] = _total(datos.get("Total"), motivos)

    faltantes = [campo for campo in OBLIGATORIOS if valores[campo] is None]
    if faltantes:
        motivos.append("No se identificó: " + ", ".join(faltantes))
    motivo_modelo = _texto(datos.get("MotivoRevision"), 1000, "MotivoRevision", [])
    if motivo_modelo:
        motivos.append(motivo_modelo)

    motivo = "; ".join(dict.fromkeys(motivos))[:1000] or None
    return Registro(
        ArchivoOrigen=origen[:500],
        NumPagina=pagina,
        Revisar=motivo is not None,
        MotivoRevision=motivo,
        JsonCrudo={**auditoria, "respuesta": _crudo(respuesta)},
        **valores,
    )


def crear_registro_error(origen: str, pagina: int, error: Exception, auditoria: dict) -> Registro:
    mensaje = f"{type(error).__name__}: {error}"
    return Registro(
        ArchivoOrigen=origen[:500],
        NumPagina=pagina,
        MotivoRevision=mensaje[:1000],
        JsonCrudo={
            **auditoria,
            "tipo": TIPO_ERROR,
            "error": mensaje,
            "respuesta": _crudo(getattr(error, "respuesta", None)),
        },
    )


def nombre_seguro(valor: str) -> str:
    texto = re.sub(r"\s+", "_", CARACTERES_INVALIDOS.sub("_", valor)).strip(" ._")
    return texto[:120] or "documento"


def buscar_pdfs(directorio: Path) -> list[Path]:
    if not directorio.is_dir():
        raise FileNotFoundError(f"La carpeta no existe: {directorio}")
    return sorted(ruta for ruta in directorio.rglob("*") if ruta.is_file() and ruta.suffix.lower() == ".pdf")


def sha256_archivo(ruta: Path) -> str:
    calculo = hashlib.sha256()
    with ruta.open("rb") as archivo:
        for bloque in iter(lambda: archivo.read(1 << 20), b""):
            calculo.update(bloque)
    return calculo.hexdigest()


def dpi_seguro(ancho: float, alto: float, dpi: int) -> int:
    def parches(valor: int) -> int:
        return math.ceil(math.ceil(ancho * valor / 72) / 32) * math.ceil(math.ceil(alto * valor / 72) / 32)

    if parches(dpi) <= MAXIMO_PARCHES:
        return dpi
    ajustado = int(dpi * math.sqrt(MAXIMO_PARCHES / parches(dpi)) * 0.98)
    while ajustado >= 72 and parches(ajustado) > MAXIMO_PARCHES:
        ajustado -= 1
    if ajustado < 72:
        raise ValueError("La página es demasiado grande para procesarla.")
    return ajustado


def _agregar_linea(ruta: Path, datos: dict) -> None:
    with ruta.open("a", encoding="utf-8") as archivo:
        archivo.write(json.dumps(datos, ensure_ascii=False, default=str) + "\n")


def normalizar(texto: str) -> str:
    sin_tildes = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    return " ".join(sin_tildes.casefold().split())


def es_documento_excluido(texto: str) -> bool:
    return bool(PATRON_EXCLUIDO.search(normalizar(texto)))


def consumo_respuesta(respuesta: str | None) -> dict | None:
    try:
        datos = json.loads(respuesta or "")
    except json.JSONDecodeError:
        return None
    if not isinstance(datos, dict):
        return None
    return datos.get("usageMetadata") or datos.get("usage")


def motivo_exclusion_previa(pagina) -> str | None:
    texto = pagina.get_text().strip()
    if not texto and not pagina.get_images() and not pagina.get_drawings():
        return "página en blanco"
    limite = pagina.rect.y0 + pagina.rect.height * 0.2
    encabezado = " ".join(bloque[4] for bloque in pagina.get_text("blocks") if bloque[1] < limite)
    if es_documento_excluido(encabezado[:500]):
        return "formulario ORDEN DE TRANSFERENCIAS AL EXTERIOR (OTE)"
    return None


class PaginasExcluidas:
    def __init__(self, salida: Path, nombre: str = "excluidos.jsonl"):
        self.ruta = salida / nombre
        self.claves: set[tuple[str, int, str]] = set()
        if not self.ruta.exists():
            return
        for linea in self.ruta.read_text(encoding="utf-8").splitlines():
            try:
                fila = json.loads(linea)
                self.claves.add((fila["ArchivoOrigen"], int(fila["NumPagina"]), fila["Hash"]))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue

    def contiene(self, origen: str, pagina: int, hash_archivo: str) -> bool:
        return (origen[:500], pagina, hash_archivo) in self.claves

    def agregar(self, origen: str, pagina: int, hash_archivo: str, motivo: str, detectado_por: str, consumo: dict | None = None) -> None:
        self.claves.add((origen[:500], pagina, hash_archivo))
        _agregar_linea(self.ruta, {
            "ArchivoOrigen": origen[:500],
            "NumPagina": pagina,
            "Hash": hash_archivo,
            "Motivo": motivo,
            "DetectadoPor": detectado_por,
            "Consumo": consumo,
            "FechaUTC": datetime.now(timezone.utc).isoformat(),
        })


class RepositorioLocal:
    def __init__(self, salida: Path, nombre: str = "resultados.jsonl"):
        self.claves: set[tuple[str, int, str]] = set()
        ruta = salida / nombre
        if not ruta.exists():
            return
        for linea in ruta.read_text(encoding="utf-8").splitlines():
            try:
                fila = json.loads(linea)
                crudo = fila.get("JsonCrudo") or {}
                if crudo.get("tipo") != TIPO_ERROR:
                    self.claves.add((fila["ArchivoOrigen"], int(fila["NumPagina"]), crudo.get("_archivo_hash_sha256", "")))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError):
                continue

    def existe(self, origen: str, pagina: int, hash_archivo: str) -> bool:
        return (origen[:500], pagina, hash_archivo) in self.claves

    def guardar(self, registro: Registro) -> None:
        if not registro.es_error:
            self.claves.add((registro.ArchivoOrigen, registro.NumPagina, registro.JsonCrudo.get("_archivo_hash_sha256", "")))


def procesar(
    entrada: Path,
    salida: Path,
    extractor,
    repositorio,
    dpi: int = 300,
    omitir_existentes: bool = True,
    concurrencia: int = 4,
    informar: Callable[[str], None] = print,
    archivo_resultados: str = "resultados.jsonl",
    archivo_excluidos: str = "excluidos.jsonl",
    archivo_errores: str = "errores.jsonl",
) -> Resumen:
    resumen = Resumen()
    salida.mkdir(parents=True, exist_ok=True)
    archivos = buscar_pdfs(entrada)
    resumen.pdf = len(archivos)
    if not archivos:
        informar(f"No se encontraron PDF en {entrada}")
        return resumen

    hoy = date.today()
    excluidas = PaginasExcluidas(salida, archivo_excluidos)
    modelo = getattr(extractor, "nombre", type(extractor).__name__)

    def guardar(registro: Registro, ruta_imagen: Path | None) -> None:
        datos = {**registro.como_json(), "ProcesadoUTC": datetime.now(timezone.utc).isoformat()}
        if ruta_imagen is not None:
            ruta_imagen.with_suffix(".json").write_text(
                json.dumps(datos, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
            )
        _agregar_linea(salida / archivo_resultados, datos)
        try:
            repositorio.guardar(registro)
        except Exception as error:
            resumen.errores += 1
            _agregar_linea(salida / archivo_errores, {
                "ArchivoOrigen": registro.ArchivoOrigen,
                "NumPagina": registro.NumPagina,
                "error": f"{type(error).__name__}: {error}",
                "fechaUTC": datetime.now(timezone.utc).isoformat(),
            })
            informar(f"  Página {registro.NumPagina}: error al guardar: {error}")
            return
        resumen.guardadas += 1
        resumen.revisar += registro.Revisar
        if registro.es_error:
            resumen.errores += 1
        estado = "ERROR" if registro.es_error else "REVISAR" if registro.Revisar else "OK"
        informar(f"  Página {registro.NumPagina}: {estado}" + (f" - {registro.MotivoRevision}" if registro.Revisar else ""))

    def excluir(origen: str, pagina: int, hash_archivo: str, motivo: str, detectado_por: str, consumo: dict | None = None) -> None:
        excluidas.agregar(origen, pagina, hash_archivo, motivo, detectado_por, consumo)
        resumen.excluidas += 1
        informar(f"  Página {pagina}: EXCLUIDA, no se guardan datos ({motivo})")

    def analizar(origen: str, pagina: int, ruta_imagen: Path, auditoria: dict) -> Registro | tuple[str, dict | None]:
        try:
            datos, respuesta = extractor.extraer(ruta_imagen)
            if datos.get("DocumentoExcluido") in (True, "true"):
                motivo = " ".join(str(datos.get("MotivoRevision") or "").split())[:300]
                return motivo or "sin documento de pago válido", consumo_respuesta(respuesta)
            return crear_registro(origen, pagina, datos, respuesta, auditoria)
        except ErrorIA as error:
            if error.fatal:
                raise
            return crear_registro_error(origen, pagina, error, auditoria)
        except Exception as error:
            return crear_registro_error(origen, pagina, error, auditoria)

    with ThreadPoolExecutor(max_workers=concurrencia) as ejecutor:
        for indice, ruta_pdf in enumerate(archivos, start=1):
            origen = str(ruta_pdf.resolve())
            informar(f"[{indice}/{len(archivos)}] {ruta_pdf.name}")
            try:
                hash_archivo = sha256_archivo(ruta_pdf)
                documento = pymupdf.open(ruta_pdf)
            except Exception as error:
                resumen.errores += 1
                informar(f"  No se pudo abrir: {error}")
                _agregar_linea(salida / archivo_errores, {"ArchivoOrigen": origen, "error": str(error)})
                continue

            auditoria = {"_archivo_hash_sha256": hash_archivo, "_modelo": modelo}
            carpeta = salida / f"{nombre_seguro(ruta_pdf.stem)}_{hoy:%Y%m%d}_{hashlib.sha256(origen.casefold().encode()).hexdigest()[:8]}"
            pendientes = {}

            with documento:
                if documento.needs_pass or documento.page_count == 0:
                    resumen.errores += 1
                    informar("  PDF protegido con contraseña o sin páginas.")
                    continue
                resumen.paginas += documento.page_count
                carpeta.mkdir(parents=True, exist_ok=True)

                for numero in range(1, documento.page_count + 1):
                    if excluidas.contiene(origen, numero, hash_archivo):
                        resumen.excluidas += 1
                        continue
                    if omitir_existentes and repositorio.existe(origen, numero, hash_archivo):
                        resumen.omitidas += 1
                        continue
                    try:
                        pagina = documento.load_page(numero - 1)
                        motivo = motivo_exclusion_previa(pagina)
                        if motivo:
                            excluir(origen, numero, hash_archivo, motivo, "análisis del PDF")
                            continue
                        ruta_imagen = carpeta / f"{nombre_seguro(ruta_pdf.stem)}_pagina_{numero:04d}_{hoy:%Y%m%d}.png"
                        dpi_real = dpi_seguro(pagina.rect.width, pagina.rect.height, dpi)
                        pagina.get_pixmap(dpi=dpi_real, alpha=False).save(str(ruta_imagen))
                    except Exception as error:
                        guardar(crear_registro_error(origen, numero, error, auditoria), None)
                        continue
                    resumen.analizadas += 1
                    pendientes[ejecutor.submit(analizar, origen, numero, ruta_imagen, auditoria)] = (numero, ruta_imagen)

            if not pendientes:
                informar("  Sin páginas nuevas por analizar.")
            for futuro in as_completed(pendientes):
                numero, ruta_imagen = pendientes[futuro]
                try:
                    registro = futuro.result()
                except ErrorIA as error:
                    ejecutor.shutdown(wait=False, cancel_futures=True)
                    raise ErrorIA(
                        f"La IA rechazó la solicitud. Revise la credencial y el modelo (ia_modelo) en .env. {error}",
                        error.respuesta,
                        error.codigo,
                    ) from error
                if isinstance(registro, tuple):
                    ruta_imagen.unlink(missing_ok=True)
                    excluir(origen, numero, hash_archivo, registro[0], "IA", registro[1])
                else:
                    guardar(registro, ruta_imagen)

    return resumen
