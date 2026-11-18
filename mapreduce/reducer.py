"""REDUCE: agrupa por clave (sede|categoria) y calcula el consolidado.

Para poder repartir el trabajo, cada worker no envía todos los valores sino un resumen
parcial por clave (combiner): (n, suma, suma_de_cuadrados). Con eso el reducer obtiene
exactamente el promedio y la desviación estándar sin volver a leer los datos:

    promedio = suma / n
    varianza = (suma_cuadrados - n * promedio²) / (n - 1)

También funciona como en el Lab 2 (entrada ordenada "clave<TAB>valor"):
    cat ventas.csv | python mapper.py | sort | python reducer.py
"""
import math
import sys


def combinar(parciales):
    """Suma los resúmenes parciales (n, suma, suma_cuadrados) de varios workers."""
    n = s = sq = 0.0
    for pn, ps, psq in parciales:
        n, s, sq = n + pn, s + ps, sq + psq
    return n, s, sq


def resultado(clave: str, n: float, suma: float, suma_cuadrados: float) -> dict:
    sede, categoria = clave.split("|", 1)
    promedio = suma / n
    varianza = (suma_cuadrados - n * promedio ** 2) / (n - 1) if n > 1 else 0.0
    return {"sede": sede, "categoria": categoria, "lineas": int(n), "ingresos": round(suma, 2),
            "ticket_promedio": round(promedio, 2), "desviacion": round(math.sqrt(max(varianza, 0.0)), 2)}


if __name__ == "__main__":
    actual, n, s, sq = None, 0, 0.0, 0.0

    def emitir():
        r = resultado(actual, n, s, sq)
        print(f"{r['sede']}\t{r['categoria']}\tlineas={r['lineas']}\tingresos={r['ingresos']:.0f}\t"
              f"promedio={r['ticket_promedio']:.2f}\tdesviacion={r['desviacion']:.2f}")

    for linea in sys.stdin:
        clave, valor = linea.rstrip("\n").split("\t")
        valor = float(valor)
        if clave != actual:
            if actual is not None:
                emitir()
            actual, n, s, sq = clave, 0, 0.0, 0.0
        n, s, sq = n + 1, s + valor, sq + valor * valor
    if actual is not None:
        emitir()
