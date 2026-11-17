# ocr de comprobantes de transferencia
# toda la configuracion, las credenciales y el prompt estan en .env
# python main.py procesa los pdf, python main.py --ventana abre la interfaz
import argparse
import copy
import queue
import shutil
import sys
import threading
from pathlib import Path

try:
    import config
    from archivo_csv import comprobar_destino, exportar_csv, resolver_ruta
    from ia import ExtractorIA
    from procesamiento import RepositorioLocal, procesar
except ModuleNotFoundError as error_importacion:
    sys.exit(
        f"Falta el paquete de Python '{error_importacion.name}'. Instale las dependencias con:\n"
        "    python -m pip install -r requirements.txt"
    )


# textos del selector de destino en la ventana
DESTINOS_VENTANA = {
    "csv": "Archivo CSV",
    "sql": "SQL Server",
    "ambos": "SQL Server y archivo CSV",
}


def preparar_entorno():
    # crea el archivo .env a partir de .env.example si falta, devuelve true si lo acaba de crear
    ruta_env = config.BASE / ".env"
    ruta_ejemplo = config.BASE / ".env.example"

    if ruta_env.exists():
        return False

    if not ruta_ejemplo.exists():
        return False

    shutil.copyfile(ruta_ejemplo, ruta_env)
    return True


def elegir_destino(configuracion, destino, ruta_csv):
    # el destino y el csv elegidos al ejecutar tienen prioridad sobre .env
    if destino is None and ruta_csv is None:
        return configuracion

    nueva = copy.copy(configuracion)

    if ruta_csv is not None:
        nueva.csv_ruta = ruta_csv.expanduser().resolve()

        # si se pide un csv pero el destino no lo incluye, se guarda en los dos
        if destino is None and not nueva.usa_csv:
            destino = "ambos"

    if destino is not None:
        nueva.destino = destino

    return nueva


def guardar_csv(configuracion, salida, ruta_csv, informar):
    # escribe el csv, devuelve false si no se pudo
    try:
        resultado = exportar_csv(salida, ruta_csv, configuracion.archivo_resultados, configuracion.archivo_csv)
    except (OSError, ValueError) as error:
        informar(f"No se pudo guardar el CSV en {ruta_csv}: {error}")
        return False

    if resultado is not None:
        informar(resultado.texto())

    return True


def ejecutar(configuracion, entrada, salida, omitir, informar=print):
    # paso 1, revisar que no falte nada
    config.validar(configuracion)

    if not entrada.is_dir():
        raise ValueError(f"No existe la carpeta de PDF: {entrada}")

    informar(configuracion.descripcion)

    # paso 2, comprobar el csv antes de gastar en ia
    ruta_csv = resolver_ruta(configuracion.csv_ruta, salida, configuracion.archivo_csv)

    if configuracion.usa_csv:
        comprobar_destino(ruta_csv)
        informar(f"Archivo CSV: {ruta_csv}")

    # paso 3, preparar la conexion con la ia
    extractor = ExtractorIA(
        configuracion.proveedor,
        configuracion.modelo,
        configuracion.api_key,
        configuracion.url_base,
        timeout=configuracion.tiempo_espera,
        reintentos=configuracion.reintentos,
        instrucciones=configuracion.instrucciones,
        solicitud=configuracion.solicitud,
    )

    # paso 4, elegir donde se revisa si una pagina ya esta guardada
    repositorio_sql = None

    if configuracion.usa_sql:
        from sql import RepositorioSql

        informar("Conectando con SQL Server...")
        repositorio_sql = RepositorioSql(
            configuracion.sql_servidor,
            configuracion.sql_base_datos,
            configuracion.sql_usuario,
            configuracion.sql_clave,
        )
        repositorio_sql.conectar()
        informar("SQL Server conectado.")
        repositorio = repositorio_sql
    else:
        repositorio = RepositorioLocal(salida, configuracion.archivo_resultados)

    # paso 5, procesar los pdf
    resumen = None

    try:
        resumen = procesar(
            entrada,
            salida,
            extractor,
            repositorio,
            dpi=configuracion.dpi,
            omitir_existentes=omitir,
            concurrencia=configuracion.concurrencia,
            informar=informar,
            archivo_resultados=configuracion.archivo_resultados,
            archivo_excluidos=configuracion.archivo_excluidos,
            archivo_errores=configuracion.archivo_errores,
            archivo_indice=configuracion.archivo_indice,
        )
        return resumen

    finally:
        # paso 6, cerrar sql server y guardar el csv, se hace siempre aunque el proceso falle a la mitad
        if repositorio_sql is not None:
            repositorio_sql.cerrar()

        if configuracion.usa_csv:
            se_guardo = guardar_csv(configuracion, salida, ruta_csv, informar)
            if not se_guardo and resumen is not None:
                resumen.errores = resumen.errores + 1


