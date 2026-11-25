# indice de paginas ya procesadas, identifica cada pagina por la huella del pdf y su numero
# la ruta y el nombre del archivo no cuentan, si el pdf se mueve o se renombra se reconoce igual
# si el contenido del pdf cambia la huella es otra y sus paginas se procesan de nuevo
#
# forma del archivo
# {
#   "huella del pdf": {
#     "archivo": "nombre.pdf",
#     "ruta": "ruta completa donde se vio por ultima vez",
#     "paginas": {"1": "excluida", "2": "ok", "3": "revisar", "4": "error"}
#   }
# }
import json
import os


# estados que cuentan como pagina terminada, las de error se vuelven a enviar
ESTADOS_TERMINADOS = ("ok", "revisar", "excluida")


class IndicePaginas:

    def __init__(self, ruta):
        self.ruta = ruta
        self.documentos = {}

        if self.ruta.exists():
            texto = self.ruta.read_text(encoding="utf-8")
            if texto.strip() != "":
                self.documentos = json.loads(texto)

    def estado(self, huella, pagina):
        # devuelve ok, revisar, excluida o error, o none si la pagina nunca se proceso
        documento = self.documentos.get(huella)

        if documento is None:
            return None

        return documento["paginas"].get(str(pagina))

    def marcar(self, huella, pagina, estado, ruta_pdf, guardar=True):
        # anota el estado de una pagina, un error nunca reemplaza un resultado bueno anterior
        documento = self.documentos.get(huella)

        if documento is None:
            documento = {"archivo": "", "ruta": "", "paginas": {}}
            self.documentos[huella] = documento

        documento["ruta"] = ruta_pdf
        documento["archivo"] = os.path.basename(ruta_pdf)

        anterior = documento["paginas"].get(str(pagina))
        if estado == "error" and anterior in ESTADOS_TERMINADOS:
            return

        documento["paginas"][str(pagina)] = estado

        if guardar:
            self.guardar()

    def guardar(self):
        # escribe primero un temporal y despues lo reemplaza, asi el indice nunca queda a medias
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        temporal = self.ruta.with_name(self.ruta.name + ".tmp")

        texto = json.dumps(self.documentos, ensure_ascii=False, indent=2)
        temporal.write_text(texto, encoding="utf-8")
        os.replace(temporal, self.ruta)

    def armar_desde_historial(self, ruta_resultados, ruta_excluidos):
        # la primera vez se arma con lo que ya hay en el historial, asi no se vuelve a cobrar nada
        cantidad = 0

        if ruta_resultados.exists():
            for linea in ruta_resultados.read_text(encoding="utf-8").splitlines():
                try:
                    fila = json.loads(linea)
                    crudo = fila.get("JsonCrudo") or {}
                    huella = crudo.get("_archivo_hash_sha256", "")
                    pagina = int(fila["NumPagina"])
                    ruta_pdf = fila["ArchivoOrigen"]
                except (json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError):
                    continue

                if huella == "":
                    continue

                if crudo.get("tipo") == "ERROR_PROCESAMIENTO":
                    estado = "error"
                elif fila.get("Revisar"):
                    estado = "revisar"
                else:
                    estado = "ok"

                self.marcar(huella, pagina, estado, ruta_pdf, guardar=False)
                cantidad = cantidad + 1

        if ruta_excluidos.exists():
            for linea in ruta_excluidos.read_text(encoding="utf-8").splitlines():
                try:
                    fila = json.loads(linea)
                    huella = fila["Hash"]
                    pagina = int(fila["NumPagina"])
                    ruta_pdf = fila["ArchivoOrigen"]
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    continue

                self.marcar(huella, pagina, "excluida", ruta_pdf, guardar=False)
                cantidad = cantidad + 1

        if cantidad > 0:
            self.guardar()

        return cantidad
