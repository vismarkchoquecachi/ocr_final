# envia cada imagen a la ia y devuelve los datos que extrajo
# funciona con gemini, openai, anthropic y servicios compatibles con openai
import base64
import json
import re
import time
import urllib.error
import urllib.request

import pymupdf


# titulo de los formularios que se excluyen sin llamar a la ia
PATRON_EXCLUIDO = re.compile(r"orden(es)? de transferencias? al exterior|\(ote\)")

# campos que la ia debe devolver siempre
CAMPOS = (
    "DocumentoExcluido",
    "NombreOrdenante",
    "FechaTransferencia",
    "INVOICE",
    "Total",
    "NombreBeneficiario",
    "BancoBeneficiario",
    "MotivoRevision",
)

# nombre de la herramienta que se le pide usar a anthropic
HERRAMIENTA = "registrar_comprobante"

# peso maximo de la imagen en bytes, anthropic acepta menos que los demas
LIMITE_IMAGEN_ANTHROPIC = 3_700_000
LIMITE_IMAGEN_GENERAL = 15_000_000

# codigos http que indican un problema pasajero, se vuelve a intentar
CODIGOS_REINTENTO = (408, 409, 425, 429, 500, 502, 503, 504, 529)

# codigos http que indican clave o modelo incorrectos, se detiene todo
CODIGOS_FATALES = (401, 403, 404)


def crear_esquema():
    # describe el json que debe devolver la ia, tipo de cada campo y que todos son obligatorios
    propiedades = {}

    for campo in CAMPOS:
        if campo == "DocumentoExcluido":
            propiedades[campo] = {"type": "boolean"}
        elif campo == "Total":
            propiedades[campo] = {"anyOf": [{"type": "number"}, {"type": "null"}]}
        else:
            propiedades[campo] = {"anyOf": [{"type": "string"}, {"type": "null"}]}

    return {
        "type": "object",
        "properties": propiedades,
        "required": list(CAMPOS),
        "additionalProperties": False,
    }


ESQUEMA = crear_esquema()


class ErrorIA(RuntimeError):
    # error al hablar con la ia, guarda la respuesta original y el codigo http

    def __init__(self, mensaje, respuesta="", codigo=None):
        super().__init__(mensaje)
        self.respuesta = respuesta
        self.codigo = codigo

    @property
    def fatal(self):
        # true si el error es de clave o modelo, en ese caso no tiene sentido seguir
        return self.codigo in CODIGOS_FATALES


def mensaje_error(cuerpo):
    # saca el mensaje de error de la respuesta del servidor, corto y en una sola linea
    mensaje = cuerpo

    try:
        datos = json.loads(cuerpo)

        if isinstance(datos, list):
            datos = datos[0]

        error = datos.get("error", {})

        if isinstance(error, dict):
            mensaje = error.get("message", "")
        else:
            mensaje = str(error)

    except (json.JSONDecodeError, AttributeError, IndexError):
        mensaje = cuerpo

    mensaje = " ".join(str(mensaje).split())
    return mensaje[:300]


def preparar_imagen(ruta, limite):
    # devuelve los bytes de la imagen y su tipo, si pesa mas del limite la pasa a jpeg y la achica
    datos = ruta.read_bytes()

    if len(datos) <= limite:
        return datos, "image/png"

    pixmap = pymupdf.Pixmap(str(ruta))

    while True:
        datos = pixmap.tobytes("jpeg", jpg_quality=90)

        if len(datos) <= limite or pixmap.width < 800:
            return datos, "image/jpeg"

        # reduce la imagen a la mitad y vuelve a probar
        pixmap.shrink(1)


def extraer_json(texto):
    # busca el primer objeto json dentro del texto, la ia a veces agrega texto alrededor
    if texto is None:
        texto = ""

    coincidencia = re.search(r"\{.*\}", texto, re.DOTALL)

    if coincidencia is None:
        raise ValueError("la respuesta no contiene un objeto JSON")

    datos = json.loads(coincidencia.group(0))

    if not isinstance(datos, dict):
        raise ValueError("la respuesta no es un objeto JSON")

    return datos


