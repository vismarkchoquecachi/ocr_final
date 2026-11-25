# guarda las paginas en sql server
# este archivo solo consulta e inserta, nunca modifica ni borra filas
# la tabla se crea una vez con crear_tabla.sql
import json
from datetime import datetime


# drivers odbc que se buscan, en este orden
DRIVERS = ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server", "SQL Server")

ARCHIVO_DDL = "crear_tabla.sql"

# una pagina que ya esta en la tabla no se vuelve a enviar a la ia
EXISTE = """
SELECT TOP (1) 1
FROM dbo.SeguimientoOCR
WHERE ArchivoOrigen = ? AND NumPagina = ?;
"""

# el orden de los valores en guardar debe ser el mismo que el de estas columnas
GUARDAR = """
INSERT INTO dbo.SeguimientoOCR
(
    ArchivoOrigen,
    NumPagina,
    FechaCarga,
    NombreOrdenante,
    FechaTransferencia,
    INVOICE,
    Total,
    NombreBeneficiario,
    BancoBeneficiario,
    Revisar,
    MotivoRevision,
    JsonCrudo
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
"""

# numero de error de sql server cuando la fila ya existe
ERROR_DUPLICADO = "(2627)"


def valor_odbc(valor):
    # encierra un valor entre llaves para que un punto y coma en la clave no rompa la conexion
    return "{" + valor.replace("}", "}}") + "}"


def cadena_conexion(servidor, usuario, clave, drivers_instalados):
    # arma el texto de conexion, sin usuario usa la autenticacion de windows
    driver = None
    for nombre in DRIVERS:
        if nombre in drivers_instalados:
            driver = nombre
            break

    if driver is None:
        raise RuntimeError("No hay un driver ODBC de SQL Server instalado. Instale 'Microsoft ODBC Driver 18 for SQL Server'.")

    partes = [
        "DRIVER={" + driver + "}",
        "SERVER=" + valor_odbc(servidor),
        "TrustServerCertificate=yes",
    ]

    if usuario != "":
        partes.append("UID=" + valor_odbc(usuario))
        partes.append("PWD=" + valor_odbc(clave))
    else:
        partes.append("Trusted_Connection=yes")

    return ";".join(partes) + ";"


class RepositorioSql:
    # conexion con sql server, primero se llama a conectar y al terminar a cerrar

    def __init__(self, servidor, base_datos, usuario="", clave=""):
        self.servidor = servidor
        self.base_datos = base_datos
        self.usuario = usuario
        self.clave = clave
        self.conexion = None

    def conectar(self):
        try:
            import pyodbc
        except ModuleNotFoundError as error:
            raise RuntimeError(
                "Para guardar en SQL Server falta el paquete pyodbc. Instálelo con: python -m pip install -r requirements.txt"
            ) from error

        cadena = cadena_conexion(self.servidor, self.usuario, self.clave, pyodbc.drivers())

        try:
            self.conexion = pyodbc.connect(cadena, autocommit=True, timeout=15)

            nombre_base = self.base_datos.replace("]", "]]")
            self.conexion.cursor().execute(f"USE [{nombre_base}];")

            # desde aqui cada insert se confirma a mano
            self.conexion.autocommit = False

        except pyodbc.Error as error:
            self.cerrar()

            if self.usuario != "":
                acceso = f"el usuario {self.usuario}"
            else:
                acceso = "la autenticación de Windows"

            raise RuntimeError(
                f"No se pudo conectar a SQL Server '{self.servidor}', base '{self.base_datos}', con {acceso}. "
                f"Revise sql_servidor, sql_base_datos, sql_usuario y sql_clave en .env. "
                f"Si la base todavía no existe, ejecute {ARCHIVO_DDL} en SQL Server. Detalle: {error}"
            ) from error

    def cerrar(self):
        if self.conexion is not None:
            self.conexion.close()
            self.conexion = None

    def ejecutar(self, sentencia, parametros):
        # ejecuta y confirma, si falla deshace
        try:
            self.conexion.cursor().execute(sentencia, parametros)
            self.conexion.commit()
        except Exception:
            self.conexion.rollback()
            raise

    def existe(self, origen, pagina, hash_archivo):
        cursor = self.conexion.cursor()
        cursor.execute(EXISTE, origen[:500], pagina)
        fila = cursor.fetchone()
        return fila is not None

    def guardar(self, registro):
        # los errores no se insertan para que la pagina se pueda reintentar en la siguiente ejecucion
        if registro.es_error:
            return

        json_crudo = json.dumps(registro.JsonCrudo, ensure_ascii=False, default=str)

        # fecha y hora de esta maquina, sin microsegundos
        fecha_carga = datetime.now().replace(microsecond=0)

        valores = (
            registro.ArchivoOrigen,
            registro.NumPagina,
            fecha_carga,
            registro.NombreOrdenante,
            registro.FechaTransferencia,
            registro.INVOICE,
            registro.Total,
            registro.NombreBeneficiario,
            registro.BancoBeneficiario,
            registro.Revisar,
            registro.MotivoRevision,
            json_crudo,
        )

        try:
            self.ejecutar(GUARDAR, valores)
        except Exception as error:
            if ERROR_DUPLICADO not in str(error):
                raise
            raise RuntimeError(
                "la página ya existe en SQL Server y no se modificó. Para reemplazarla, borre antes esa fila de la tabla."
            ) from error
