# envia a la ia todas las paginas de un pdf en una sola llamada y devuelve los datos que extrajo
# cada imagen va precedida por su numero de pagina, el prompt va una sola vez
# funciona con gemini, openai, anthropic y servicios compatibles con openai
import base64
import json
import re
import threading
import time
import urllib.error
import urllib.request

import pymupdf


# titulo de los formularios que se excluyen sin llamar a la ia
PATRON_EXCLUIDO = re.compile(r"orden(es)? de transferencias? al exterior|\(ote\)")

# campos que la ia devuelve por cada documento de pago que encuentra en el pdf
# Paginas son los numeros de pagina donde esta ese documento
CAMPOS_DOCUMENTO = (
    "Paginas",
    "NombreOrdenante",
    "FechaTransferencia",
    "INVOICE",
    "Total",
    "NombreBeneficiario",
    "BancoBeneficiario",
    "MotivoRevision",
)

# limite de tokens de la respuesta de anthropic, incluye el razonamiento del modelo
# solo se paga lo que realmente se usa, un limite bajo puede cortar la respuesta
MAXIMO_TOKENS_ANTHROPIC = 16000

# modelos de anthropic que aceptan un modelo de respaldo si el primero rechaza la pagina
MODELOS_CON_RESPALDO = ("claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-2", "claude-fable-5-1")

# niveles de esfuerzo que acepta anthropic, mas esfuerzo es mas razonamiento y mas costo
ESFUERZOS = ("low", "medium", "high", "xhigh", "max")

# peso maximo de la imagen en bytes, anthropic acepta menos que los demas
LIMITE_IMAGEN_ANTHROPIC = 3_700_000
LIMITE_IMAGEN_GENERAL = 15_000_000

# codigos http que indican un problema pasajero, se vuelve a intentar
CODIGOS_REINTENTO = (408, 409, 425, 429, 500, 502, 503, 504, 529)

# codigos http que indican clave o modelo incorrectos, se detiene todo
CODIGOS_FATALES = (401, 403, 404)


