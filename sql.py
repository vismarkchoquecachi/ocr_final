import json

from procesamiento import Registro

DRIVERS = ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server", "SQL Server")

# este archivo solo consulta e inserta, nunca modifica ni borra filas
# la tabla se crea una vez con crear_tabla.sql
ARCHIVO_DDL = "crear_tabla.sql"

# una pagina que ya esta en la tabla no se vuelve a enviar a la ia
EXISTE = """
SELECT TOP (1) 1
FROM dbo.SeguimientoOCR
WHERE ArchivoOrigen = ? AND NumPagina = ?;
"""

CAMPOS = (
    "NombreOrdenante", "FechaTransferencia", "INVOICE", "Total", "NombreBeneficiario",
    "BancoBeneficiario", "Revisar", "MotivoRevision", "JsonCrudo",
)

GUARDAR = f"""
INSERT INTO dbo.SeguimientoOCR (ArchivoOrigen, NumPagina, {", ".join(CAMPOS)})
VALUES (?, ?, {", ".join("?" for _ in CAMPOS)});
"""

# numero de error de sql server cuando la fila ya existe
ERROR_DUPLICADO = "(2627)"


def _valor_odbc(valor: str) -> str:
    return "{" + valor.replace("}", "}}") + "}"


def cadena_conexion(servidor: str, usuario: str, clave: str, drivers_instalados: list[str]) -> str:
    driver = next((d for d in DRIVERS if d in drivers_instalados), None)
    if driver is None:
        raise RuntimeError("No hay un driver ODBC de SQL Server instalado. Instale 'Microsoft ODBC Driver 18 for SQL Server'.")
    partes = [f"DRIVER={{{driver}}}", f"SERVER={_valor_odbc(servidor)}", "TrustServerCertificate=yes"]
    if usuario:
        partes += [f"UID={_valor_odbc(usuario)}", f"PWD={_valor_odbc(clave)}"]
    else:
        partes.append("Trusted_Connection=yes")
    return ";".join(partes) + ";"


class RepositorioSql:
    def __init__(self, servidor: str, base_datos: str, usuario: str = "", clave: str = ""):
        self.servidor = servidor
        self.base_datos = base_datos
        self.usuario = usuario
        self.clave = clave
        self.conexion = None

    def __enter__(self):
        try:
            import pyodbc
        except ModuleNotFoundError as error:
            raise RuntimeError(
                "Para guardar en SQL Server falta el paquete pyodbc. Instálelo con: python -m pip install -r requirements.txt"
            ) from error

        cadena = cadena_conexion(self.servidor, self.usuario, self.clave, pyodbc.drivers())
        try:
            self.conexion = pyodbc.connect(cadena, autocommit=True, timeout=15)
            cursor = self.conexion.cursor()
            cursor.execute(f"USE [{self.base_datos.replace(']', ']]')}];")
            self.conexion.autocommit = False
        except pyodbc.Error as error:
            self.__exit__()
            acceso = f"el usuario {self.usuario}" if self.usuario else "la autenticación de Windows"
            raise RuntimeError(
                f"No se pudo conectar a SQL Server '{self.servidor}', base '{self.base_datos}', con {acceso}. "
                f"Revise sql_servidor, sql_base_datos, sql_usuario y sql_clave en .env. "
                f"Si la base todavía no existe, ejecute {ARCHIVO_DDL} en SQL Server. Detalle: {error}"
            ) from error
        return self

    def __exit__(self, *_):
        if self.conexion is not None:
            self.conexion.close()
            self.conexion = None

    def _ejecutar(self, sql: str, *parametros) -> None:
        try:
            self.conexion.cursor().execute(sql, *parametros)
            self.conexion.commit()
        except Exception:
            self.conexion.rollback()
            raise

    def existe(self, origen: str, pagina: int, hash_archivo: str) -> bool:
        return self.conexion.cursor().execute(EXISTE, origen[:500], pagina).fetchone() is not None

    def guardar(self, registro: Registro) -> None:
        # los errores no se insertan para que la pagina se pueda reintentar en la siguiente ejecucion
        if registro.es_error:
            return
        valores = [getattr(registro, c) for c in CAMPOS]
        valores[-1] = json.dumps(registro.JsonCrudo, ensure_ascii=False, default=str)
        try:
            self._ejecutar(GUARDAR, registro.ArchivoOrigen, registro.NumPagina, *valores)
        except Exception as error:
            if ERROR_DUPLICADO not in str(error):
                raise
            raise RuntimeError(
                "la página ya existe en SQL Server y no se modificó. Para reemplazarla, borre antes esa fila de la tabla."
            ) from error
