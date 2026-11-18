"""Genera datos_demo/catalogo_demo.xlsx: catálogo para la demo (se carga con el importador de Excel).

Usa las mismas columnas que aceptaba el importador del monolito GestPro.
Uso:  python scripts/generar_catalogo_demo.py
"""
from pathlib import Path

import openpyxl

# (nombre, categoría, precio venta, costo, cantidad, stock mínimo)
PRODUCTOS = [
    ("Pilas AA x4", "General", 8500, 5200, 40, 5),
    ("Bombillo LED 9W", "General", 6900, 3800, 35, 5),
    ("Cinta adhesiva", "General", 3500, 1800, 50, 8),
    ("Encendedor", "General", 2000, 900, 60, 10),
    ("Audífonos Bluetooth", "Electrónica", 89900, 52000, 7, 5),   # casi en el mínimo
    ("Cargador USB-C 20W", "Electrónica", 39900, 21000, 20, 4),
    ("Cable USB-C 1m", "Electrónica", 14900, 6500, 30, 6),
    ("Mouse inalámbrico", "Electrónica", 45900, 24000, 15, 4),
    ("Camiseta básica", "Ropa", 29900, 14000, 40, 6),
    ("Medias deportivas x3", "Ropa", 19900, 8500, 35, 6),
    ("Gorra", "Ropa", 24900, 11000, 25, 5),
    ("Jean clásico", "Ropa", 89900, 45000, 18, 4),
    ("Arroz 1kg", "Alimentos", 5200, 3900, 60, 15),
    ("Café molido 500g", "Alimentos", 18900, 12500, 6, 5),        # casi en el mínimo
    ("Panela 500g", "Alimentos", 3800, 2600, 45, 10),
    ("Aceite 1L", "Alimentos", 12900, 9800, 30, 8),
    ("Atún en lata", "Alimentos", 7900, 5200, 40, 10),
    ("Detergente 1kg", "Hogar", 14500, 9900, 25, 6),
    ("Jabón de loza", "Hogar", 6500, 3900, 30, 8),
    ("Escoba", "Hogar", 15900, 8500, 12, 3),
    ("Toalla de manos", "Hogar", 12900, 6500, 20, 5),
    ("Balón de fútbol", "Deportes", 59900, 32000, 10, 3),
    ("Botella deportiva", "Deportes", 19900, 9000, 25, 5),
    ("Lazo para saltar", "Deportes", 15900, 6000, 18, 4),
    ("Cuaderno 100 hojas", "Libros", 6900, 3500, 50, 10),
    ("Agenda 2027", "Libros", 24900, 12000, 20, 4),
    ("Novela de bolsillo", "Libros", 32900, 18000, 15, 3),
    ("Gaseosa 1.5L", "Bebidas", 6500, 4200, 48, 12),
    ("Agua 600ml", "Bebidas", 2500, 1200, 72, 18),
    ("Jugo 1L", "Bebidas", 7500, 4800, 36, 10),
    ("Cerveza lata", "Bebidas", 3800, 2500, 96, 24),
    ("Carro de juguete", "Juguetes", 24900, 12000, 14, 3),
    ("Rompecabezas 500 piezas", "Juguetes", 39900, 21000, 10, 3),
    ("Plastilina x12", "Juguetes", 9900, 4800, 25, 5),
    ("Acetaminofén x10", "Salud", 4500, 2200, 40, 10),
    ("Alcohol 350ml", "Salud", 7900, 4500, 30, 8),
    ("Tapabocas x10", "Salud", 8900, 3900, 35, 8),
    ("Crema dental", "Salud", 6900, 4100, 40, 10),
    ("Vitamina C x30", "Salud", 22900, 12500, 15, 4),
    ("Termo 1L", "Hogar", 34900, 18000, 12, 3),
]


def main():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Inventario"
    ws.append(["Nombre", "Categoría", "PrecioVenta", "PrecioCompra", "Cantidad",
               "StockMinimo", "Proveedor", "Unidad"])
    for nombre, cat, precio, costo, cant, minimo in PRODUCTOS:
        ws.append([nombre, cat, precio, costo, cant, minimo, "Distribuidora Demo", "unidad"])
    destino = Path(__file__).resolve().parent.parent / "datos_demo" / "catalogo_demo.xlsx"
    destino.parent.mkdir(exist_ok=True)
    wb.save(destino)
    print(f"Catálogo generado: {destino} ({len(PRODUCTOS)} productos)")


if __name__ == "__main__":
    main()