def crear_esquema():
    # describe el json que debe devolver la ia, todos los campos son obligatorios
    # Documentos: un elemento por documento de pago, puede ocupar varias paginas
    # PaginasExcluidas: las paginas sin documento de pago, con su motivo
    propiedades = {}

    for campo in CAMPOS_DOCUMENTO:
        if campo == "Paginas":
            propiedades[campo] = {"type": "array", "items": {"type": "integer"}}
        elif campo == "Total":
            propiedades[campo] = {"anyOf": [{"type": "number"}, {"type": "null"}]}
        else:
            propiedades[campo] = {"anyOf": [{"type": "string"}, {"type": "null"}]}

    documento = {
        "type": "object",
        "properties": propiedades,
        "required": list(CAMPOS_DOCUMENTO),
        "additionalProperties": False,
    }

    excluida = {
        "type": "object",
        "properties": {"Pagina": {"type": "integer"}, "Motivo": {"type": "string"}},
        "required": ["Pagina", "Motivo"],
        "additionalProperties": False,
    }

    return {
        "type": "object",
        "properties": {
            "Documentos": {"type": "array", "items": documento},
            "PaginasExcluidas": {"type": "array", "items": excluida},
        },
        "required": ["Documentos", "PaginasExcluidas"],
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
        esfuerzo="",
    ):
        if proveedor not in ("openai", "anthropic", "gemini", "compatible"):
            raise ValueError(f"Proveedor no soportado: {proveedor}")

        if esfuerzo != "" and esfuerzo not in ESFUERZOS:
            raise ValueError(f"ia_esfuerzo debe ser uno de: {', '.join(ESFUERZOS)}, o quedar vacío.")

        self.esfuerzo = esfuerzo

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

        # tokens del prompt sin la imagen, se cuentan una sola vez por ejecucion
        self.candado = threading.Lock()
        self.prompt_contado = False
        self.tokens_prompt = None

    @property
    def nombre(self):
        # nombre que se muestra en los mensajes, por ejemplo gemini/gemini-3-flash-preview
        return f"{self.proveedor}/{self.modelo}"

    def extraer(self, paginas):
        # envia en una sola llamada todas las paginas recibidas, una lista de pares numero de pagina y ruta de la imagen
        # paso 1, preparar las imagenes
        if self.proveedor == "anthropic":
            limite = LIMITE_IMAGEN_ANTHROPIC
        else:
            limite = LIMITE_IMAGEN_GENERAL

        imagenes = []
        for numero, ruta_imagen in paginas:
            imagen, tipo = preparar_imagen(ruta_imagen, limite)
            imagenes.append((numero, base64.b64encode(imagen).decode("ascii"), tipo))

        # paso 2, enviarlas al proveedor que corresponde
        if self.proveedor == "anthropic":
            crudo = self.llamar_anthropic(imagenes)
        elif self.proveedor == "gemini":
            crudo = self.llamar_gemini(imagenes)
        else:
            crudo = self.llamar_openai(imagenes)

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

    def llamar_openai(self, imagenes):
        # formato de openai, tambien lo usan ollama, openrouter, groq, deepseek y azure
        encabezados = {}
        if self.api_key != "":
            encabezados["Authorization"] = f"Bearer {self.api_key}"
        if "azure" in self.url_base:
            encabezados["api-key"] = self.api_key

        mensaje_sistema = {"role": "system", "content": self.instrucciones}

        # cada imagen va precedida por su numero de pagina y la solicitud va al final
        contenido = []
        for numero, imagen64, tipo in imagenes:
            contenido.append({"type": "text", "text": f"Página {numero}"})
            contenido.append({"type": "image_url", "image_url": {"url": f"data:{tipo};base64,{imagen64}", "detail": "high"}})
        contenido.append({"type": "text", "text": self.solicitud})

        mensaje_usuario = {"role": "user", "content": contenido}

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

    def llamar_anthropic(self, imagenes):
        # formato de anthropic, la respuesta se pide en json con el esquema
        # no se usa una herramienta obligatoria porque los modelos actuales la rechazan con error 400
        encabezados = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}

        # cada imagen va precedida por su numero de pagina y la solicitud va al final
        contenido = []
        for numero, imagen64, tipo in imagenes:
            contenido.append({"type": "text", "text": f"Página {numero}"})
            contenido.append({"type": "image", "source": {"type": "base64", "media_type": tipo, "data": imagen64}})
        contenido.append({"type": "text", "text": self.solicitud})

        mensaje_usuario = {"role": "user", "content": contenido}

        # formato de la respuesta y esfuerzo, si el esfuerzo queda vacio se usa el del modelo
        opciones_salida = {"format": {"type": "json_schema", "schema": ESQUEMA}}
        if self.esfuerzo != "":
            opciones_salida["effort"] = self.esfuerzo

        cuerpo = {
            "model": self.modelo,
            "max_tokens": MAXIMO_TOKENS_ANTHROPIC,
            "system": self.instrucciones,
            "messages": [mensaje_usuario],
            "output_config": opciones_salida,
        }

        # si el modelo rechaza la pagina por seguridad, la api la reintenta sola con un modelo de respaldo
        if self.modelo in MODELOS_CON_RESPALDO:
            encabezados["anthropic-beta"] = "server-side-fallback-2026-07-01"
            cuerpo["fallbacks"] = "default"

        url = f"{self.url_base}/messages"
        return self.enviar(url, encabezados, cuerpo)

    def contar_tokens_prompt(self):
        # tokens del prompt sin la imagen, anthropic devuelve prompt e imagen sumados en input_tokens
        # se cuentan una vez con el endpoint de conteo, que no se cobra, y la diferencia es la imagen
        # devuelve none si no es anthropic o si no se pudo contar
        if self.proveedor != "anthropic":
            return None

        with self.candado:
            if self.prompt_contado:
                return self.tokens_prompt
            self.prompt_contado = True

            encabezados = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}
            cuerpo = {
                "model": self.modelo,
                "system": self.instrucciones,
                "messages": [{"role": "user", "content": [{"type": "text", "text": self.solicitud}]}],
                "output_config": {"format": {"type": "json_schema", "schema": ESQUEMA}},
            }
            url = f"{self.url_base}/messages/count_tokens"

            try:
                crudo = self.enviar(url, encabezados, cuerpo)
            except ErrorIA as error:
                # si el conteo no acepta el esquema se cuenta sin el
                if error.codigo != 400:
                    return None
                cuerpo.pop("output_config")
                try:
                    crudo = self.enviar(url, encabezados, cuerpo)
                except ErrorIA:
                    return None

            try:
                self.tokens_prompt = int(json.loads(crudo)["input_tokens"])
            except (ValueError, KeyError, TypeError):
                self.tokens_prompt = None

            return self.tokens_prompt

    def llamar_gemini(self, imagenes):
        # formato de gemini, la respuesta se pide en json con el esquema
        encabezados = {"x-goog-api-key": self.api_key}

        # cada imagen va precedida por su numero de pagina y la solicitud va al final
        partes = []
        for numero, imagen64, tipo in imagenes:
            partes.append({"text": f"Página {numero}"})
            partes.append({"inline_data": {"mime_type": tipo, "data": imagen64}})
        partes.append({"text": self.solicitud})

        mensaje_usuario = {"role": "user", "parts": partes}

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
            motivo = respuesta.get("stop_reason")

            # el modelo, y el de respaldo si lo hubo, rechazaron la pagina
            if motivo == "refusal":
                detalle = respuesta.get("stop_details") or {}
                categoria = detalle.get("category") or "sin categoría"
                raise ErrorIA(f"{self.nombre} rechazó la página (categoría: {categoria})")

            # la respuesta se corto antes de terminar el json
            if motivo == "max_tokens":
                raise ErrorIA(f"{self.nombre} cortó la respuesta por el límite de {MAXIMO_TOKENS_ANTHROPIC} tokens")

            # se juntan los bloques de texto, los de razonamiento se ignoran
            texto = ""
            for bloque in respuesta.get("content", []):
                if bloque.get("type") == "text":
                    texto = texto + bloque.get("text", "")

            if texto == "":
                raise ErrorIA(f"{self.nombre} no devolvió datos (stop_reason={motivo})")

            return extraer_json(texto)

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
