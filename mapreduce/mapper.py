"""MAP: una línea cruda (bronze) -> par (clave, valor) validado.

clave = "sede|categoria"      valor = total de la línea de venta (COP)

Las filas inválidas se descartan aquí (bronze -> silver): sede o categoría vacías,
cantidades o totales no numéricos o <= 0, número de columnas incorrecto.

También funciona como en el Lab 2, leyendo de la entrada estándar:
    cat ventas.csv | python mapper.py | sort | python reducer.py
"""
import csv
import sys

COLUMNAS = 10


def map_line(linea: str):
    """Devuelve (clave, valor, campos_limpios) o None si la fila no es válida."""
    linea = linea.rstrip("\r\n")
    campos = next(csv.reader([linea])) if '"' in linea else linea.split(",")
    if len(campos) != COLUMNAS or campos[0] == "sale_id":
        return None
    sale_id, fecha, sede, categoria, producto, cantidad, precio, total, metodo, origen = campos
    sede, categoria = sede.strip(), categoria.strip()
    if not sede or not categoria or sede == "N/A":
        return None
    try:
        cantidad, precio, total = int(cantidad), float(precio), float(total)
    except ValueError:
        return None
    if cantidad <= 0 or total <= 0:
        return None
    return f"{sede}|{categoria}", total, [sale_id, fecha, sede, categoria, producto.strip(), cantidad,
                                          precio, total, metodo, origen]


if __name__ == "__main__":
    for linea in sys.stdin:
        r = map_line(linea)
        if r:
            print(f"{r[0]}\t{r[1]}")
