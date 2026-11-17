# recorre los pdf, decide que paginas enviar a la ia, revisa las respuestas y guarda los resultados
import hashlib
import json
import math
import re
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

import pymupdf

from ia import PATRON_EXCLUIDO, ErrorIA
from indice import IndicePaginas


# largo maximo de cada campo de texto, igual que en la tabla de sql server
LARGO_MAXIMO = {
    "NombreOrdenante": 200,
    "INVOICE": 100,
    "NombreBeneficiario": 200,
    "BancoBeneficiario": 200,
}

# campos que deben tener valor, si falta alguno la pagina queda para revisar
OBLIGATORIOS = (
    "NombreOrdenante",
    "FechaTransferencia",
    "INVOICE",
    "Total",
    "NombreBeneficiario",
    "BancoBeneficiario",
)

# textos que la ia a veces devuelve en lugar de dejar el campo vacio
VALORES_NULOS = (
    "", "-", "—", "n/a", "na", "none", "null", "desconocido", "ilegible", "no aplica",
    "no disponible", "no identificado", "no se identifica",
)

# titulos de documentos que no deben aparecer como valor de un campo
TITULO_DOCUMENTO = re.compile(
    r"^(orden(es)?|comprobante|constancia|solicitud|formulario|datos)\b.*\b(transferencia|ordenante|beneficiario|operaci[oó]n)"
    r"|^(ote|swift|mt\s?103)$|\(ote\)",
    re.IGNORECASE,
)

# mayor importe que cabe en decimal 18,2
TOTAL_MAXIMO = Decimal("9999999999999999.99")

# limite de tamano de imagen, en bloques de 32 pixeles
MAXIMO_PARCHES = 20_000

# caracteres que no se pueden usar en nombres de archivo de windows
CARACTERES_INVALIDOS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# marca que se pone en el json de los registros de error
TIPO_ERROR = "ERROR_PROCESAMIENTO"


class Registro:
    # datos de una pagina, listos para guardar en el csv y en sql server

    def __init__(
        self,
        ArchivoOrigen,
        NumPagina,
        NombreOrdenante=None,
        FechaTransferencia=None,
        INVOICE=None,
        Total=None,
        NombreBeneficiario=None,
        BancoBeneficiario=None,
        Revisar=True,
        MotivoRevision=None,
        JsonCrudo=None,
    ):
        self.ArchivoOrigen = ArchivoOrigen
        self.NumPagina = NumPagina
        self.NombreOrdenante = NombreOrdenante
        self.FechaTransferencia = FechaTransferencia
        self.INVOICE = INVOICE
        self.Total = Total
        self.NombreBeneficiario = NombreBeneficiario
        self.BancoBeneficiario = BancoBeneficiario
        self.Revisar = Revisar
        self.MotivoRevision = MotivoRevision

        if JsonCrudo is None:
            JsonCrudo = {}
        self.JsonCrudo = JsonCrudo

    @property
    def es_error(self):
        # true si este registro es de una pagina que fallo
        return self.JsonCrudo.get("tipo") == TIPO_ERROR

    def como_json(self):
        # convierte el registro en un diccionario que se puede escribir como json
        fecha = None
        if self.FechaTransferencia is not None:
            fecha = self.FechaTransferencia.isoformat()

        total = None
        if self.Total is not None:
            total = str(self.Total)

        return {
            "ArchivoOrigen": self.ArchivoOrigen,
            "NumPagina": self.NumPagina,
            "NombreOrdenante": self.NombreOrdenante,
            "FechaTransferencia": fecha,
            "INVOICE": self.INVOICE,
            "Total": total,
            "NombreBeneficiario": self.NombreBeneficiario,
            "BancoBeneficiario": self.BancoBeneficiario,
            "Revisar": self.Revisar,
            "MotivoRevision": self.MotivoRevision,
            "JsonCrudo": self.JsonCrudo,
        }


