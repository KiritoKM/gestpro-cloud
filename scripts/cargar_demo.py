"""Deja la plataforma lista para la demo: sedes, catálogo en cada sede (por Excel) y clientes.
Es idempotente: se puede correr varias veces.
Uso:  python scripts/cargar_demo.py [contraseña_admin]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prueba_humo import EXCEL, llamar, multipart  # noqa: E402

SEDES = [("Norte", "Calle 70 # 5-20"), ("Sur", "Carrera 100 # 16-45")]
CLIENTES = [
    {"name": "Laura Gómez", "doc_type": "CC", "document": "1144123456", "phone": "3001234567", "email": "laura.gomez@demo.co"},
    {"name": "Tienda Don Pepe SAS", "doc_type": "NIT", "document": "900123456", "phone": "6023334455", "email": "compras@donpepe.co"},
    {"name": "Carlos Ruiz", "doc_type": "CC", "document": "16789012", "phone": "3159876543", "email": "carlos.ruiz@demo.co"},
]


def main():
    clave = sys.argv[1] if len(sys.argv) > 1 else "admin123"
    s, login, _ = llamar("POST", "/api/auth/login", cuerpo={"username": "admin", "password": clave})
    if s != 200:
        sys.exit(f"No se pudo iniciar sesión como admin: {login}")
    token = login["access_token"]
    for nombre, direccion in SEDES:
        llamar("POST", "/api/inventory/locations", token, {"name": nombre, "address": direccion})
    _, sedes, _ = llamar("GET", "/api/inventory/locations", token)
    for sede in sedes:
        datos, tipo = multipart({"location_id": sede["id"]}, "file", EXCEL)
        _, r, _ = llamar("POST", "/api/inventory/products/import", token, datos=datos, tipo=tipo)
        print(f"Catálogo en {sede['name']}: {r}")
    s, existentes, _ = llamar("GET", "/api/clients", token)
    if s != 200 or not isinstance(existentes, list):
        sys.exit(f"clients-credits no responde ({s}: {existentes}). Corre .\\scripts\\revivir.ps1 y vuelve a correr este script.")
    nombres = {c["name"] for c in existentes}
    for c in CLIENTES:
        if c["name"] not in nombres:
            llamar("POST", "/api/clients", token, c)
    print(f"Clientes: {len(CLIENTES)} de demo listos")
    llamar("POST", "/api/auth/users", token, {"username": "cajero1", "password": "cajero123", "role": "vendedor"})
    print("Usuario vendedor: cajero1 / cajero123")


if __name__ == "__main__":
    main()
