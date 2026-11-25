# recorre los pdf, decide que paginas enviar a la ia, revisa las respuestas y guarda los resultados
import hashlib
import json
import math
import os
import re
import threading
import time
import unicodedata
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
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
    r"|^(ote|swift|mt\s?103)$|\(ote\)"
    r"|^(proforma|commercial|factura)?\s*(invoice|factura|proforma)$",
    re.IGNORECASE,
)

# mayor importe que cabe en decimal 18,2
TOTAL_MAXIMO = Decimal("9999999999999999.99")

# limite de tamano de imagen, en bloques de 32 pixeles
MAXIMO_PARCHES = 20_000

# peso maximo de las imagenes de un solo envio a la ia, la api acepta hasta 32 mb por llamada
# las imagenes van en base64, que pesa un tercio mas, por eso se deja margen
LIMITE_BYTES_ENVIO = 20_000_000

# largo maximo del nombre del pdf en carpetas y archivos de salida
# la ruta se repite en la salida y windows no acepta rutas de mas de 260 caracteres
LARGO_NOMBRE = 60

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


def nombre_seguro(valor, maximo=LARGO_NOMBRE):
    # quita del nombre los caracteres que windows no permite y lo acorta
    texto = CARACTERES_INVALIDOS.sub("_", valor)
    texto = re.sub(r"\s+", "_", texto)
    texto = texto.strip(" ._")
    texto = texto[:maximo].strip(" ._")

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


def detectar_contenido(pagina, ajustes):
    # devuelve el rectangulo de la pagina que tiene algo escrito, o none si no se pudo decidir
    pixmap = pagina.get_pixmap(dpi=ajustes["dpi_deteccion"], colorspace=pymupdf.csGRAY, alpha=False)
    ancho = pixmap.width
    alto = pixmap.height
    salto = pixmap.stride
    muestras = pixmap.samples

    # el fondo es el gris mas comun de la pagina, asi sirve para papel blanco o grisaceo
    fondo = Counter(muestras[::5]).most_common(1)[0][0]

    # un pixel es contenido si se aleja del fondo mas que la diferencia
    diferencia = ajustes["diferencia"]
    tabla = bytes(1 if abs(valor - fondo) > diferencia else 0 for valor in range(256))
    binario = muestras.translate(tabla)

    # filas y columnas con suficientes pixeles de contenido, asi una mota suelta no cuenta
    minimo = ajustes["minimo_pixeles"]

    filas = []
    for fila in range(alto):
        inicio = fila * salto
        if binario[inicio:inicio + ancho].count(1) >= minimo:
            filas.append(fila)

    columnas = []
    for columna in range(ancho):
        if binario[columna::salto].count(1) >= minimo:
            columnas.append(columna)

    if len(filas) == 0 or len(columnas) == 0:
        return None

    # pasa de pixeles a puntos de la pagina, un punto es 1/72 de pulgada
    rectangulo = pagina.rect
    escala_x = rectangulo.width / ancho
    escala_y = rectangulo.height / alto

    x0 = rectangulo.x0 + columnas[0] * escala_x
    x1 = rectangulo.x0 + (columnas[-1] + 1) * escala_x
    y0 = rectangulo.y0 + filas[0] * escala_y
    y1 = rectangulo.y0 + (filas[-1] + 1) * escala_y

    # margen de seguridad para no cortar letras del borde
    margen = min(rectangulo.width, rectangulo.height) * ajustes["margen_porcentaje"] / 100
    contenido = pymupdf.Rect(x0 - margen, y0 - margen, x1 + margen, y1 + margen)

    # nunca sale de la pagina
    return contenido & rectangulo


def crear_imagen(pagina, ruta_imagen, dpi, recorte):
    # guarda la pagina como png, si hay ajustes de recorte quita los margenes en blanco
    zona = None
    if recorte is not None:
        try:
            zona = detectar_contenido(pagina, recorte)
        except Exception:
            # si la deteccion falla se envia la pagina completa
            zona = None

    if zona is None or zona.is_empty:
        zona = pagina.rect

    dpi_real = dpi_seguro(zona.width, zona.height, dpi)
    pixmap = pagina.get_pixmap(dpi=dpi_real, clip=zona, alpha=False)
    pixmap.save(str(ruta_imagen))

    # datos de la imagen que se guardan en las metricas de la respuesta
    area_pagina = pagina.rect.width * pagina.rect.height
    porcentaje = 100.0
    if area_pagina > 0:
        porcentaje = round(100 * zona.width * zona.height / area_pagina, 1)

    return {
        "ancho_px": pixmap.width,
        "alto_px": pixmap.height,
        "dpi": dpi_real,
        "recortada": zona != pagina.rect,
        "porcentaje_area": porcentaje,
        "bytes": ruta_imagen.stat().st_size,
    }