def modo_consola(entrada, salida, destino, ruta_csv):
    # ejecucion normal desde la terminal, devuelve 0 si todo salio bien y 1 si hubo errores
    try:
        configuracion = config.cargar()
        configuracion = elegir_destino(configuracion, destino, ruta_csv)

        configuracion.entrada.mkdir(parents=True, exist_ok=True)

        if entrada is None:
            entrada = configuracion.entrada
        if salida is None:
            salida = configuracion.salida

        resumen = ejecutar(configuracion, entrada, salida, configuracion.omitir_existentes)

    except Exception as error:
        print(f"\nERROR: {error}", file=sys.stderr)
        return 1

    print("\n" + resumen.texto())

    if resumen.errores > 0:
        return 1

    return 0


class Ventana:
    # interfaz grafica, hace lo mismo que la consola

    def __init__(self):
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk

        self.tk = tk
        self.filedialog = filedialog
        self.messagebox = messagebox

        self.ventana = tk.Tk()
        self.ventana.title("OCR de transferencias")
        self.ventana.geometry("860x640")
        self.ventana.minsize(720, 540)

        # mensajes que manda el hilo de trabajo para mostrarlos en la ventana
        self.eventos = queue.Queue()

        # valores iniciales desde el .env
        try:
            configuracion = config.cargar()
            configuracion.entrada.mkdir(parents=True, exist_ok=True)
            carpeta_entrada = configuracion.entrada
            carpeta_salida = configuracion.salida
            ruta_csv = configuracion.csv_ruta or ""
            destino = configuracion.destino
            omitir = configuracion.omitir_existentes
            estado = configuracion.descripcion
            self.nombre_csv = configuracion.archivo_csv
        except (ValueError, OSError) as error:
            carpeta_entrada = config.BASE / "pdfs"
            carpeta_salida = config.BASE / "salida"
            ruta_csv = ""
            destino = "csv"
            omitir = True
            estado = str(error)
            self.nombre_csv = "resultados.csv"

        self.entrada = tk.StringVar(value=str(carpeta_entrada))
        self.salida = tk.StringVar(value=str(carpeta_salida))
        self.archivo_csv = tk.StringVar(value=str(ruta_csv))
        self.destino = tk.StringVar(value=DESTINOS_VENTANA[destino])
        self.omitir = tk.BooleanVar(value=omitir)
        self.estado = tk.StringVar(value=estado)

        self.armar(ttk)

    def armar(self, ttk):
        # coloca todos los controles en la ventana
        marco = ttk.Frame(self.ventana, padding=16)
        marco.grid(sticky="nsew")
        self.ventana.rowconfigure(0, weight=1)
        self.ventana.columnconfigure(0, weight=1)
        marco.columnconfigure(1, weight=1)
        marco.rowconfigure(6, weight=1)

        # carpeta de pdf
        ttk.Label(marco, text="Carpeta de PDF").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(marco, textvariable=self.entrada).grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Button(marco, text="Examinar", command=self.elegir_entrada).grid(row=0, column=2, padx=(8, 0), pady=4)

        # carpeta de salida
        ttk.Label(marco, text="Carpeta de salida").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(marco, textvariable=self.salida).grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Button(marco, text="Examinar", command=self.elegir_salida).grid(row=1, column=2, padx=(8, 0), pady=4)

        # archivo csv
        ttk.Label(marco, text="Archivo CSV").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(marco, textvariable=self.archivo_csv).grid(row=2, column=1, sticky="ew", pady=4)
        ttk.Button(marco, text="Examinar", command=self.elegir_csv).grid(row=2, column=2, padx=(8, 0), pady=4)

        # destino
        ttk.Label(marco, text="Guardar en").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=4)
        opciones = ttk.Frame(marco)
        opciones.grid(row=3, column=1, columnspan=2, sticky="ew", pady=4)
        selector = ttk.Combobox(
            opciones,
            textvariable=self.destino,
            values=list(DESTINOS_VENTANA.values()),
            state="readonly",
            width=26,
        )
        selector.grid(row=0, column=0)
        aviso = f"Si deja vacío el archivo CSV, se usa {self.nombre_csv} en la carpeta de salida."
        ttk.Label(opciones, text=aviso).grid(row=0, column=1, sticky="w", padx=(12, 0))

        # omitir paginas ya procesadas
        casilla = ttk.Checkbutton(marco, text="Omitir páginas ya procesadas", variable=self.omitir)
        casilla.grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 2))
        ttk.Label(marco, textvariable=self.estado).grid(row=5, column=0, columnspan=3, sticky="w", pady=(0, 8))

        # registro de mensajes
        self.registro = self.tk.Text(marco, wrap="word", state="disabled", font=("Consolas", 10))
        self.registro.grid(row=6, column=0, columnspan=2, sticky="nsew")
        barra = ttk.Scrollbar(marco, orient="vertical", command=self.registro.yview)
        barra.grid(row=6, column=2, sticky="nsw")
        self.registro.configure(yscrollcommand=barra.set)

        # barra de progreso y boton
        inferior = ttk.Frame(marco)
        inferior.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        inferior.columnconfigure(0, weight=1)
        self.progreso = ttk.Progressbar(inferior, mode="indeterminate")
        self.progreso.grid(row=0, column=0, sticky="ew", padx=(0, 12))
        self.boton = ttk.Button(inferior, text="Procesar", command=self.iniciar)
        self.boton.grid(row=0, column=1)

    def elegir_carpeta(self, variable):
        seleccion = self.filedialog.askdirectory(initialdir=variable.get() or str(config.BASE))
        if seleccion:
            variable.set(seleccion)

    def elegir_entrada(self):
        self.elegir_carpeta(self.entrada)

    def elegir_salida(self):
        self.elegir_carpeta(self.salida)

    def elegir_csv(self):
        actual = self.archivo_csv.get().strip()
        if actual == "":
            carpeta = self.salida.get().strip() or str(config.BASE)
            actual = str(Path(carpeta) / self.nombre_csv)
        actual = Path(actual)

        seleccion = self.filedialog.asksaveasfilename(
            initialdir=str(actual.parent),
            initialfile=actual.name,
            defaultextension=".csv",
            filetypes=[("Archivo CSV", "*.csv")],
            confirmoverwrite=False,
        )
        if seleccion:
            self.archivo_csv.set(seleccion)

    def escribir(self, mensaje):
        # agrega una linea al registro de mensajes
        self.registro.configure(state="normal")
        self.registro.insert("end", mensaje + "\n")
        self.registro.see("end")
        self.registro.configure(state="disabled")

    def informar_desde_hilo(self, mensaje):
        # el hilo de trabajo no puede tocar la ventana, deja el mensaje en la cola
        self.eventos.put(("log", mensaje))

    def trabajar(self, configuracion, entrada, salida, omitir):
        # corre en un hilo aparte para que la ventana no se congele
        try:
            resumen = ejecutar(configuracion, entrada, salida, omitir, self.informar_desde_hilo)
            self.eventos.put(("fin", resumen.texto()))
        except Exception as error:
            self.eventos.put(("error", str(error)))

    def iniciar(self):
        # se ejecuta al pulsar procesar
        try:
            configuracion = config.cargar()

            codigo_destino = "csv"
            for codigo in DESTINOS_VENTANA:
                if DESTINOS_VENTANA[codigo] == self.destino.get():
                    codigo_destino = codigo

            configuracion = copy.copy(configuracion)
            configuracion.destino = codigo_destino

            ruta_csv = self.archivo_csv.get().strip()
            if ruta_csv != "":
                configuracion.csv_ruta = Path(ruta_csv).expanduser().resolve()
            else:
                configuracion.csv_ruta = None

            config.validar(configuracion)

        except ValueError as error:
            self.estado.set(str(error))
            self.messagebox.showerror("Configuración", f"{error}\n\nEdite el archivo .env y vuelva a intentarlo.")
            return

        self.estado.set(configuracion.descripcion)
        self.boton.configure(state="disabled")
        self.progreso.start(10)

        entrada = Path(self.entrada.get().strip())
        salida = Path(self.salida.get().strip())
        hilo = threading.Thread(
            target=self.trabajar,
            args=(configuracion, entrada, salida, self.omitir.get()),
            daemon=True,
        )
        hilo.start()

    def leer_eventos(self):
        # cada 100 milisegundos muestra los mensajes que dejo el hilo de trabajo
        while not self.eventos.empty():
            tipo, valor = self.eventos.get_nowait()
            self.escribir(valor)

            if tipo != "log":
                self.progreso.stop()
                self.boton.configure(state="normal")

                if tipo == "fin":
                    self.messagebox.showinfo("Resultado", valor)
                else:
                    self.messagebox.showerror("Resultado", valor)

        self.ventana.after(100, self.leer_eventos)

    def mostrar(self):
        self.ventana.after(100, self.leer_eventos)
        self.ventana.mainloop()


