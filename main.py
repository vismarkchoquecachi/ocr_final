# ocr de comprobantes de transferencia
# toda la configuracion, las credenciales y el prompt estan en .env
# python main.py procesa los pdf, python main.py --ventana abre la interfaz
import argparse
import queue
import shutil
import sys
import threading
from dataclasses import replace
from pathlib import Path

try:
    import config
    from archivo_csv import comprobar_destino, exportar_csv, resolver_ruta
    from ia import ExtractorIA
    from procesamiento import RepositorioLocal, Resumen, procesar
except ModuleNotFoundError as error:
    sys.exit(
        f"Falta el paquete de Python '{error.name}'. Instale las dependencias con:\n"
        "    python -m pip install -r requirements.txt"
    )

DESTINOS_VENTANA = {"csv": "Archivo CSV", "sql": "SQL Server", "ambos": "SQL Server y archivo CSV"}


def preparar_entorno() -> bool:
    # crea el archivo .env si falta, devuelve true si lo acaba de crear
    env, ejemplo = config.BASE / ".env", config.BASE / ".env.example"
    if env.exists() or not ejemplo.exists():
        return False
    shutil.copyfile(ejemplo, env)
    return True


def elegir_destino(cfg: config.Configuracion, destino: str | None, ruta_csv: Path | None) -> config.Configuracion:
    # el destino y el csv elegidos al ejecutar tienen prioridad sobre .env
    if ruta_csv is not None:
        cfg = replace(cfg, csv_ruta=ruta_csv.expanduser().resolve())
        if destino is None and not cfg.usa_csv:
            destino = "ambos"
    return replace(cfg, destino=destino) if destino else cfg


def guardar_csv(cfg: config.Configuracion, salida: Path, ruta_csv: Path, informar) -> bool:
    try:
        resultado = exportar_csv(salida, ruta_csv, cfg.archivo_resultados, cfg.archivo_csv)
    except (OSError, ValueError) as error:
        informar(f"No se pudo guardar el CSV en {ruta_csv}: {error}")
        return False
    if resultado:
        informar(resultado.texto())
    return True


def ejecutar(cfg: config.Configuracion, entrada: Path, salida: Path, omitir: bool, informar=print) -> Resumen:
    config.validar(cfg)
    if not entrada.is_dir():
        raise ValueError(f"No existe la carpeta de PDF: {entrada}")
    informar(cfg.descripcion)

    # se comprueba el destino antes de procesar para no gastar en ia si luego no se puede guardar
    ruta_csv = resolver_ruta(cfg.csv_ruta, salida, cfg.archivo_csv)
    if cfg.usa_csv:
        comprobar_destino(ruta_csv)
        informar(f"Archivo CSV: {ruta_csv}")

    extractor = ExtractorIA(
        cfg.proveedor,
        cfg.modelo,
        cfg.api_key,
        cfg.url_base,
        timeout=cfg.tiempo_espera,
        reintentos=cfg.reintentos,
        instrucciones=cfg.instrucciones,
        solicitud=cfg.solicitud,
    )
    argumentos = dict(
        entrada=entrada,
        salida=salida,
        extractor=extractor,
        dpi=cfg.dpi,
        omitir_existentes=omitir,
        concurrencia=cfg.concurrencia,
        informar=informar,
        archivo_resultados=cfg.archivo_resultados,
        archivo_excluidos=cfg.archivo_excluidos,
        archivo_errores=cfg.archivo_errores,
    )
    resumen = None
    try:
        if not cfg.usa_sql:
            resumen = procesar(repositorio=RepositorioLocal(salida, cfg.archivo_resultados), **argumentos)
        else:
            from sql import RepositorioSql

            informar("Conectando con SQL Server...")
            with RepositorioSql(cfg.sql_servidor, cfg.sql_base_datos, cfg.sql_usuario, cfg.sql_clave) as repositorio:
                informar("SQL Server conectado.")
                resumen = procesar(repositorio=repositorio, **argumentos)
        return resumen
    finally:
        # el csv se guarda siempre, tambien si el proceso fallo a mitad de camino
        if cfg.usa_csv and not guardar_csv(cfg, salida, ruta_csv, informar) and resumen is not None:
            resumen.errores += 1