class ExtractorIA:
    # se encarga de enviar una imagen a la ia y devolver los datos

    def __init__(
        self,
        proveedor,
        modelo,
        api_key="",
        url_base="",
        timeout=120,
        reintentos=5,
        instrucciones="",
        solicitud="",
    ):
        if proveedor not in ("openai", "anthropic", "gemini", "compatible"):
            raise ValueError(f"Proveedor no soportado: {proveedor}")

        if instrucciones.strip() == "" or solicitud.strip() == "":
            raise ValueError("Falta el prompt. Revise las variables prompt_ en .env.")

        self.proveedor = proveedor
        self.modelo = modelo
        self.api_key = api_key
        self.url_base = url_base.rstrip("/")
        self.timeout = timeout
        self.reintentos = reintentos
        self.instrucciones = instrucciones
        self.solicitud = solicitud

    @property
    def nombre(self):
        # nombre que se muestra en los mensajes, por ejemplo gemini/gemini-3-flash-preview
        return f"{self.proveedor}/{self.modelo}"

    def extraer(self, ruta_imagen):
        # paso 1, preparar la imagen
        if self.proveedor == "anthropic":
            limite = LIMITE_IMAGEN_ANTHROPIC
        else:
            limite = LIMITE_IMAGEN_GENERAL

        imagen, tipo = preparar_imagen(ruta_imagen, limite)
        imagen64 = base64.b64encode(imagen).decode("ascii")

        # paso 2, enviarla al proveedor que corresponde
        if self.proveedor == "anthropic":
            crudo = self.llamar_anthropic(imagen64, tipo)
        elif self.proveedor == "gemini":
            crudo = self.llamar_gemini(imagen64, tipo)
        else:
            crudo = self.llamar_openai(imagen64, tipo)

        # paso 3, sacar los datos de la respuesta
        try:
            respuesta = json.loads(crudo)
            datos = self.interpretar(respuesta)
        except ErrorIA as error:
            error.respuesta = crudo
            raise
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise ErrorIA(f"Respuesta no interpretable de {self.nombre}: {error}", crudo) from error

        return datos, crudo

    def llamar_openai(self, imagen64, tipo):
        # formato de openai, tambien lo usan ollama, openrouter, groq, deepseek y azure
        encabezados = {}
        if self.api_key != "":
            encabezados["Authorization"] = f"Bearer {self.api_key}"
        if "azure" in self.url_base:
            encabezados["api-key"] = self.api_key

        mensaje_sistema = {"role": "system", "content": self.instrucciones}

        mensaje_usuario = {
            "role": "user",
            "content": [
                {"type": "text", "text": self.solicitud},
                {"type": "image_url", "image_url": {"url": f"data:{tipo};base64,{imagen64}", "detail": "high"}},
            ],
        }

        cuerpo = {
            "model": self.modelo,
            "messages": [mensaje_sistema, mensaje_usuario],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "comprobante", "strict": True, "schema": ESQUEMA},
            },
        }

        url = f"{self.url_base}/chat/completions"

        try:
            return self.enviar(url, encabezados, cuerpo)
        except ErrorIA as error:
            # algunos servicios compatibles no aceptan el esquema, se repite pidiendo json simple
            if self.proveedor == "compatible" and error.codigo in (400, 422):
                cuerpo["response_format"] = {"type": "json_object"}
                return self.enviar(url, encabezados, cuerpo)
            raise

    def llamar_anthropic(self, imagen64, tipo):
        # formato de anthropic, se obliga a la ia a usar una herramienta con el esquema
        encabezados = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}

        herramienta = {
            "name": HERRAMIENTA,
            "description": "Registra los datos extraídos del comprobante.",
            "input_schema": ESQUEMA,
        }

        mensaje_usuario = {
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": tipo, "data": imagen64}},
                {"type": "text", "text": self.solicitud},
            ],
        }

        cuerpo = {
            "model": self.modelo,
            "max_tokens": 2048,
            "system": self.instrucciones,
            "tools": [herramienta],
            "tool_choice": {"type": "tool", "name": HERRAMIENTA},
            "messages": [mensaje_usuario],
        }

        url = f"{self.url_base}/messages"
        return self.enviar(url, encabezados, cuerpo)

    def llamar_gemini(self, imagen64, tipo):
        # formato de gemini, la respuesta se pide en json con el esquema
        encabezados = {"x-goog-api-key": self.api_key}

        mensaje_usuario = {
            "role": "user",
            "parts": [
                {"text": self.solicitud},
                {"inline_data": {"mime_type": tipo, "data": imagen64}},
            ],
        }

        cuerpo = {
            "systemInstruction": {"parts": [{"text": self.instrucciones}]},
            "contents": [mensaje_usuario],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseJsonSchema": ESQUEMA,
            },
        }

        url = f"{self.url_base}/models/{self.modelo}:generateContent"
        return self.enviar(url, encabezados, cuerpo)

    def interpretar(self, respuesta):
        # cada proveedor devuelve los datos en un lugar distinto

        if self.proveedor == "anthropic":
            for bloque in respuesta.get("content", []):
                if bloque.get("type") == "tool_use":
                    return bloque["input"]

            motivo = respuesta.get("stop_reason")
            raise ErrorIA(f"{self.nombre} no devolvió datos estructurados (stop_reason={motivo})")

        if self.proveedor == "gemini":
            candidatos = respuesta.get("candidates") or []

            if len(candidatos) == 0:
                bloqueo = respuesta.get("promptFeedback", {}).get("blockReason", "desconocido")
                raise ErrorIA(f"{self.nombre} no devolvió candidatos (bloqueo: {bloqueo})")

            # se juntan los textos y se ignoran las partes de pensamiento del modelo
            partes = candidatos[0].get("content", {}).get("parts", [])
            texto = ""
            for parte in partes:
                if not parte.get("thought"):
                    texto = texto + parte.get("text", "")

            return extraer_json(texto)

        # openai y compatibles
        mensaje = respuesta["choices"][0]["message"]

        if mensaje.get("refusal"):
            raise ErrorIA(f"{self.nombre} rechazó la solicitud: {mensaje['refusal']}")

        contenido = mensaje.get("content") or ""
        return extraer_json(contenido)

    def enviar(self, url, encabezados, cuerpo):
        # hace la peticion http, si el error es pasajero espera y reintenta
        datos = json.dumps(cuerpo).encode("utf-8")

        todos_los_encabezados = {"Content-Type": "application/json"}
        todos_los_encabezados.update(encabezados)

        intento = 0
        while intento <= self.reintentos:
            es_ultimo_intento = intento == self.reintentos

            peticion = urllib.request.Request(url, data=datos, headers=todos_los_encabezados, method="POST")

            try:
                with urllib.request.urlopen(peticion, timeout=self.timeout) as respuesta:
                    return respuesta.read().decode("utf-8")

            except urllib.error.HTTPError as error:
                detalle = error.read().decode("utf-8", errors="replace")

                if error.code not in CODIGOS_REINTENTO or es_ultimo_intento:
                    raise ErrorIA(
                        f"{self.nombre} respondió HTTP {error.code}: {mensaje_error(detalle)}", detalle, error.code
                    ) from error

                # el servidor puede indicar cuantos segundos esperar, si no se espera 2, 4, 8 segundos
                espera = 2 ** (intento + 1)
                indicado = error.headers.get("retry-after", "")
                if indicado.replace(".", "", 1).isdigit():
                    espera = float(indicado)

                time.sleep(min(espera, 60))

            except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
                if es_ultimo_intento:
                    raise ErrorIA(f"Sin conexión con {self.nombre}: {error}") from error

                time.sleep(min(2 ** (intento + 1), 30))

            intento = intento + 1

        raise ErrorIA(f"Sin respuesta de {self.nombre}")