def modo_ventana():
    ventana = Ventana()
    ventana.mostrar()
    return 0


def principal():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    lector = argparse.ArgumentParser(description="Extracción de comprobantes de transferencia con IA.")
    lector.add_argument("--ventana", action="store_true", help="abre la interfaz gráfica")
    lector.add_argument("--entrada", type=Path, help="carpeta de PDF")
    lector.add_argument("--salida", type=Path, help="carpeta de resultados (imágenes, historial y registros)")
    lector.add_argument("--destino", choices=config.DESTINOS, help="dónde guardar: csv, sql o ambos (si no se indica, lo que diga .env)")
    lector.add_argument("--csv", type=Path, metavar="RUTA", help="archivo CSV donde guardar (si no se indica, csv_ruta de .env)")
    argumentos = lector.parse_args()

    env_nuevo = preparar_entorno()

    if argumentos.ventana:
        return modo_ventana()

    if env_nuevo:
        print(f"Se creó el archivo {config.BASE / '.env'}.")
        print("Complete allí la clave de la IA (por ejemplo gemini_clave) y vuelva a ejecutar: python main.py")
        return 1

    return modo_consola(argumentos.entrada, argumentos.salida, argumentos.destino, argumentos.csv)


if __name__ == "__main__":
    sys.exit(principal())