class Resumen:
    # contadores que se muestran al terminar

    def __init__(self):
        self.pdf = 0
        self.paginas = 0
        self.analizadas = 0
        self.omitidas = 0
        self.excluidas = 0
        self.guardadas = 0
        self.revisar = 0
        self.errores = 0

    def texto(self):
        lineas = [
            f"PDF encontrados: {self.pdf}",
            f"Páginas totales: {self.paginas}",
            f"Páginas analizadas por IA: {self.analizadas}",
            f"Páginas omitidas (ya procesadas): {self.omitidas}",
            f"Páginas excluidas (OTE, en blanco o sin datos): {self.excluidas}",
            f"Registros guardados: {self.guardadas}",
            f"Registros para revisar: {self.revisar}",
            f"Errores: {self.errores}",
        ]
        return "\n".join(lineas)


def limpiar_texto(valor, maximo, campo, motivos):
    # limpia un campo de texto, devuelve none si no es un valor real
    if valor is None or isinstance(valor, (dict, list)):
        return None

    # deja un solo espacio entre palabras
    texto = " ".join(str(valor).split())
    texto_corto = texto.strip(" .:")

    if texto_corto.lower() in VALORES_NULOS:
        return None

    if TITULO_DOCUMENTO.search(texto_corto):
        motivos.append(f"{campo} contenía el título del documento y fue descartado")
        return None

    if len(texto) > maximo:
        motivos.append(f"{campo} excedía {maximo} caracteres y fue recortado")
        return texto[:maximo]

    return texto


def leer_fecha(valor, motivos):
    # la fecha debe venir como año-mes-dia, por ejemplo 2024-03-15
    if valor is None or valor == "":
        return None

    texto = str(valor).strip()

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", texto) is None:
        motivos.append("FechaTransferencia no tiene el formato YYYY-MM-DD")
        return None

    try:
        return date.fromisoformat(texto)
    except ValueError:
        motivos.append("FechaTransferencia no tiene el formato YYYY-MM-DD")
        return None


def convertir_importe(valor):
    # convierte un importe en numero, entiende 1.234,56 y 1,234.56
    if isinstance(valor, bool):
        raise InvalidOperation

    if isinstance(valor, (int, float)):
        if not math.isfinite(valor):
            raise InvalidOperation
        return Decimal(str(valor))

    # deja solo digitos, comas, puntos y el signo menos
    texto = re.sub(r"[^\d,.\-]", "", str(valor))

    tiene_coma = "," in texto
    tiene_punto = "." in texto

    if tiene_coma and tiene_punto:
        # el separador que aparece al final es el de los decimales
        if texto.rfind(",") > texto.rfind("."):
            texto = texto.replace(".", "")
            texto = texto.replace(",", ".")
        else:
            texto = texto.replace(",", "")

    elif tiene_coma:
        # una sola coma con uno o dos digitos despues es decimal, si no es separador de miles
        partes = texto.split(",")
        if len(partes) == 2 and len(partes[1]) <= 2:
            texto = texto.replace(",", ".")
        else:
            texto = texto.replace(",", "")

    return Decimal(texto)


def leer_total(valor, motivos):
    # el total debe ser un numero positivo con dos decimales
    if valor is None or valor == "":
        return None

    try:
        total = convertir_importe(valor)
        total = total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
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


def leer_respuesta_cruda(respuesta):
    # guarda la respuesta de la ia como json si se puede, si no como texto
    if not respuesta:
        return None

    try:
        return json.loads(respuesta)
    except json.JSONDecodeError:
        return respuesta


