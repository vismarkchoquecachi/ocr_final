# lee el archivo .env, revisa cada valor y arma la configuracion que usa el resto del programa
import os
from pathlib import Path

from dotenv import load_dotenv

import prompt


# carpeta donde esta este archivo, las rutas relativas del .env parten de aqui
BASE = Path(__file__).resolve().parent

# por cada proveedor, variable de su clave, modelo que se usa si no se indica otro y url base
PROVEEDORES = {
    "openai": ("openai_clave", "gpt-6-astra", "https://api.openai.com/v1"),
    "anthropic": ("anthropic_clave", "claude-haiku-5-5", "https://api.anthropic.com/v1"),
    "gemini": ("gemini_clave", "gemini-3-flash-preview", "https://generativelanguage.googleapis.com/v1beta"),
    "compatible": ("ia_clave", "", ""),
}

# donde se pueden guardar los resultados
DESTINOS = ("csv", "sql", "ambos")


class Configuracion:
    # guarda todos los valores leidos del .env en un solo objeto

    def __init__(
        self,
        proveedor="",
        modelo="",
        api_key="",
        url_base="",
        destino="csv",
        csv_ruta=None,
        sql_servidor="",
        sql_base_datos="FacturasOCR",
        sql_usuario="",
        sql_clave="",
        entrada=None,
        salida=None,
        dpi=300,
        concurrencia=4,
        omitir_existentes=True,
        archivo_csv="resultados.csv",
        archivo_resultados="resultados.jsonl",
        archivo_excluidos="excluidos.jsonl",
        archivo_errores="errores.jsonl",
        archivo_indice="indice.json",
        tiempo_espera=120,
        reintentos=5,
        instrucciones="",
        solicitud="",
        esfuerzo="",
        recorte=None,
        carpeta_imagenes="imagenes",
        carpeta_respuestas="respuestas_ia",
        paginas_por_envio=20,
    ):
        # credenciales y modelo de la ia
        self.proveedor = proveedor
        self.modelo = modelo
        self.api_key = api_key
        self.url_base = url_base
        self.esfuerzo = esfuerzo

        # donde se guarda
        self.destino = destino
        self.csv_ruta = csv_ruta

        # conexion con sql server
        self.sql_servidor = sql_servidor
        self.sql_base_datos = sql_base_datos
        self.sql_usuario = sql_usuario
        self.sql_clave = sql_clave

        # carpetas
        self.entrada = entrada
        self.salida = salida

        # parametros
        self.dpi = dpi
        self.recorte = recorte
        self.concurrencia = concurrencia
        self.omitir_existentes = omitir_existentes
        self.tiempo_espera = tiempo_espera
        self.reintentos = reintentos

        # nombres de los archivos de la carpeta de salida
        self.archivo_csv = archivo_csv
        self.archivo_resultados = archivo_resultados
        self.archivo_excluidos = archivo_excluidos
        self.archivo_errores = archivo_errores
        self.archivo_indice = archivo_indice

        # carpetas dentro de la carpeta de salida
        self.carpeta_imagenes = carpeta_imagenes
        self.carpeta_respuestas = carpeta_respuestas

        # paginas de un pdf que van juntas en una sola llamada a la ia
        self.paginas_por_envio = paginas_por_envio

        # prompt
        self.instrucciones = instrucciones
        self.solicitud = solicitud

    @property
    def usa_csv(self):
        # true si hay que guardar en el csv
        return self.destino == "csv" or self.destino == "ambos"

    @property
    def usa_sql(self):
        # true si hay que guardar en sql server
        return self.destino == "sql" or self.destino == "ambos"

    @property
    def descripcion(self):
        # texto corto que se muestra al empezar
        destinos = []

        if self.usa_sql:
            servidor = self.sql_servidor
            if servidor == "":
                servidor = "(sin servidor)"
            destinos.append(f"SQL Server {servidor}/{self.sql_base_datos}")

        if self.usa_csv:
            destinos.append("CSV")

        proveedor = self.proveedor
        if proveedor == "":
            proveedor = "sin configurar"

        modelo = self.modelo
        if modelo == "":
            modelo = "-"

        return f"Proveedor: {proveedor} | Modelo: {modelo} | Destino: {' + '.join(destinos)}"


def leer_texto(nombre, predeterminado=""):
    # lee una variable del .env, si esta vacia devuelve el valor predeterminado
    texto = os.getenv(nombre, "").strip()

    if texto == "":
        return predeterminado

    return texto


