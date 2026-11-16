# arma el prompt con las secciones escritas en .env
# para ver el prompt completo ejecutar python prompt.py
import os

# variables de .env en el orden en que la ia las lee
secciones = (
    "prompt_rol",
    "prompt_paso_1",
    "prompt_paso_2",
    "prompt_paso_3",
    "prompt_paso_4",
    "prompt_reglas_finales",
)

# variable de .env con la frase que va junto a cada imagen
variable_solicitud = "prompt_solicitud"


def leer(nombre: str) -> str:
    texto = os.getenv(nombre, "").strip()
    if not texto:
        raise ValueError(f"Falta {nombre} en .env. Copie esa sección desde .env.example.")
    return texto


def cargar() -> tuple[str, str]:
    instrucciones = "\n\n".join(leer(nombre) for nombre in secciones)
    return instrucciones, leer(variable_solicitud)


if __name__ == "__main__":
    import sys

    import config

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        configuracion = config.cargar()
    except ValueError as error:
        sys.exit(f"ERROR: {error}")
    print("=" * 30, "instrucciones (prompt de sistema)", "=" * 30)
    print(configuracion.instrucciones)
    print()
    print("=" * 30, "solicitud (va junto a cada imagen)", "=" * 30)
    print(configuracion.solicitud)
    print()
    print(f"Tamaño: {len(configuracion.instrucciones)} caracteres en {len(secciones)} secciones.")