def modo_consola(entrada: Path | None, salida: Path | None, destino: str | None, ruta_csv: Path | None) -> int:
    try:
        cfg = elegir_destino(config.cargar(), destino, ruta_csv)
        cfg.entrada.mkdir(parents=True, exist_ok=True)
        resumen = ejecutar(cfg, entrada or cfg.entrada, salida or cfg.salida, cfg.omitir_existentes)
    except Exception as error:
        print(f"\nERROR: {error}", file=sys.stderr)
        return 1
    print("\n" + resumen.texto())
    return 1 if resumen.errores else 0


def modo_ventana() -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    ventana = tk.Tk()
    ventana.title("OCR de transferencias")
    ventana.geometry("860x640")
    ventana.minsize(720, 540)
    eventos: queue.Queue = queue.Queue()

    try:
        cfg = config.cargar()
        cfg.entrada.mkdir(parents=True, exist_ok=True)
        valores = (cfg.entrada, cfg.salida, cfg.csv_ruta or "", cfg.destino, cfg.omitir_existentes, cfg.descripcion)
        nombre_csv = cfg.archivo_csv
    except (ValueError, OSError) as error:
        valores = (config.BASE / "pdfs", config.BASE / "salida", "", "csv", True, str(error))
        nombre_csv = "resultados.csv"
    entrada, salida = tk.StringVar(value=str(valores[0])), tk.StringVar(value=str(valores[1]))
    archivo_csv, destino = tk.StringVar(value=str(valores[2])), tk.StringVar(value=DESTINOS_VENTANA[valores[3]])
    omitir, estado = tk.BooleanVar(value=valores[4]), tk.StringVar(value=valores[5])

    marco = ttk.Frame(ventana, padding=16)
    marco.grid(sticky="nsew")
    ventana.rowconfigure(0, weight=1)
    ventana.columnconfigure(0, weight=1)
    marco.columnconfigure(1, weight=1)
    marco.rowconfigure(6, weight=1)

    def elegir_carpeta(variable: tk.StringVar) -> None:
        seleccion = filedialog.askdirectory(initialdir=variable.get() or str(config.BASE))
        if seleccion:
            variable.set(seleccion)

    def elegir_csv(variable: tk.StringVar) -> None:
        actual = Path(variable.get().strip() or Path(salida.get().strip() or config.BASE) / nombre_csv)
        seleccion = filedialog.asksaveasfilename(
            initialdir=str(actual.parent),
            initialfile=actual.name,
            defaultextension=".csv",
            filetypes=[("Archivo CSV", "*.csv")],
            confirmoverwrite=False,
        )
        if seleccion:
            variable.set(seleccion)

    campos = (
        ("Carpeta de PDF", entrada, elegir_carpeta),
        ("Carpeta de salida", salida, elegir_carpeta),
        ("Archivo CSV", archivo_csv, elegir_csv),
    )
    for fila, (texto, variable, elegir) in enumerate(campos):
        ttk.Label(marco, text=texto).grid(row=fila, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(marco, textvariable=variable).grid(row=fila, column=1, sticky="ew", pady=4)
        ttk.Button(marco, text="Examinar", command=lambda v=variable, e=elegir: e(v)).grid(row=fila, column=2, padx=(8, 0), pady=4)

    ttk.Label(marco, text="Guardar en").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=4)
    opciones = ttk.Frame(marco)
    opciones.grid(row=3, column=1, columnspan=2, sticky="ew", pady=4)
    ttk.Combobox(opciones, textvariable=destino, values=list(DESTINOS_VENTANA.values()), state="readonly", width=26).grid(row=0, column=0)
    ttk.Label(opciones, text=f"Si deja vacío el archivo CSV, se usa {nombre_csv} en la carpeta de salida.").grid(
        row=0, column=1, sticky="w", padx=(12, 0)
    )

    ttk.Checkbutton(marco, text="Omitir páginas ya procesadas", variable=omitir).grid(
        row=4, column=0, columnspan=3, sticky="w", pady=(8, 2)
    )
    ttk.Label(marco, textvariable=estado).grid(row=5, column=0, columnspan=3, sticky="w", pady=(0, 8))

    registro = tk.Text(marco, wrap="word", state="disabled", font=("Consolas", 10))
    registro.grid(row=6, column=0, columnspan=2, sticky="nsew")
    barra = ttk.Scrollbar(marco, orient="vertical", command=registro.yview)
    barra.grid(row=6, column=2, sticky="nsw")
    registro.configure(yscrollcommand=barra.set)

    inferior = ttk.Frame(marco)
    inferior.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(12, 0))
    inferior.columnconfigure(0, weight=1)
    progreso = ttk.Progressbar(inferior, mode="indeterminate")
    progreso.grid(row=0, column=0, sticky="ew", padx=(0, 12))
    boton = ttk.Button(inferior, text="Procesar")
    boton.grid(row=0, column=1)

    def escribir(mensaje: str) -> None:
        registro.configure(state="normal")
        registro.insert("end", mensaje + "\n")
        registro.see("end")
        registro.configure(state="disabled")

    def trabajar(cfg, ruta_entrada, ruta_salida, omitir_existentes) -> None:
        try:
            resumen = ejecutar(cfg, ruta_entrada, ruta_salida, omitir_existentes, lambda m: eventos.put(("log", m)))
            eventos.put(("fin", resumen.texto()))
        except Exception as error:
            eventos.put(("error", str(error)))

    def iniciar() -> None:
        try:
            cfg = config.cargar()
            ruta_csv = archivo_csv.get().strip()
            codigo = next(codigo for codigo, texto in DESTINOS_VENTANA.items() if texto == destino.get())
            cfg = replace(cfg, destino=codigo, csv_ruta=Path(ruta_csv).expanduser().resolve() if ruta_csv else None)
            config.validar(cfg)
        except ValueError as error:
            estado.set(str(error))
            messagebox.showerror("Configuración", f"{error}\n\nEdite el archivo .env y vuelva a intentarlo.")
            return
        estado.set(cfg.descripcion)
        boton.configure(state="disabled")
        progreso.start(10)
        argumentos = (cfg, Path(entrada.get().strip()), Path(salida.get().strip()), omitir.get())
        threading.Thread(target=trabajar, args=argumentos, daemon=True).start()

    def leer_eventos() -> None:
        while not eventos.empty():
            tipo, valor = eventos.get_nowait()
            escribir(valor)
            if tipo != "log":
                progreso.stop()
                boton.configure(state="normal")
                (messagebox.showinfo if tipo == "fin" else messagebox.showerror)("Resultado", valor)
        ventana.after(100, leer_eventos)

    boton.configure(command=iniciar)
    ventana.after(100, leer_eventos)
    ventana.mainloop()
    return 0


def principal() -> int:
    for flujo in (sys.stdout, sys.stderr):
        if hasattr(flujo, "reconfigure"):
            flujo.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Extracción de comprobantes de transferencia con IA.")
    parser.add_argument("--ventana", action="store_true", help="abre la interfaz gráfica")
    parser.add_argument("--entrada", type=Path, help="carpeta de PDF")
    parser.add_argument("--salida", type=Path, help="carpeta de resultados (imágenes, historial y registros)")
    parser.add_argument("--destino", choices=config.DESTINOS, help="dónde guardar: csv, sql o ambos (si no se indica, lo que diga .env)")
    parser.add_argument("--csv", type=Path, metavar="RUTA", help="archivo CSV donde guardar (si no se indica, csv_ruta de .env)")
    argumentos = parser.parse_args()

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