def leer_ruta(nombre, predeterminada):
    # lee una ruta, si es relativa se toma desde la carpeta del programa
    ruta = Path(leer_texto(nombre, predeterminada)).expanduser()

    if not ruta.is_absolute():
        ruta = BASE / ruta

    return ruta.resolve()


def leer_nombre_archivo(nombre, predeterminado):
    # lee un nombre de archivo, debe ser solo el nombre y con la misma extension que el predeterminado
    texto = leer_texto(nombre, predeterminado)
    extension = Path(predeterminado).suffix

    tiene_carpeta = Path(texto).name != texto
    extension_distinta = Path(texto).suffix.lower() != extension

    if tiene_carpeta or extension_distinta:
        raise ValueError(f"{nombre} debe ser un nombre de archivo {extension}, sin carpetas.")

    return texto


def leer_nombre_carpeta(nombre, predeterminado):
    # lee el nombre de una carpeta que va dentro de la carpeta de salida, sin rutas
    texto = leer_texto(nombre, predeterminado)

    if Path(texto).name != texto or texto in (".", ".."):
        raise ValueError(f"{nombre} debe ser solo el nombre de una carpeta, sin rutas.")

    return texto


def leer_entero(nombre, predeterminado, minimo, maximo):
    # lee un numero entero y revisa que este dentro del rango
    texto = leer_texto(nombre, str(predeterminado))

    if not texto.isdigit():
        raise ValueError(f"{nombre} debe ser un entero entre {minimo} y {maximo}.")

    numero = int(texto)

    if numero < minimo or numero > maximo:
        raise ValueError(f"{nombre} debe ser un entero entre {minimo} y {maximo}.")

    return numero


def leer_booleano(nombre, predeterminado):
    # lee true o false, tambien acepta si, no, 1 y 0
    texto = leer_texto(nombre).lower()

    if texto == "":
        return predeterminado

    if texto in ("1", "true", "si", "sí", "yes"):
        return True

    if texto in ("0", "false", "no"):
        return False

    raise ValueError(f"{nombre} debe ser true o false.")


def detectar_proveedor():
    # si no se indica ia_proveedor se elige segun la clave que este escrita
    if leer_texto("ia_url_base") != "":
        return "compatible"

    for nombre in PROVEEDORES:
        if nombre == "compatible":
            continue

        variable_clave = PROVEEDORES[nombre][0]
        if leer_texto(variable_clave) != "":
            return nombre

    return ""


