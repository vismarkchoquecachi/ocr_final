# lee los json de respuestas de la ia y guarda sus campos en sql server
# no llama a la ia, solo lee lo que main.py dejo en las carpetas respuestas_ia de cada pdf
# las busca en todas las subcarpetas de la salida, por ejemplo año-mes/año-mes-dia/codigo/pdf/respuestas_ia
# python cargar_sql.py --prueba muestra lo que se insertaria sin insertar nada
# python cargar_sql.py inserta las paginas que todavia no estan en la tabla
# --carpeta ruta busca las respuestas en otra carpeta y sus subcarpetas
import argparse
import json
import sys
from pathlib import Path

import config
from procesamiento import crear_registro
from sql import RepositorioSql


def leer_respuesta(ruta):
    # devuelve el registro listo para sql server y none, o none y el motivo por el que no se carga
    # utf-8-sig lee igual los archivos guardados con bom, por ejemplo desde el bloc de notas
    contenido = json.loads(ruta.read_text(encoding="utf-8-sig"))
    estado = contenido.get("estado")

    if estado == "error":
        return None, "la IA no devolvió datos: " + str(contenido.get("error") or "sin detalle")

    if estado == "excluida":
        return None, "excluida"

    datos = contenido.get("respuesta")
    if not isinstance(datos, dict):
        return None, "el archivo no tiene respuesta"

    auditoria = {
        "_archivo_hash_sha256": contenido.get("huella_pdf", ""),
        "_modelo": contenido.get("modelo", ""),
    }

    # crear_registro limpia y valida cada campo, y marca la pagina para revisar si falta algo
    registro = crear_registro(contenido["archivo_origen"], int(contenido["pagina"]), datos, None, auditoria)

    # en la columna JsonCrudo queda la respuesta de la ia y sus metricas
    registro.JsonCrudo["respuesta"] = datos
    registro.JsonCrudo["metricas"] = contenido.get("metricas")
    registro.JsonCrudo["archivo_respuesta"] = ruta.name

    # ruta completa de la imagen png que se envio a la ia
    registro.RutaImagen = contenido.get("ruta_imagen")
    registro.JsonCrudo["ruta_imagen"] = registro.RutaImagen

    return registro, None


def cargar(configuracion, carpeta, prueba):
    if not carpeta.is_dir():
        print(f"ERROR: no existe la carpeta de respuestas {carpeta}", file=sys.stderr)
        return 1

    # solo los json que estan dentro de una carpeta de respuestas, asi no se toman otros json de la salida
    archivos = sorted(r for r in carpeta.rglob("*.json") if r.parent.name == configuracion.carpeta_respuestas)
    print(f"Carpeta de salida: {carpeta}")
    print(f"Archivos encontrados: {len(archivos)}")

    insertadas = 0
    existentes = 0
    excluidas = 0
    sin_datos = 0
    ilegibles = 0
    errores = 0

    repositorio = RepositorioSql(
        configuracion.sql_servidor,
        configuracion.sql_base_datos,
        configuracion.sql_usuario,
        configuracion.sql_clave,
    )
    repositorio.conectar()

    try:
        print(f"SQL Server conectado: {configuracion.sql_servidor}/{configuracion.sql_base_datos}\n")

        for ruta in archivos:
            # ruta desde la carpeta de salida, los nombres de archivo se repiten entre carpetas
            nombre = str(ruta.relative_to(carpeta))

            try:
                registro, motivo = leer_respuesta(ruta)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError, OSError) as error:
                ilegibles = ilegibles + 1
                print(f"  {nombre}: no se pudo leer ({error})")
                continue

            if registro is None:
                if motivo == "excluida":
                    excluidas = excluidas + 1
                else:
                    sin_datos = sin_datos + 1
                    print(f"  {nombre}: no se carga, {motivo}")
                continue

            # la pagina ya esta en la tabla, no se toca
            if repositorio.existe(registro.ArchivoOrigen, registro.NumPagina, ""):
                existentes = existentes + 1
                continue

            estado = "REVISAR" if registro.Revisar else "OK"

            if prueba:
                insertadas = insertadas + 1
                print(f"  Se insertaría: {nombre} ({estado})")
                continue

            try:
                repositorio.guardar(registro)
                insertadas = insertadas + 1
                print(f"  Insertada: {nombre} ({estado})")
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
    print(f"Excluidas por la IA (no se cargan): {excluidas}")
    print(f"Sin datos por error de la IA: {sin_datos}")
    print(f"Archivos ilegibles: {ilegibles}")
    print(f"Errores al insertar: {errores}")

    if prueba:
        print("\nModo prueba, no se insertó nada. Ejecute sin --prueba para insertar.")

    if errores > 0 or ilegibles > 0:
        return 1

    return 0


def principal():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    lector = argparse.ArgumentParser(description="Guarda en SQL Server las respuestas de la IA de la carpeta de salida.")
    lector.add_argument("--carpeta", type=Path, help="carpeta donde se buscan las respuestas, con sus subcarpetas (si no se indica, carpeta_salida de .env)")
    lector.add_argument("--prueba", action="store_true", help="muestra lo que se insertaría sin insertar nada")
    argumentos = lector.parse_args()

    try:
        configuracion = config.cargar()

        if configuracion.sql_servidor == "":
            raise ValueError("Falta sql_servidor en .env.")

        if configuracion.sql_usuario != "" and configuracion.sql_clave == "":
            raise ValueError("Falta sql_clave en .env para el usuario de SQL Server.")

        carpeta = argumentos.carpeta
        if carpeta is None:
            carpeta = configuracion.salida

        carpeta = carpeta.expanduser().resolve()
        return cargar(configuracion, carpeta, argumentos.prueba)

    except Exception as error:
        print(f"\nERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(principal())