def crear_registro(origen, pagina, datos, respuesta, auditoria):
    # revisa cada campo que devolvio la ia y arma el registro de la pagina
    motivos = []

    nombre_ordenante = limpiar_texto(datos.get("NombreOrdenante"), 200, "NombreOrdenante", motivos)
    invoice = limpiar_texto(datos.get("INVOICE"), 100, "INVOICE", motivos)
    nombre_beneficiario = limpiar_texto(datos.get("NombreBeneficiario"), 200, "NombreBeneficiario", motivos)
    banco_beneficiario = limpiar_texto(datos.get("BancoBeneficiario"), 200, "BancoBeneficiario", motivos)
    fecha = leer_fecha(datos.get("FechaTransferencia"), motivos)
    total = leer_total(datos.get("Total"), motivos)

    # anota los campos obligatorios que quedaron vacios
    valores = {
        "NombreOrdenante": nombre_ordenante,
        "FechaTransferencia": fecha,
        "INVOICE": invoice,
        "Total": total,
        "NombreBeneficiario": nombre_beneficiario,
        "BancoBeneficiario": banco_beneficiario,
    }
    faltantes = []
    for campo in OBLIGATORIOS:
        if valores[campo] is None:
            faltantes.append(campo)

    if len(faltantes) > 0:
        motivos.append("No se identificó: " + ", ".join(faltantes))

    # suma la duda que haya dejado la propia ia
    motivo_ia = limpiar_texto(datos.get("MotivoRevision"), 1000, "MotivoRevision", [])
    if motivo_ia:
        motivos.append(motivo_ia)

    # junta los motivos sin repetir
    motivos_unicos = []
    for motivo in motivos:
        if motivo not in motivos_unicos:
            motivos_unicos.append(motivo)

    motivo_final = "; ".join(motivos_unicos)[:1000]
    if motivo_final == "":
        motivo_final = None

    json_crudo = dict(auditoria)
    json_crudo["respuesta"] = leer_respuesta_cruda(respuesta)

    return Registro(
        ArchivoOrigen=origen[:500],
        NumPagina=pagina,
        NombreOrdenante=nombre_ordenante,
        FechaTransferencia=fecha,
        INVOICE=invoice,
        Total=total,
        NombreBeneficiario=nombre_beneficiario,
        BancoBeneficiario=banco_beneficiario,
        Revisar=motivo_final is not None,
        MotivoRevision=motivo_final,
        JsonCrudo=json_crudo,
    )


def crear_registro_error(origen, pagina, error, auditoria):
    # registro de una pagina que fallo, queda en el historial para saber que paso
    mensaje = f"{type(error).__name__}: {error}"

    json_crudo = dict(auditoria)
    json_crudo["tipo"] = TIPO_ERROR
    json_crudo["error"] = mensaje
    json_crudo["respuesta"] = leer_respuesta_cruda(getattr(error, "respuesta", None))

    return Registro(
        ArchivoOrigen=origen[:500],
        NumPagina=pagina,
        MotivoRevision=mensaje[:1000],
        JsonCrudo=json_crudo,
    )


def nombre_seguro(valor):
    # quita del nombre los caracteres que windows no permite
    texto = CARACTERES_INVALIDOS.sub("_", valor)
    texto = re.sub(r"\s+", "_", texto)
    texto = texto.strip(" ._")
    texto = texto[:120]

    if texto == "":
        return "documento"

    return texto


def buscar_pdfs(directorio):
    # lista todos los pdf de la carpeta y sus subcarpetas, en orden
    if not directorio.is_dir():
        raise FileNotFoundError(f"La carpeta no existe: {directorio}")

    encontrados = []
    for ruta in directorio.rglob("*"):
        if ruta.is_file() and ruta.suffix.lower() == ".pdf":
            encontrados.append(ruta)

    encontrados.sort()
    return encontrados


def sha256_archivo(ruta):
    # huella del contenido completo del pdf, si el archivo cambia la huella cambia
    calculo = hashlib.sha256()

    with ruta.open("rb") as archivo:
        while True:
            bloque = archivo.read(1024 * 1024)
            if not bloque:
                break
            calculo.update(bloque)

    return calculo.hexdigest()


def contar_parches(ancho, alto, dpi):
    # cuantos bloques de 32 pixeles tendria la imagen con esos dpi
    pixeles_ancho = math.ceil(ancho * dpi / 72)
    pixeles_alto = math.ceil(alto * dpi / 72)
    return math.ceil(pixeles_ancho / 32) * math.ceil(pixeles_alto / 32)