def cargar():
    # lee el .env completo y devuelve un objeto configuracion
    load_dotenv(BASE / ".env", override=True, encoding="utf-8-sig")

    # proveedor de ia
    proveedor = leer_texto("ia_proveedor").lower()
    if proveedor == "":
        proveedor = detectar_proveedor()

    if proveedor != "" and proveedor not in PROVEEDORES:
        raise ValueError(f"ia_proveedor debe ser uno de: {', '.join(PROVEEDORES)}.")

    variable_clave = ""
    modelo_base = ""
    url_base = ""
    if proveedor != "":
        variable_clave = PROVEEDORES[proveedor][0]
        modelo_base = PROVEEDORES[proveedor][1]
        url_base = PROVEEDORES[proveedor][2]

    # clave de la ia, ia_clave tiene prioridad sobre la del proveedor
    api_key = leer_texto("ia_clave")
    if api_key == "" and variable_clave != "":
        api_key = leer_texto(variable_clave)

    # destino, sin destino se guarda en ambos si hay servidor sql, si no solo en csv
    sql_servidor = leer_texto("sql_servidor")
    destino = leer_texto("destino").lower()
    if destino == "":
        if sql_servidor != "":
            destino = "ambos"
        else:
            destino = "csv"

    if destino not in DESTINOS:
        raise ValueError(f"destino debe ser uno de: {', '.join(DESTINOS)}.")

    # ruta del csv, vacia significa carpeta de salida
    csv_ruta = None
    if leer_texto("csv_ruta") != "":
        csv_ruta = leer_ruta("csv_ruta", "")

    # nombres de archivos, no pueden repetirse
    archivo_csv = leer_nombre_archivo("archivo_csv", "resultados.csv")
    archivo_resultados = leer_nombre_archivo("archivo_resultados", "resultados.jsonl")
    archivo_excluidos = leer_nombre_archivo("archivo_excluidos", "excluidos.jsonl")
    archivo_errores = leer_nombre_archivo("archivo_errores", "errores.jsonl")
    archivo_indice = leer_nombre_archivo("archivo_indice", "indice.json")

    nombres_vistos = []
    for nombre_archivo in (archivo_csv, archivo_resultados, archivo_excluidos, archivo_errores, archivo_indice):
        if nombre_archivo.lower() in nombres_vistos:
            raise ValueError(
                "Los nombres de archivo_csv, archivo_resultados, archivo_excluidos, archivo_errores y archivo_indice deben ser distintos entre sí."
            )
        nombres_vistos.append(nombre_archivo.lower())

    # prompt
    instrucciones, solicitud = prompt.cargar()

    # la clave de sql no se recorta porque puede tener espacios a proposito
    sql_clave = os.getenv("sql_clave", "")

    url_final = leer_texto("ia_url_base", url_base)
    url_final = url_final.rstrip("/")

    return Configuracion(
        proveedor=proveedor,
        modelo=leer_texto("ia_modelo", modelo_base),
        api_key=api_key,
        url_base=url_final,
        destino=destino,
        csv_ruta=csv_ruta,
        sql_servidor=sql_servidor,
        sql_base_datos=leer_texto("sql_base_datos", "FacturasOCR"),
        sql_usuario=leer_texto("sql_usuario"),
        sql_clave=sql_clave,
        entrada=leer_ruta("carpeta_pdf", "pdfs"),
        salida=leer_ruta("carpeta_salida", "salida"),
        dpi=leer_entero("imagen_dpi", 200, 72, 450),
        recorte=leer_recorte(),
        carpeta_imagenes=leer_nombre_carpeta("carpeta_imagenes", "imagenes"),
        carpeta_respuestas=leer_nombre_carpeta("carpeta_respuestas", "respuestas_ia"),
        paginas_por_envio=leer_entero("ia_paginas_por_envio", 20, 1, 50),
        concurrencia=leer_entero("ia_concurrencia", 4, 1, 16),
        omitir_existentes=leer_booleano("omitir_existentes", True),
        archivo_csv=archivo_csv,
        archivo_resultados=archivo_resultados,
        archivo_excluidos=archivo_excluidos,
        archivo_errores=archivo_errores,
        archivo_indice=archivo_indice,
        tiempo_espera=leer_entero("ia_tiempo_espera", 120, 10, 600),
        reintentos=leer_entero("ia_reintentos", 5, 0, 10),
        instrucciones=instrucciones,
        solicitud=solicitud,
        esfuerzo=leer_texto("ia_esfuerzo").lower(),
    )


def leer_recorte():
    # ajustes del recorte de margenes, devuelve none si recorte_activo es false
    if not leer_booleano("recorte_activo", True):
        return None

    return {
        "dpi_deteccion": leer_entero("recorte_dpi_deteccion", 50, 20, 150),
        "diferencia": leer_entero("recorte_diferencia", 40, 5, 200),
        "minimo_pixeles": leer_entero("recorte_minimo_pixeles", 2, 1, 50),
        "margen_porcentaje": leer_entero("recorte_margen_porcentaje", 2, 0, 20),
    }


def validar(configuracion):
    # revisa que no falte nada antes de empezar a procesar
    if configuracion.proveedor == "":
        raise ValueError("Falta la credencial de IA. Complete en .env la clave, por ejemplo anthropic_clave.")

    if configuracion.modelo == "":
        raise ValueError("Falta ia_modelo en .env.")

    if configuracion.url_base == "":
        raise ValueError("Falta ia_url_base en .env para el proveedor compatible.")

    if configuracion.instrucciones == "" or configuracion.solicitud == "":
        raise ValueError("Falta el prompt en .env. Copie las variables prompt_ desde .env.example.")

    if configuracion.proveedor != "compatible" and configuracion.api_key == "":
        variable_clave = PROVEEDORES[configuracion.proveedor][0]
        raise ValueError(f"Falta {variable_clave} en .env.")

    if configuracion.usa_sql and configuracion.sql_servidor == "":
        raise ValueError("Para guardar en SQL Server falta sql_servidor en .env (o elija el destino csv).")

    if configuracion.usa_sql and configuracion.sql_usuario != "" and configuracion.sql_clave == "":
        raise ValueError("Falta sql_clave en .env para el usuario de SQL Server.")