def destinos_pdf(archivos, entrada, salida):
    # carpeta de salida y nombre de cada pdf
    # la salida repite la ruta del pdf dentro de la entrada, por ejemplo
    # entrada/2026-10/2026-10-09/TRE00726053874/OTE.pdf -> salida/2026-10/2026-10-09/TRE00726053874/OTE/
    # si en una misma carpeta dos pdf quedan con el mismo nombre se agrega una marca de su ruta
    base = entrada.resolve()
    cantidad = Counter((ruta.parent, nombre_seguro(ruta.stem).lower()) for ruta in archivos)
    destinos = {}

    for ruta in archivos:
        nombre = nombre_seguro(ruta.stem)
        if cantidad[(ruta.parent, nombre.lower())] > 1:
            marca = hashlib.sha256(str(ruta.resolve()).lower().encode()).hexdigest()[:8]
            nombre = f"{nombre}_{marca}"

        relativa = ruta.resolve().parent.relative_to(base)
        destinos[ruta] = (salida / relativa / nombre, nombre)

    return destinos


def primer_valor(datos, *claves):
    # devuelve el valor de la primera clave que exista, cada proveedor usa nombres distintos
    for clave in claves:
        if datos.get(clave) is not None:
            return datos.get(clave)
    return None


def metricas_uso(respuesta, tokens_prompt=None):
    # pasa el consumo de tokens de gemini, anthropic u openai a los mismos nombres
    # tokens_prompt es el conteo del prompt sin imagen, lo usa anthropic para separar la imagen
    uso = consumo_respuesta(respuesta)
    if not isinstance(uso, dict):
        return {"tokens_prompt": None, "tokens_imagen": None, "tokens_entrada": None, "tokens_salida": None,
                "tokens_razonamiento": None, "tokens_total": None, "uso_proveedor": None}

    entrada = primer_valor(uso, "promptTokenCount", "input_tokens", "prompt_tokens")

    # gemini separa en su respuesta los tokens de texto y los de imagen
    tokens_imagen = None
    tokens_texto = None
    for detalle in uso.get("promptTokensDetails") or []:
        if detalle.get("modality") == "IMAGE":
            tokens_imagen = detalle.get("tokenCount")
        elif detalle.get("modality") == "TEXT":
            tokens_texto = detalle.get("tokenCount")

    # anthropic los manda sumados, la imagen es lo que queda al restar el prompt contado aparte
    if tokens_imagen is None and tokens_prompt is not None and entrada is not None and entrada >= tokens_prompt:
        tokens_texto = tokens_prompt
        tokens_imagen = entrada - tokens_prompt

    detalle_salida = uso.get("completion_tokens_details") or {}

    salida = primer_valor(uso, "candidatesTokenCount", "output_tokens", "completion_tokens")
    razonamiento = primer_valor(uso, "thoughtsTokenCount")
    if razonamiento is None:
        razonamiento = detalle_salida.get("reasoning_tokens")

    # anthropic no manda el total, se suma
    total = primer_valor(uso, "totalTokenCount", "total_tokens")
    if total is None and entrada is not None and salida is not None:
        total = entrada + salida

    return {
        "tokens_prompt": tokens_texto,
        "tokens_imagen": tokens_imagen,
        "tokens_entrada": entrada,
        "tokens_salida": salida,
        "tokens_razonamiento": razonamiento,
        "tokens_total": total,
        "uso_proveedor": uso,
    }


def dividir_en_lotes(paginas, maximo_paginas, maximo_bytes):
    # reparte las paginas de un pdf en envios, normalmente todas van en uno solo
    # se corta si se pasa del maximo de paginas o del peso total de las imagenes
    # cada pagina es un trio numero, ruta de la imagen e informacion de la imagen
    lotes = []
    actual = []
    peso = 0

    for pagina in paginas:
        tamano = pagina[2].get("bytes", 0)
        if len(actual) > 0 and (len(actual) >= maximo_paginas or peso + tamano > maximo_bytes):
            lotes.append(actual)
            actual = []
            peso = 0
        actual.append(pagina)
        peso = peso + tamano

    if len(actual) > 0:
        lotes.append(actual)

    return lotes


