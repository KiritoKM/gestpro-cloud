"""Genera ventas sintéticas (capa BRONZE) con el mismo formato que GET /api/sales/export.

Una fila = una línea de venta:
    sale_id,fecha,sede,categoria,producto,cantidad,precio_unitario,total_linea,metodo_pago,origen

- Usa el catálogo real de la demo (catalogo_demo.xlsx): mismos productos, categorías y precios.
- Cada sede tiene preferencias distintas por categoría, para que el consolidado sea interesante.
- ~1 % de filas "sucias" a propósito (sede vacía, valores negativos, campos faltantes...):
  el mapper las descarta al pasar de bronze a silver (calidad de datos).
- Se reparte en varios archivos CSV (entregable del Lab 2: múltiples archivos de entrada).

Uso:  python generate_sales.py --rows 2000000 --files 8 --out /data/bronze
"""
import argparse
import random
from datetime import date, timedelta
from pathlib import Path

import openpyxl

ENCABEZADO = "sale_id,fecha,sede,categoria,producto,cantidad,precio_unitario,total_linea,metodo_pago,origen\n"
SEDES = {"Principal": 0.45, "Norte": 0.30, "Sur": 0.25}
# Peso extra de cada categoría por sede (comportamiento de compra distinto)
PREFERENCIAS = {
    "Principal": {"Alimentos": 2.0, "Bebidas": 2.0, "Hogar": 1.5},
    "Norte": {"Electrónica": 3.0, "Ropa": 2.0, "Deportes": 1.5},
    "Sur": {"Alimentos": 2.5, "Salud": 2.0, "Juguetes": 1.5},
}
METODOS = (["efectivo"] * 55) + (["tarjeta"] * 25) + (["transferencia"] * 12) + (["credito"] * 8)


def cargar_catalogo(ruta: Path):
    filas = openpyxl.load_workbook(ruta, read_only=True).active.iter_rows(values_only=True)
    encabezado = next(filas)
    i_nombre, i_cat, i_precio = (encabezado.index(c) for c in ("Nombre", "Categoría", "PrecioVenta"))
    return [(f[i_nombre], f[i_cat], float(f[i_precio])) for f in filas if f[i_nombre]]


def fila_sucia(rng: random.Random, sale_id: int, fecha: str) -> str:
    tipo = rng.randrange(5)
    if tipo == 0:
        return f"{sale_id},{fecha},,General,Producto,1,1000,1000,efectivo,sintetico\n"            # sin sede
    if tipo == 1:
        return f"{sale_id},{fecha},Norte,,Producto,1,1000,1000,efectivo,sintetico\n"             # sin categoría
    if tipo == 2:
        return f"{sale_id},{fecha},Sur,Bebidas,Agua 600ml,2,2500,-5000,efectivo,sintetico\n"     # total negativo
    if tipo == 3:
        return f"{sale_id},{fecha},Principal,Hogar,Escoba,dos,15900\n"                          # campos faltantes
    return f"{sale_id},{fecha},N/A,Ropa,Gorra,abc,24900,xx,tarjeta,sintetico\n"                  # valores inválidos


def generar(salida: Path, filas: int, archivos: int, catalogo: Path, semilla: int = 2026):
    rng = random.Random(semilla)
    productos = cargar_catalogo(catalogo)
    sedes, pesos_sede = list(SEDES), list(SEDES.values())
    pesos = {s: [PREFERENCIAS[s].get(cat, 1.0) for _, cat, _ in productos] for s in sedes}
    hoy = date.today()
    fechas = [(hoy - timedelta(days=d)).isoformat() for d in range(365)]
    salida.mkdir(parents=True, exist_ok=True)
    por_archivo = filas // archivos
    sale_id, escritas = 10_000_000, 0
    for n in range(1, archivos + 1):
        objetivo = por_archivo if n < archivos else filas - escritas
        temporal = salida / f".ventas_sinteticas_{n:02d}.csv.tmp"
        with open(temporal, "w", encoding="utf-8", newline="") as f:
            f.write(ENCABEZADO)
            hechas, buffer = 0, []
            while hechas < objetivo:
                sale_id += 1
                sede = rng.choices(sedes, pesos_sede)[0]
                fecha = rng.choice(fechas)
                metodo = rng.choice(METODOS)
                for _ in range(min(rng.randint(1, 4), objetivo - hechas)):
                    if rng.random() < 0.01:
                        buffer.append(fila_sucia(rng, sale_id, fecha))
                    else:
                        nombre, cat, precio = rng.choices(productos, pesos[sede])[0]
                        cant = rng.randint(1, 6 if cat in ("Alimentos", "Bebidas") else 3)
                        buffer.append(f"{sale_id},{fecha},{sede},{cat},{nombre},{cant},{precio:.0f},"
                                      f"{precio * cant:.0f},{metodo},sintetico\n")
                    hechas += 1
                if len(buffer) >= 50_000:
                    f.writelines(buffer)
                    buffer.clear()
            f.writelines(buffer)
        temporal.replace(salida / f"ventas_sinteticas_{n:02d}.csv")   # el archivo aparece completo o no aparece
        escritas += objetivo
    return escritas


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--rows", type=int, default=2_000_000)
    p.add_argument("--files", type=int, default=8)
    p.add_argument("--out", default="/data/bronze")
    p.add_argument("--catalog", default=str(Path(__file__).with_name("catalogo_demo.xlsx")))
    a = p.parse_args()
    total = generar(Path(a.out), a.rows, a.files, Path(a.catalog))
    print(f"{total:,} líneas sintéticas en {a.files} archivos -> {a.out}")
