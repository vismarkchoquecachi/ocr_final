import base64
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

import pymupdf

# titulo de los formularios que se excluyen sin llamar a la ia
PATRON_EXCLUIDO = re.compile(r"orden(es)? de transferencias? al exterior|\(ote\)")

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

TIPOS = {"DocumentoExcluido": {"type": "boolean"}, "Total": {"anyOf": [{"type": "number"}, {"type": "null"}]}}

ESQUEMA = {
    "type": "object",
    "properties": {campo: TIPOS.get(campo, {"anyOf": [{"type": "string"}, {"type": "null"}]}) for campo in CAMPOS},
    "required": list(CAMPOS),
    "additionalProperties": False,
}

HERRAMIENTA = "registrar_comprobante"
LIMITE_IMAGEN = {"anthropic": 3_700_000}
LIMITE_IMAGEN_GENERAL = 15_000_000
CODIGOS_REINTENTO = {408, 409, 425, 429, 500, 502, 503, 504, 529}
CODIGOS_FATALES = {401, 403, 404}


class ErrorIA(RuntimeError):
    def __init__(self, mensaje: str, respuesta: str = "", codigo: int | None = None):
        super().__init__(mensaje)
        self.respuesta = respuesta
        self.codigo = codigo

    @property
    def fatal(self) -> bool:
        return self.codigo in CODIGOS_FATALES


def _mensaje_error(cuerpo: str) -> str:
    try:
        datos = json.loads(cuerpo)
        error = datos[0].get("error", {}) if isinstance(datos, list) else datos.get("error", {})
        mensaje = error.get("message", "") if isinstance(error, dict) else str(error)
    except (json.JSONDecodeError, AttributeError, IndexError):
        mensaje = cuerpo
    return " ".join(str(mensaje).split())[:300]


def _preparar_imagen(ruta: Path, limite: int) -> tuple[bytes, str]:
    datos = ruta.read_bytes()
    if len(datos) <= limite:
        return datos, "image/png"
    pixmap = pymupdf.Pixmap(str(ruta))
    while True:
        datos = pixmap.tobytes("jpeg", jpg_quality=90)
        if len(datos) <= limite or pixmap.width < 800:
            return datos, "image/jpeg"
        pixmap.shrink(1)


def _extraer_json(texto: str) -> dict:
    coincidencia = re.search(r"\{.*\}", texto or "", re.DOTALL)
    if not coincidencia:
        raise ValueError("la respuesta no contiene un objeto JSON")
    datos = json.loads(coincidencia.group(0))
    if not isinstance(datos, dict):
        raise ValueError("la respuesta no es un objeto JSON")
    return datos