def dpi_seguro(ancho, alto, dpi):
    # baja los dpi si la pagina es tan grande que la imagen no seria aceptada
    if contar_parches(ancho, alto, dpi) <= MAXIMO_PARCHES:
        return dpi

    proporcion = MAXIMO_PARCHES / contar_parches(ancho, alto, dpi)
    ajustado = int(dpi * math.sqrt(proporcion) * 0.98)

    while ajustado >= 72 and contar_parches(ancho, alto, ajustado) > MAXIMO_PARCHES:
        ajustado = ajustado - 1

    if ajustado < 72:
        raise ValueError("La página es demasiado grande para procesarla.")

    return ajustado


def agregar_linea(ruta, datos):
    # agrega una linea json al final del archivo
    with ruta.open("a", encoding="utf-8") as archivo:
        archivo.write(json.dumps(datos, ensure_ascii=False, default=str) + "\n")


def ahora_utc():
    # fecha y hora actual en utc como texto
    return datetime.now(timezone.utc).isoformat()


def normalizar(texto):
    # quita tildes, pasa a minusculas y deja un solo espacio entre palabras
    sin_tildes = unicodedata.normalize("NFKD", texto)
    sin_tildes = sin_tildes.encode("ascii", "ignore").decode("ascii")
    return " ".join(sin_tildes.lower().split())


def es_documento_excluido(texto):
    # true si el texto tiene el titulo de un formulario excluido
    return PATRON_EXCLUIDO.search(normalizar(texto)) is not None


def consumo_respuesta(respuesta):
    # saca de la respuesta de la ia cuantos tokens se usaron
    try:
        datos = json.loads(respuesta or "")
    except json.JSONDecodeError:
        return None

    if not isinstance(datos, dict):
        return None

    if datos.get("usageMetadata"):
        return datos.get("usageMetadata")

    return datos.get("usage")


def motivo_exclusion_previa(pagina):
    # revisa la pagina sin llamar a la ia, devuelve el motivo si se excluye o none si hay que enviarla
    texto = pagina.get_text().strip()
    sin_imagenes = len(pagina.get_images()) == 0
    sin_dibujos = len(pagina.get_drawings()) == 0

    if texto == "" and sin_imagenes and sin_dibujos:
        return "página en blanco"

    # solo se mira el 20 por ciento superior, donde va el titulo del formulario
    limite = pagina.rect.y0 + pagina.rect.height * 0.2

    textos_encabezado = []
    for bloque in pagina.get_text("blocks"):
        posicion_arriba = bloque[1]
        texto_bloque = bloque[4]
        if posicion_arriba < limite:
            textos_encabezado.append(texto_bloque)

    encabezado = " ".join(textos_encabezado)

    if es_documento_excluido(encabezado[:500]):
        return "formulario ORDEN DE TRANSFERENCIAS AL EXTERIOR (OTE)"

    return None


def anotar_exclusion(ruta, origen, pagina, hash_archivo, motivo, detectado_por, consumo=None):
    # deja constancia de una pagina excluida con su motivo y los tokens que consumio
    agregar_linea(ruta, {
        "ArchivoOrigen": origen[:500],
        "NumPagina": pagina,
        "Hash": hash_archivo,
        "Motivo": motivo,
        "DetectadoPor": detectado_por,
        "Consumo": consumo,
        "FechaUTC": ahora_utc(),
    })