def separar_respuesta(datos, enviadas):
    # reparte la respuesta de la ia en documentos de pago y paginas excluidas
    # enviadas son los numeros de las paginas que se mandaron en esa llamada
    # devuelve documentos como trios primera pagina, paginas y campos, excluidas como pares pagina y motivo, y avisos
    enviadas = set(enviadas)
    documentos = []
    primeras = set()
    usadas = set()
    avisos = []

    for documento in datos.get("Documentos") or []:
        if not isinstance(documento, dict):
            continue

        # solo cuentan las paginas que de verdad se enviaron
        paginas = set()
        for numero in documento.get("Paginas") or []:
            if isinstance(numero, int) and not isinstance(numero, bool) and numero in enviadas:
                paginas.add(numero)
        paginas = sorted(paginas)

        if len(paginas) == 0:
            avisos.append("la IA devolvió un documento sin páginas válidas, no se guarda")
            continue

        # cada documento se guarda con su primera pagina libre, dos documentos no pueden usar la misma
        primera = None
        for numero in paginas:
            if numero not in primeras:
                primera = numero
                break

        if primera is None:
            avisos.append(f"la IA devolvió dos documentos en las mismas páginas {paginas}, se guarda solo el primero")
            continue

        campos = dict(documento)
        if primera != paginas[0]:
            nota = f"Comparte la página {paginas[0]} con otro documento."
            campos["MotivoRevision"] = " ".join(filter(None, [campos.get("MotivoRevision"), nota]))

        primeras.add(primera)
        usadas.update(paginas)
        documentos.append((primera, paginas, campos))

    # motivo de cada pagina sin documento, si la ia no la menciono se pone uno generico
    motivos = {}
    for excluida in datos.get("PaginasExcluidas") or []:
        if isinstance(excluida, dict) and isinstance(excluida.get("Pagina"), int):
            motivo = " ".join(str(excluida.get("Motivo") or "").split())[:300]
            motivos.setdefault(excluida["Pagina"], motivo)

    excluidas = []
    for numero in sorted(enviadas - usadas):
        excluidas.append((numero, motivos.get(numero) or "sin documento de pago según la IA"))

    return documentos, excluidas, avisos