class ExtractorIA:
    def __init__(
        self,
        proveedor: str,
        modelo: str,
        api_key: str = "",
        url_base: str = "",
        timeout: int = 120,
        reintentos: int = 5,
        instrucciones: str = "",
        solicitud: str = "",
    ):
        if proveedor not in {"openai", "anthropic", "gemini", "compatible"}:
            raise ValueError(f"Proveedor no soportado: {proveedor}")
        if not instrucciones.strip() or not solicitud.strip():
            raise ValueError("Falta el prompt. Revise las variables prompt_ en .env.")
        self.instrucciones = instrucciones
        self.solicitud = solicitud
        self.proveedor = proveedor
        self.modelo = modelo
        self.api_key = api_key
        self.url_base = url_base.rstrip("/")
        self.timeout = timeout
        self.reintentos = reintentos

    @property
    def nombre(self) -> str:
        return f"{self.proveedor}/{self.modelo}"

    def extraer(self, ruta_imagen: Path) -> tuple[dict, str]:
        limite = LIMITE_IMAGEN.get(self.proveedor, LIMITE_IMAGEN_GENERAL)
        imagen, tipo = _preparar_imagen(ruta_imagen, limite)
        imagen64 = base64.b64encode(imagen).decode("ascii")

        if self.proveedor == "anthropic":
            crudo = self._anthropic(imagen64, tipo)
        elif self.proveedor == "gemini":
            crudo = self._gemini(imagen64, tipo)
        else:
            crudo = self._openai(imagen64, tipo)

        try:
            return self._interpretar(json.loads(crudo)), crudo
        except ErrorIA as error:
            error.respuesta = crudo
            raise
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise ErrorIA(f"Respuesta no interpretable de {self.nombre}: {error}", crudo) from error

    def _openai(self, imagen64: str, tipo: str) -> str:
        encabezados = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        if "azure" in self.url_base:
            encabezados["api-key"] = self.api_key
        cuerpo = {
            "model": self.modelo,
            "messages": [
                {"role": "system", "content": self.instrucciones},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": self.solicitud},
                        {"type": "image_url", "image_url": {"url": f"data:{tipo};base64,{imagen64}", "detail": "high"}},
                    ],
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "comprobante", "strict": True, "schema": ESQUEMA},
            },
        }
        url = f"{self.url_base}/chat/completions"
        try:
            return self._post(url, encabezados, cuerpo)
        except ErrorIA as error:
            if self.proveedor != "compatible" or error.codigo not in {400, 422}:
                raise
            cuerpo["response_format"] = {"type": "json_object"}
            return self._post(url, encabezados, cuerpo)

    def _anthropic(self, imagen64: str, tipo: str) -> str:
        encabezados = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}
        cuerpo = {
            "model": self.modelo,
            "max_tokens": 2048,
            "system": self.instrucciones,
            "tools": [
                {
                    "name": HERRAMIENTA,
                    "description": "Registra los datos extraídos del comprobante.",
                    "input_schema": ESQUEMA,
                }
            ],
            "tool_choice": {"type": "tool", "name": HERRAMIENTA},
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": tipo, "data": imagen64}},
                        {"type": "text", "text": self.solicitud},
                    ],
                }
            ],
        }
        return self._post(f"{self.url_base}/messages", encabezados, cuerpo)

    def _gemini(self, imagen64: str, tipo: str) -> str:
        cuerpo = {
            "systemInstruction": {"parts": [{"text": self.instrucciones}]},
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": self.solicitud},
                        {"inline_data": {"mime_type": tipo, "data": imagen64}},
                    ],
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseJsonSchema": ESQUEMA,
            },
        }
        url = f"{self.url_base}/models/{self.modelo}:generateContent"
        return self._post(url, {"x-goog-api-key": self.api_key}, cuerpo)

    def _interpretar(self, respuesta: dict) -> dict:
        if self.proveedor == "anthropic":
            for bloque in respuesta.get("content", []):
                if bloque.get("type") == "tool_use":
                    return bloque["input"]
            raise ErrorIA(f"{self.nombre} no devolvió datos estructurados (stop_reason={respuesta.get('stop_reason')})")

        if self.proveedor == "gemini":
            candidatos = respuesta.get("candidates") or []
            if not candidatos:
                bloqueo = respuesta.get("promptFeedback", {}).get("blockReason", "desconocido")
                raise ErrorIA(f"{self.nombre} no devolvió candidatos (bloqueo: {bloqueo})")
            partes = candidatos[0].get("content", {}).get("parts", [])
            return _extraer_json("".join(p.get("text", "") for p in partes if not p.get("thought")))

        mensaje = respuesta["choices"][0]["message"]
        if mensaje.get("refusal"):
            raise ErrorIA(f"{self.nombre} rechazó la solicitud: {mensaje['refusal']}")
        return _extraer_json(mensaje.get("content") or "")

    def _post(self, url: str, encabezados: dict, cuerpo: dict) -> str:
        datos = json.dumps(cuerpo).encode("utf-8")
        for intento in range(self.reintentos + 1):
            peticion = urllib.request.Request(
                url,
                data=datos,
                headers={"Content-Type": "application/json", **encabezados},
                method="POST",
            )
            try:
                with urllib.request.urlopen(peticion, timeout=self.timeout) as respuesta:
                    return respuesta.read().decode("utf-8")
            except urllib.error.HTTPError as error:
                detalle = error.read().decode("utf-8", errors="replace")
                if error.code not in CODIGOS_REINTENTO or intento == self.reintentos:
                    raise ErrorIA(
                        f"{self.nombre} respondió HTTP {error.code}: {_mensaje_error(detalle)}", detalle, error.code
                    ) from error
                espera = error.headers.get("retry-after", "")
                time.sleep(min(float(espera) if espera.replace(".", "", 1).isdigit() else 2 ** (intento + 1), 60))
            except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
                if intento == self.reintentos:
                    raise ErrorIA(f"Sin conexión con {self.nombre}: {error}") from error
                time.sleep(min(2 ** (intento + 1), 30))
        raise ErrorIA(f"Sin respuesta de {self.nombre}")
