# pasa a sql server las paginas ya procesadas en el historial, sin llamar a la ia
# python importar.py --prueba muestra lo que se insertaria sin insertar nada
# python importar.py inserta las paginas que todavia no estan en la tabla
import argparse
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import config
from procesamiento import TIPO_ERROR, Registro
from sql import RepositorioSql


def crear_registro_desde_linea(fila):
    # convierte una linea del historial en un registro listo para insertar
    fecha = None
    if fila.get("FechaTransferencia"):
        fecha = date.fromisoformat(fila["FechaTransferencia"])

    total = None
    if fila.get("Total") is not None and fila.get("Total") != "":
        total = Decimal(str(fila["Total"]))

    return Registro(
        ArchivoOrigen=fila["ArchivoOrigen"],
        NumPagina=int(fila["NumPagina"]),
        NombreOrdenante=fila.get("NombreOrdenante"),
        FechaTransferencia=fecha,
        INVOICE=fila.get("INVOICE"),
        Total=total,
        NombreBeneficiario=fila.get("NombreBeneficiario"),
        BancoBeneficiario=fila.get("BancoBeneficiario"),
        Revisar=bool(fila.get("Revisar")),
        MotivoRevision=fila.get("MotivoRevision"),
        JsonCrudo=fila.get("JsonCrudo") or {},
    )


def leer_paginas(ruta):
    # ultimo resultado valido de cada pagina, los errores se ignoran
    paginas = {}
    numero_linea = 0

    for linea in ruta.read_text(encoding="utf-8").splitlines():
        numero_linea = numero_linea + 1

        if linea.strip() == "":
            continue

        try:
            fila = json.loads(linea)
            crudo = fila.get("JsonCrudo") or {}

            if crudo.get("tipo") == TIPO_ERROR:
                continue

            registro = crear_registro_desde_linea(fila)

        except (json.JSONDecodeError, KeyError, TypeError, ValueError, ArithmeticError) as error:
            print(f"  Línea {numero_linea} ignorada, no se pudo leer: {error}")
            continue

        # si la pagina aparece varias veces queda la ultima
        clave = (registro.ArchivoOrigen.lower(), registro.NumPagina)
        paginas[clave] = registro

    return list(paginas.values())


def importar(configuracion, ruta, prueba):
    if not ruta.is_file():
        print(f"ERROR: no existe el historial {ruta}", file=sys.stderr)
        return 1

    paginas = leer_paginas(ruta)
    print(f"Historial: {ruta}")
    print(f"Páginas válidas en el historial: {len(paginas)}")

    insertadas = 0
    existentes = 0
    errores = 0

    repositorio = RepositorioSql(
        configuracion.sql_servidor,
        configuracion.sql_base_datos,
        configuracion.sql_usuario,
        configuracion.sql_clave,
    )
    repositorio.conectar()

    try:
        print(f"SQL Server conectado: {configuracion.sql_servidor}/{configuracion.sql_base_datos}")

        for registro in paginas:
            nombre = f"{Path(registro.ArchivoOrigen).name} página {registro.NumPagina}"

            # la pagina ya esta en la tabla, no se toca
            if repositorio.existe(registro.ArchivoOrigen, registro.NumPagina, ""):
                existentes = existentes + 1
                continue

            # en modo prueba solo se muestra
            if prueba:
                insertadas = insertadas + 1
                print(f"  Se insertaría: {nombre}")
                continue

            try:
                repositorio.guardar(registro)
                insertadas = insertadas + 1
                print(f"  Insertada: {nombre}")
            except Exception as error:
                errores = errores + 1
                print(f"  No se pudo insertar {nombre}: {error}")

    finally:
        repositorio.cerrar()

    if prueba:
        print(f"\nSe insertarían: {insertadas}")
    else:
        print(f"\nInsertadas: {insertadas}")

    print(f"Ya estaban en SQL Server: {existentes}")
    print(f"Errores: {errores}")

    if prueba:
        print("\nModo prueba, no se insertó nada. Ejecute sin --prueba para insertar.")

    if errores > 0:
        return 1

    return 0


def principal():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    lector = argparse.ArgumentParser(description="Importa a SQL Server las páginas ya procesadas, sin llamar a la IA.")
    lector.add_argument("--historial", type=Path, help="archivo de historial a importar (si no se indica, el de la carpeta de salida de .env)")
    lector.add_argument("--prueba", action="store_true", help="muestra lo que se insertaría sin insertar nada")
    argumentos = lector.parse_args()

    try:
        configuracion = config.cargar()

        if configuracion.sql_servidor == "":
            raise ValueError("Falta sql_servidor en .env.")

        if configuracion.sql_usuario != "" and configuracion.sql_clave == "":
            raise ValueError("Falta sql_clave en .env para el usuario de SQL Server.")

        ruta = argumentos.historial
        if ruta is None:
            ruta = configuracion.salida / configuracion.archivo_resultados

        ruta = ruta.expanduser().resolve()
        return importar(configuracion, ruta, argumentos.prueba)

    except Exception as error:
        print(f"\nERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(principal())