class RepositorioLocal:
    # paginas ya procesadas segun el historial, se usa cuando no se guarda en sql server

    def __init__(self, salida, nombre="resultados.jsonl"):
        self.claves = set()
        ruta = salida / nombre

        if not ruta.exists():
            return

        for linea in ruta.read_text(encoding="utf-8").splitlines():
            try:
                fila = json.loads(linea)
                crudo = fila.get("JsonCrudo") or {}

                # las paginas con error no cuentan como procesadas, asi se reintentan
                if crudo.get("tipo") == TIPO_ERROR:
                    continue

                clave = (fila["ArchivoOrigen"], int(fila["NumPagina"]), crudo.get("_archivo_hash_sha256", ""))
                self.claves.add(clave)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError):
                continue

    def existe(self, origen, pagina, hash_archivo):
        clave = (origen[:500], pagina, hash_archivo)
        return clave in self.claves

    def guardar(self, registro):
        if registro.es_error:
            return

        hash_archivo = registro.JsonCrudo.get("_archivo_hash_sha256", "")
        self.claves.add((registro.ArchivoOrigen, registro.NumPagina, hash_archivo))


class Proceso:
    # guarda todo lo que se necesita durante una ejecucion, asi las funciones no reciben diez parametros

    def __init__(
        self,
        salida,
        extractor,
        repositorio,
        informar,
        archivo_resultados,
        archivo_excluidos,
        archivo_errores,
        archivo_indice,
    ):
        self.salida = salida
        self.extractor = extractor
        self.repositorio = repositorio
        self.informar = informar
        self.ruta_resultados = salida / archivo_resultados
        self.ruta_excluidos = salida / archivo_excluidos
        self.ruta_errores = salida / archivo_errores
        self.resumen = Resumen()

        # indice que decide si una pagina ya se proceso, por huella del pdf y numero de pagina
        ruta_indice = salida / archivo_indice
        self.indice = IndicePaginas(ruta_indice)

        if not ruta_indice.exists():
            cantidad = self.indice.armar_desde_historial(self.ruta_resultados, self.ruta_excluidos)
            if cantidad > 0:
                informar(f"Se creó {archivo_indice} con {cantidad} páginas del historial.")

        # se activa cuando la ia rechaza la clave o el modelo, asi las demas paginas ya no se envian
        self.detener = threading.Event()

    def anotar_error(self, datos):
        datos["fechaUTC"] = ahora_utc()
        agregar_linea(self.ruta_errores, datos)

    def guardar(self, registro, ruta_imagen):
        # guarda una pagina, primero en archivos locales y despues en el repositorio
        datos = registro.como_json()
        datos["ProcesadoUTC"] = ahora_utc()

        # json junto a la imagen
        if ruta_imagen is not None:
            texto = json.dumps(datos, ensure_ascii=False, indent=2, default=str)
            ruta_imagen.with_suffix(".json").write_text(texto, encoding="utf-8")

        # historial, de aqui se arma el csv
        agregar_linea(self.ruta_resultados, datos)

        huella = registro.JsonCrudo.get("_archivo_hash_sha256", "")

        # sql server o memoria
        try:
            self.repositorio.guardar(registro)
        except Exception as error:
            # no se guardo en sql server, en el indice queda como error para reintentarla
            self.indice.marcar(huella, registro.NumPagina, "error", registro.ArchivoOrigen)
            self.resumen.errores = self.resumen.errores + 1
            self.anotar_error({
                "ArchivoOrigen": registro.ArchivoOrigen,
                "NumPagina": registro.NumPagina,
                "error": f"{type(error).__name__}: {error}",
            })
            self.informar(f"  Página {registro.NumPagina}: error al guardar: {error}")
            return

        # contadores y mensaje
        self.resumen.guardadas = self.resumen.guardadas + 1

        if registro.Revisar:
            self.resumen.revisar = self.resumen.revisar + 1

        if registro.es_error:
            self.resumen.errores = self.resumen.errores + 1
            estado = "ERROR"
        elif registro.Revisar:
            estado = "REVISAR"
        else:
            estado = "OK"

        # el indice recuerda como termino la pagina
        self.indice.marcar(huella, registro.NumPagina, estado.lower(), registro.ArchivoOrigen)

        mensaje = f"  Página {registro.NumPagina}: {estado}"
        if registro.Revisar:
            mensaje = mensaje + f" - {registro.MotivoRevision}"
        self.informar(mensaje)

    def excluir(self, origen, pagina, hash_archivo, motivo, detectado_por, consumo=None):
        # anota una pagina excluida, no se guardan sus datos
        anotar_exclusion(self.ruta_excluidos, origen, pagina, hash_archivo, motivo, detectado_por, consumo)
        self.indice.marcar(hash_archivo, pagina, "excluida", origen)
        self.resumen.excluidas = self.resumen.excluidas + 1
        self.informar(f"  Página {pagina}: EXCLUIDA, no se guardan datos ({motivo})")

    def analizar(self, origen, pagina, ruta_imagen, auditoria):
        # corre en un hilo aparte, envia la imagen a la ia
        # devuelve un registro, o un par motivo y consumo si la ia dijo que la pagina se excluye

        if self.detener.is_set():
            return None

        try:
            datos, respuesta = self.extractor.extraer(ruta_imagen)

        except ErrorIA as error:
            if error.fatal:
                self.detener.set()
                raise
            return crear_registro_error(origen, pagina, error, auditoria)

        except Exception as error:
            return crear_registro_error(origen, pagina, error, auditoria)

        try:
            excluida = datos.get("DocumentoExcluido")

            if excluida is True or excluida == "true":
                motivo = " ".join(str(datos.get("MotivoRevision") or "").split())[:300]
                if motivo == "":
                    motivo = "sin documento de pago válido"
                return (motivo, consumo_respuesta(respuesta))

            return crear_registro(origen, pagina, datos, respuesta, auditoria)

        except Exception as error:
            return crear_registro_error(origen, pagina, error, auditoria)

    def procesar_pdf(self, ruta_pdf, indice, total_pdf, ejecutor, dpi, omitir_existentes):
        # procesa todas las paginas de un pdf
        origen = str(ruta_pdf.resolve())
        self.informar(f"[{indice}/{total_pdf}] {ruta_pdf.name}")

        # huella del contenido y apertura del pdf
        try:
            hash_archivo = sha256_archivo(ruta_pdf)
            documento = pymupdf.open(ruta_pdf)
        except Exception as error:
            self.resumen.errores = self.resumen.errores + 1
            self.informar(f"  No se pudo abrir: {error}")
            self.anotar_error({"ArchivoOrigen": origen, "error": str(error)})
            return

        auditoria = {"_archivo_hash_sha256": hash_archivo, "_modelo": self.extractor.nombre}

        # carpeta de imagenes, el ultimo tramo sale de la ruta para que dos pdf con el mismo nombre no se pisen
        hoy = date.today()
        id_ruta = hashlib.sha256(origen.lower().encode()).hexdigest()[:8]
        nombre_pdf = nombre_seguro(ruta_pdf.stem)
        carpeta = self.salida / f"{nombre_pdf}_{hoy:%Y%m%d}_{id_ruta}"

        # paginas enviadas a la ia, cada una con su numero y su imagen
        pendientes = {}

        with documento:
            if documento.needs_pass or documento.page_count == 0:
                self.resumen.errores = self.resumen.errores + 1
                self.informar("  PDF protegido con contraseña o sin páginas.")
                return

            self.resumen.paginas = self.resumen.paginas + documento.page_count
            carpeta.mkdir(parents=True, exist_ok=True)

            for numero in range(1, documento.page_count + 1):
                if self.detener.is_set():
                    break

                # estado de la pagina en el indice, por huella del pdf y numero, sin importar la ruta
                estado = self.indice.estado(hash_archivo, numero)

                # filtro 1, ya fue excluida antes
                if estado == "excluida":
                    self.resumen.excluidas = self.resumen.excluidas + 1
                    continue

                # filtro 2, ya fue procesada antes
                if omitir_existentes and (estado == "ok" or estado == "revisar"):
                    self.resumen.omitidas = self.resumen.omitidas + 1
                    continue

                # filtro 2b, no esta en el indice pero ya esta guardada con esta ruta
                # pasa por ejemplo si se borro el indice, o si el pdf cambio y sus paginas siguen en sql server
                # como solo se inserta, enviarla a la ia costaria dinero y no se podria guardar
                if omitir_existentes and estado is None and self.repositorio.existe(origen, numero, hash_archivo):
                    self.resumen.omitidas = self.resumen.omitidas + 1
                    self.informar(
                        f"  Página {numero}: ya está guardada con esta ruta, se omite. "
                        "Si el PDF cambió, borre sus filas en SQL Server para procesarla de nuevo."
                    )
                    continue

                try:
                    pagina = documento.load_page(numero - 1)

                    # filtro 3, pagina en blanco o formulario ote con texto
                    motivo = motivo_exclusion_previa(pagina)
                    if motivo is not None:
                        self.excluir(origen, numero, hash_archivo, motivo, "análisis del PDF")
                        continue

                    # crea la imagen png de la pagina
                    ruta_imagen = carpeta / f"{nombre_pdf}_pagina_{numero:04d}_{hoy:%Y%m%d}.png"
                    dpi_real = dpi_seguro(pagina.rect.width, pagina.rect.height, dpi)
                    pagina.get_pixmap(dpi=dpi_real, alpha=False).save(str(ruta_imagen))

                except Exception as error:
                    self.guardar(crear_registro_error(origen, numero, error, auditoria), None)
                    continue

                # paso 4, se envia a la ia en un hilo aparte
                self.resumen.analizadas = self.resumen.analizadas + 1
                tarea = ejecutor.submit(self.analizar, origen, numero, ruta_imagen, auditoria)
                pendientes[tarea] = (numero, ruta_imagen)

        if len(pendientes) == 0:
            self.informar("  Sin páginas nuevas por analizar.")

        # espera las respuestas en el orden en que van llegando
        for tarea in as_completed(pendientes):
            numero = pendientes[tarea][0]
            ruta_imagen = pendientes[tarea][1]

            try:
                resultado = tarea.result()
            except ErrorIA as error:
                ejecutor.shutdown(wait=False, cancel_futures=True)
                raise ErrorIA(
                    f"La IA rechazó la solicitud. Revise la credencial y el modelo (ia_modelo) en .env. {error}",
                    error.respuesta,
                    error.codigo,
                ) from error

            # la tarea no se envio porque el proceso se estaba deteniendo
            if resultado is None:
                continue

            # la ia dijo que la pagina no tiene datos validos
            if isinstance(resultado, tuple):
                motivo = resultado[0]
                consumo = resultado[1]
                ruta_imagen.unlink(missing_ok=True)
                self.excluir(origen, numero, hash_archivo, motivo, "IA", consumo)
                continue

            self.guardar(resultado, ruta_imagen)


def procesar(
    entrada,
    salida,
    extractor,
    repositorio,
    dpi=300,
    omitir_existentes=True,
    concurrencia=4,
    informar=print,
    archivo_resultados="resultados.jsonl",
    archivo_excluidos="excluidos.jsonl",
    archivo_errores="errores.jsonl",
    archivo_indice="indice.json",
):
    # punto de entrada del proceso, recorre todos los pdf de la carpeta
    salida.mkdir(parents=True, exist_ok=True)

    proceso = Proceso(
        salida,
        extractor,
        repositorio,
        informar,
        archivo_resultados,
        archivo_excluidos,
        archivo_errores,
        archivo_indice,
    )

    archivos = buscar_pdfs(entrada)
    proceso.resumen.pdf = len(archivos)

    if len(archivos) == 0:
        informar(f"No se encontraron PDF en {entrada}")
        return proceso.resumen

    # grupo de hilos, cada hilo espera la respuesta de una pagina
    with ThreadPoolExecutor(max_workers=concurrencia) as ejecutor:
        indice = 1
        for ruta_pdf in archivos:
            proceso.procesar_pdf(ruta_pdf, indice, len(archivos), ejecutor, dpi, omitir_existentes)
            indice = indice + 1

    return proceso.resumen