def escribir_json(ruta, contenido):
    # escribe primero un temporal y despues lo reemplaza, asi quien lo lea nunca ve un archivo a medias
    temporal = ruta.with_name(ruta.name + ".tmp")
    texto = json.dumps(contenido, ensure_ascii=False, indent=2, default=str)
    temporal.write_text(texto, encoding="utf-8")
    os.replace(temporal, ruta)


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

        # ajustes del recorte de margenes, none si esta apagado
        self.recorte = None

        # nombre de las carpetas que se crean dentro de la carpeta de cada pdf
        # una con las imagenes listas para la ia y otra con sus respuestas, una por imagen
        self.carpeta_imagenes = "imagenes"
        self.carpeta_respuestas = "respuestas_ia"

        # paginas de un pdf que van juntas en una sola llamada a la ia
        self.paginas_por_envio = 20

    def anotar_error(self, datos):
        datos["fechaUTC"] = ahora_utc()
        agregar_linea(self.ruta_errores, datos)

    def guardar(self, registro):
        # guarda un registro, primero en archivos locales y despues en el repositorio
        # devuelve el estado con que quedo en el indice, ok, revisar o error
        datos = registro.como_json()
        datos["ProcesadoUTC"] = ahora_utc()

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
            return "error"

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

        paginas = registro.JsonCrudo.get("paginas") or [registro.NumPagina]
        if len(paginas) > 1:
            lista = ", ".join(str(numero) for numero in paginas)
            mensaje = f"  Páginas {lista}: {estado}"
        else:
            mensaje = f"  Página {registro.NumPagina}: {estado}"
        if registro.Revisar:
            mensaje = mensaje + f" - {registro.MotivoRevision}"
        self.informar(mensaje)

        return estado.lower()

    def excluir(self, origen, pagina, hash_archivo, motivo, detectado_por, consumo=None):
        # anota una pagina excluida, no se guardan sus datos
        anotar_exclusion(self.ruta_excluidos, origen, pagina, hash_archivo, motivo, detectado_por, consumo)
        self.indice.marcar(hash_archivo, pagina, "excluida", origen)
        self.resumen.excluidas = self.resumen.excluidas + 1
        self.informar(f"  Página {pagina}: EXCLUIDA, no se guardan datos ({motivo})")

    def guardar_respuesta(self, origen, auditoria, lote, ruta_respuesta, datos, respuesta, segundos, error):
        # deja la respuesta de la ia y sus metricas en un json por envio, otro programa lo pasa a sql server
        # normalmente hay un envio por pdf con todas sus paginas
        if error is not None:
            estado = "error"
        elif len(datos.get("Documentos") or []) == 0:
            estado = "excluida"
        else:
            estado = "extraida"

        # anthropic no separa prompt e imagenes, el extractor cuenta el prompt una vez para restarlo
        tokens_prompt = None
        if respuesta is not None and hasattr(self.extractor, "contar_tokens_prompt"):
            tokens_prompt = self.extractor.contar_tokens_prompt()

        metricas = metricas_uso(respuesta, tokens_prompt)
        metricas["segundos"] = round(segundos, 2)

        imagenes = []
        for numero, ruta_imagen, info_imagen in lote:
            imagen = {"pagina": numero, "archivo": ruta_imagen.name}
            imagen.update(info_imagen)
            imagenes.append(imagen)
        metricas["imagenes"] = imagenes

        mensaje_error = None
        if error is not None:
            mensaje_error = f"{type(error).__name__}: {error}"

        contenido = {
            "archivo_origen": origen[:500],
            "archivo": os.path.basename(origen),
            "paginas": [numero for numero, _, _ in lote],
            "huella_pdf": auditoria.get("_archivo_hash_sha256", ""),
            "modelo": auditoria.get("_modelo", ""),
            "fecha_utc": ahora_utc(),
            "estado": estado,
            "respuesta": datos,
            "error": mensaje_error,
            "metricas": metricas,
        }

        try:
            ruta_respuesta.parent.mkdir(parents=True, exist_ok=True)
            escribir_json(ruta_respuesta, contenido)
        except OSError as falla:
            self.anotar_error({"ArchivoOrigen": origen, "error": f"no se pudo escribir la respuesta: {falla}"})
            self.informar(f"  No se pudo escribir el json de la respuesta: {falla}")

    def analizar(self, origen, auditoria, lote, ruta_respuesta):
        # corre en un hilo aparte, envia todas las paginas del lote a la ia en una sola llamada
        # devuelve el par datos y respuesta, el error si fallo, o none si el proceso se estaba deteniendo
        if self.detener.is_set():
            return None

        inicio = time.monotonic()
        paginas = [(numero, ruta_imagen) for numero, ruta_imagen, _ in lote]

        try:
            datos, respuesta = self.extractor.extraer(paginas)

        except ErrorIA as error:
            if error.fatal:
                self.detener.set()
                raise
            segundos = time.monotonic() - inicio
            self.guardar_respuesta(origen, auditoria, lote, ruta_respuesta, None, error.respuesta, segundos, error)
            return error

        except Exception as error:
            segundos = time.monotonic() - inicio
            self.guardar_respuesta(origen, auditoria, lote, ruta_respuesta, None, None, segundos, error)
            return error

        segundos = time.monotonic() - inicio
        self.guardar_respuesta(origen, auditoria, lote, ruta_respuesta, datos, respuesta, segundos, None)
        return (datos, respuesta)

    def recibir(self, tarea, contexto, ejecutor):
        # toma el resultado de una llamada a la ia y lo guarda, si la clave o el modelo fallan se detiene todo
        try:
            resultado = tarea.result()
        except ErrorIA as error:
            ejecutor.shutdown(wait=False, cancel_futures=True)
            raise ErrorIA(
                f"La IA rechazó la solicitud. Revise la credencial y el modelo (ia_modelo) en .env. {error}",
                error.respuesta,
                error.codigo,
            ) from error

        self.registrar_lote(contexto, resultado)

    def registrar_lote(self, contexto, resultado):
        # guarda lo que devolvio la ia para las paginas de un envio
        origen, hash_archivo, auditoria, lote, ruta_visible = contexto
        numeros = [numero for numero, _, _ in lote]

        # la llamada no se hizo porque el proceso se estaba deteniendo
        if resultado is None:
            return

        self.informar(f"Respuesta de la IA para {ruta_visible}:")

        # la llamada fallo, todas sus paginas quedan con error y se reintentan en la siguiente ejecucion
        if isinstance(resultado, Exception):
            for numero in numeros:
                self.guardar(crear_registro_error(origen, numero, resultado, auditoria))
            return

        datos, respuesta = resultado

        try:
            documentos, excluidas, avisos = separar_respuesta(datos, numeros)
        except Exception as error:
            for numero in numeros:
                self.guardar(crear_registro_error(origen, numero, error, auditoria))
            return

        for aviso in avisos:
            self.anotar_error({"ArchivoOrigen": origen, "error": aviso})
            self.informar(f"  Aviso: {aviso}")

        # un registro por documento de pago, se guarda con su primera pagina
        for primera, paginas, campos in documentos:
            try:
                registro = crear_registro(origen, primera, campos, respuesta, auditoria)
                registro.JsonCrudo["paginas"] = paginas
            except Exception as error:
                registro = crear_registro_error(origen, primera, error, auditoria)

            estado = self.guardar(registro)

            # las demas paginas del documento quedan en el indice con el mismo estado
            for numero in paginas:
                if numero != primera:
                    self.indice.marcar(hash_archivo, numero, estado, origen)

        # las paginas sin documento de pago, las imagenes se conservan
        for numero, motivo in excluidas:
            self.excluir(origen, numero, hash_archivo, motivo, "IA")

    def procesar_pdf(self, ruta_pdf, carpeta_pdf, nombre_pdf, indice, total_pdf, ejecutor, dpi, omitir_existentes):
        # prepara las imagenes de un pdf y envia todas sus paginas a la ia en una sola llamada
        # si son muchas paginas se envian en partes, devuelve las tareas enviadas con lo necesario para guardarlas
        origen = str(ruta_pdf.resolve())
        # se muestra la ruta de año, mes, dia y codigo, el nombre del pdf solo se repite entre carpetas
        try:
            ruta_visible = carpeta_pdf.parent.relative_to(self.salida) / ruta_pdf.name
        except ValueError:
            ruta_visible = ruta_pdf.name
        self.informar(f"[{indice}/{total_pdf}] {ruta_visible}")

        # huella del contenido y apertura del pdf
        try:
            hash_archivo = sha256_archivo(ruta_pdf)
            documento = pymupdf.open(ruta_pdf)
        except Exception as error:
            self.resumen.errores = self.resumen.errores + 1
            self.informar(f"  No se pudo abrir: {error}")
            self.anotar_error({"ArchivoOrigen": origen, "error": str(error)})
            return []

        auditoria = {"_archivo_hash_sha256": hash_archivo, "_modelo": self.extractor.nombre}

        # las imagenes van a la carpeta del pdf, que repite su ruta de año, mes, dia y codigo
        carpeta = carpeta_pdf / self.carpeta_imagenes

        # paginas que se envian a la ia, cada una con su numero, su imagen y los datos de la imagen
        para_enviar = []

        with documento:
            if documento.needs_pass or documento.page_count == 0:
                self.resumen.errores = self.resumen.errores + 1
                self.informar("  PDF protegido con contraseña o sin páginas.")
                return []

            self.resumen.paginas = self.resumen.paginas + documento.page_count

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

                # filtro 2b, no esta terminada en el indice pero ya esta guardada con esta ruta
                # pasa por ejemplo si se borro el indice, si se cargo con cargar_sql.py despues de un error,
                # o si el pdf cambio y sus paginas siguen en sql server
                # como solo se inserta, enviarla a la ia costaria dinero y no se podria guardar
                sin_terminar = estado is None or estado == "error"
                if omitir_existentes and sin_terminar and self.repositorio.existe(origen, numero, hash_archivo):
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

                    # crea la imagen png de la pagina, el nombre es solo el del pdf y el numero de pagina
                    # la carpeta se crea recien aqui, asi un pdf sin paginas para la ia no deja carpetas vacias
                    carpeta.mkdir(parents=True, exist_ok=True)
                    ruta_imagen = carpeta / f"{nombre_pdf}_pagina_{numero}.png"
                    # se recortan los margenes en blanco para enviar menos pixeles
                    info_imagen = crear_imagen(pagina, ruta_imagen, dpi, self.recorte)

                except Exception as error:
                    self.guardar(crear_registro_error(origen, numero, error, auditoria))
                    continue

                para_enviar.append((numero, ruta_imagen, info_imagen))

        if len(para_enviar) == 0:
            self.informar("  Sin páginas nuevas por analizar.")
            return []

        self.resumen.analizadas = self.resumen.analizadas + len(para_enviar)

        # todas las paginas van juntas con un solo prompt, salvo que sean demasiadas o pesen mucho
        lotes = dividir_en_lotes(para_enviar, self.paginas_por_envio, LIMITE_BYTES_ENVIO)
        tareas = []

        for parte, lote in enumerate(lotes, start=1):
            nombre = nombre_pdf
            detalle = ""
            if len(lotes) > 1:
                nombre = f"{nombre_pdf}_parte_{parte}"
                detalle = f", parte {parte} de {len(lotes)}"

            # el json de la respuesta va en la carpeta del pdf, al lado de la carpeta de imagenes
            ruta_respuesta = carpeta_pdf / self.carpeta_respuestas / f"{nombre}.json"
            paginas_texto = ", ".join(str(numero) for numero, _, _ in lote)
            self.informar(f"  Enviando a la IA en una sola llamada las páginas {paginas_texto}{detalle}")

            tarea = ejecutor.submit(self.analizar, origen, auditoria, lote, ruta_respuesta)
            tareas.append((tarea, (origen, hash_archivo, auditoria, lote, ruta_visible)))

        return tareas


def procesar(
    entrada,
    salida,
    extractor,
    repositorio,
    dpi=200,
    omitir_existentes=True,
    concurrencia=4,
    informar=print,
    archivo_resultados="resultados.jsonl",
    archivo_excluidos="excluidos.jsonl",
    archivo_errores="errores.jsonl",
    archivo_indice="indice.json",
    recorte=None,
    carpeta_imagenes="imagenes",
    carpeta_respuestas="respuestas_ia",
    paginas_por_envio=20,
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

    proceso.recorte = recorte
    proceso.carpeta_imagenes = carpeta_imagenes
    proceso.carpeta_respuestas = carpeta_respuestas
    proceso.paginas_por_envio = paginas_por_envio

    # busca los pdf en todas las subcarpetas, por ejemplo año-mes/año-mes-dia/codigo
    # solo se toman los pdf, los demas archivos de esas carpetas se ignoran
    archivos = buscar_pdfs(entrada)
    proceso.resumen.pdf = len(archivos)

    if len(archivos) == 0:
        informar(f"No se encontraron PDF en {entrada}")
        return proceso.resumen

    destinos = destinos_pdf(archivos, entrada, salida)

    # llamadas enviadas a la ia que todavia no se guardaron, cada una con lo necesario para guardarla
    pendientes = {}

    # grupo de hilos, cada hilo espera la respuesta de un pdf, asi varios pdf se analizan a la vez
    with ThreadPoolExecutor(max_workers=concurrencia) as ejecutor:
        indice = 1
        for ruta_pdf in archivos:
            if proceso.detener.is_set():
                break

            carpeta_pdf, nombre_pdf = destinos[ruta_pdf]
            tareas = proceso.procesar_pdf(ruta_pdf, carpeta_pdf, nombre_pdf, indice, len(archivos), ejecutor, dpi, omitir_existentes)
            for tarea, contexto in tareas:
                pendientes[tarea] = contexto
            indice = indice + 1

            # no se preparan mas pdf mientras haya muchas llamadas esperando
            # asi un error de clave o de modelo se ve enseguida y no se crean imagenes de mas
            while len(pendientes) >= concurrencia * 2:
                listas, _ = wait(list(pendientes), return_when=FIRST_COMPLETED)
                for tarea in listas:
                    proceso.recibir(tarea, pendientes.pop(tarea), ejecutor)

        # guarda las respuestas que faltan, en el orden en que van llegando
        for tarea in as_completed(list(pendientes)):
            proceso.recibir(tarea, pendientes.pop(tarea), ejecutor)

    return proceso.resumen
