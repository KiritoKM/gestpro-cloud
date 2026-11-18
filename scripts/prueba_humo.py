"""Prueba de humo de punta a punta contra el gateway (http://localhost:8080).

Recorre el caso de uso estrella: login -> sedes -> importar Excel -> venta ->
evento en Redis -> stock descontado + alerta -> resumen CQRS -> export para MapReduce.
Uso:  python scripts/prueba_humo.py [contraseña_admin]     (solo librería estándar)
      Tras migrar GestPro.db, el admin usa la contraseña que tenía en el monolito.
"""
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

BASE = "http://localhost:8080"
EXCEL = Path(__file__).resolve().parent.parent / "datos_demo" / "catalogo_demo.xlsx"
fallos = 0


def llamar(metodo, ruta, token=None, cuerpo=None, datos=None, tipo="application/json"):
    headers = {"Content-Type": tipo}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if cuerpo is not None:
        datos = json.dumps(cuerpo).encode()
    req = urllib.request.Request(BASE + ruta, data=datos, method=metodo, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            texto = r.read().decode()
            return r.status, (json.loads(texto) if "json" in r.headers.get("Content-Type", "") else texto), r.headers
    except urllib.error.HTTPError as e:
        texto = e.read().decode()
        try:
            return e.code, json.loads(texto), e.headers
        except ValueError:
            return e.code, texto, e.headers


def check(nombre, condicion, detalle=""):
    global fallos
    print(f"  {'OK ' if condicion else 'FALLA'}  {nombre}  {detalle}")
    if not condicion:
        fallos += 1


def multipart(campos, archivo_campo, archivo):
    limite = uuid.uuid4().hex
    partes = []
    for k, v in campos.items():
        partes.append(f'--{limite}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    partes.append((f'--{limite}\r\nContent-Disposition: form-data; name="{archivo_campo}"; '
                   f'filename="{archivo.name}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()
                  + archivo.read_bytes() + b"\r\n")
    partes.append(f"--{limite}--\r\n".encode())
    return b"".join(partes), f"multipart/form-data; boundary={limite}"


def main():
    print("1. Seguridad en el gateway (HAProxy + JWT)")
    s, _, _ = llamar("GET", "/api/sales")
    check("sin token -> 401", s == 401)
    s, _, _ = llamar("GET", "/api/sales", token="xxx.yyy.zzz")
    check("token falso -> 401", s == 401)
    clave = sys.argv[1] if len(sys.argv) > 1 else "admin123"
    s, login, _ = llamar("POST", "/api/auth/login", cuerpo={"username": "admin", "password": clave})
    check("login admin", s == 200, f"rol={login.get('user', {}).get('role') if s == 200 else login}")
    token = login["access_token"]

    print("2. Sedes e importación de Excel (catalog-inventory)")
    for sede in ("Norte", "Sur"):
        llamar("POST", "/api/inventory/locations", token, {"name": sede})
    s, sedes, _ = llamar("GET", "/api/inventory/locations", token)
    check("sedes", s == 200 and len(sedes) >= 3, str([x["name"] for x in sedes]))
    principal = sedes[0]["id"]
    datos, tipo = multipart({"location_id": principal}, "file", EXCEL)
    s, res, _ = llamar("POST", "/api/inventory/products/import", token, datos=datos, tipo=tipo)
    check("importar Excel", s == 200, str(res))
    s, stock, _ = llamar("GET", f"/api/inventory/stock?location_id={principal}", token)
    productos = {p["name"]: p for p in stock["items"]}
    check("stock por sede", s == 200 and len(productos) >= 40, f"{len(productos)} productos")

    print("3. Venta en el POS -> evento sale.created en Redis")
    aud = productos["Audífonos Bluetooth"]
    if aud["stock"] < 3:   # en corridas repetidas, vender otro producto con stock
        aud = next(p for p in productos.values() if p["stock"] >= 3)
    s, venta, h = llamar("POST", "/api/sales", token, {
        "location_id": principal, "payment_method": "efectivo",
        "items": [{"product_id": aud["product_id"], "quantity": 3}]})
    check("venta registrada", s == 201, f"venta #{venta.get('id')} total={venta.get('total')} "
                                        f"atendida por {h.get('X-Replica')}")
    s, res, _ = llamar("POST", "/api/sales", token, {
        "location_id": principal, "items": [{"product_id": aud["product_id"], "quantity": 999}]})
    check("sin stock -> 409", s == 409, res.get("detail", "") if isinstance(res, dict) else "")
    s, res, _ = llamar("POST", "/api/sales", token, {
        "location_id": principal, "payment_method": "credito",
        "items": [{"product_id": aud["product_id"], "quantity": 1}]})
    check("crédito sin cliente -> 400", s == 400)

    print("4. Efectos asíncronos (consistencia eventual)")
    esperado = aud["stock"] - 3
    for _ in range(20):
        time.sleep(0.5)
        _, stock, _ = llamar("GET", f"/api/inventory/stock?location_id={principal}", token)
        actual = next(p["stock"] for p in stock["items"] if p["product_id"] == aud["product_id"])
        if actual == esperado:
            break
    check("stock descontado por el evento", actual == esperado, f"{aud['name']}: {aud['stock']} -> {actual}")
    if esperado <= aud["min_stock"]:
        _, alertas, _ = llamar("GET", f"/api/inventory/alerts?location_id={principal}", token)
        check("alerta de stock bajo", any(a["product_id"] == aud["product_id"] for a in alertas["items"]))
    _, resumen, _ = llamar("GET", "/api/sales/summary", token)
    check("resumen CQRS actualizado", resumen["num_sales"] >= 1,
          f"ventas={resumen['num_sales']} ingresos={resumen['revenue']:.0f}")

    print("5. Balanceo round-robin entre réplicas de pos-sales")
    replicas = {llamar("GET", "/api/sales?limit=1", token)[2].get("X-Replica") for _ in range(9)}
    check("varias réplicas atienden", len(replicas) >= 2, ", ".join(sorted(replicas)))

    print("6. Roles y export para MapReduce")
    llamar("POST", "/api/auth/users", token, {"username": "cajero1", "password": "cajero123"})
    _, l2, _ = llamar("POST", "/api/auth/login", cuerpo={"username": "cajero1", "password": "cajero123"})
    s, _, _ = llamar("POST", "/api/inventory/products", l2["access_token"], {"name": "X", "price": 1})
    check("vendedor no puede crear productos -> 403", s == 403)
    s, csv_txt, _ = llamar("GET", "/api/sales/export", token)
    check("export CSV (bronze)", s == 200 and csv_txt.startswith("sale_id,"),
          f"{len(csv_txt.splitlines()) - 1} líneas")

    print("7. Caja, clientes a crédito y factura electrónica (día 2)")
    s, caja, _ = llamar("GET", f"/api/cash/sessions/current?location_id={principal}", token)
    if s == 404:
        s, caja, _ = llamar("POST", "/api/cash/sessions/open", token, {"location_id": principal, "initial_cash": 100000})
    check("caja abierta", s in (200, 201), f"caja #{caja.get('id')}")
    s, cliente, _ = llamar("POST", "/api/clients", token, {"name": "Cliente Prueba Humo", "doc_type": "CC",
                                                           "document": "1234567890", "email": "prueba@demo.co"})
    check("cliente creado", s == 201, f"#{cliente.get('id')}")
    prod = next(p for p in productos.values() if p["stock"] >= 2 and p["name"] != aud["name"])
    s, venta, _ = llamar("POST", "/api/sales", token, {
        "location_id": principal, "payment_method": "credito", "client_id": cliente["id"],
        "session_id": caja["id"], "invoice": True,
        "customer": {"doc_type": "CC", "identification": "1234567890", "name": "Cliente Prueba Humo",
                     "email": "prueba@demo.co"},
        "items": [{"product_id": prod["product_id"], "quantity": 1}]})
    check("venta a crédito con factura", s == 201, f"venta #{venta.get('id')} total={venta.get('total')}")
    deuda = factura = None
    for _ in range(40):
        time.sleep(0.5)
        _, c, _ = llamar("GET", f"/api/clients/{cliente['id']}", token)
        deuda = c["debt"]
        s, f, _ = llamar("GET", f"/api/billing/invoices/by-sale/{venta['id']}", token)
        factura = f if s == 200 else None
        if deuda == venta["total"] and factura and factura["dian_status"] in ("emitida", "simulada"):
            break
    check("deuda sumada por el evento", deuda == venta["total"], f"deuda={deuda}")
    check("factura DIAN emitida (Factus)", bool(factura) and factura["dian_status"] in ("emitida", "simulada"),
          f"{factura['number']} CUFE={factura['cufe'][:16]}... doc={bool(factura['pdf_url'])}" if factura else "sin factura")
    s, _, _ = llamar("POST", f"/api/clients/{cliente['id']}/payments", token,
                     {"amount": venta["total"], "session_id": caja["id"]})
    check("abono registrado", s == 201)
    for _ in range(20):
        time.sleep(0.5)
        _, caja2, _ = llamar("GET", f"/api/cash/sessions/current?location_id={principal}", token)
        if caja2["payments"] >= venta["total"]:
            break
    check("abono llegó a la caja (client.payment)", caja2["payments"] >= venta["total"],
          f"esperado en caja={caja2['expected_cash']:.0f}")

    print("8. Frontend y monitoreo")
    s, html, _ = llamar("GET", "/")
    check("frontend servido por el gateway", s == 200 and "GestPro" in html)
    try:
        with urllib.request.urlopen("http://localhost:9090/api/v1/targets?state=active", timeout=10) as r:
            targets = json.loads(r.read())["data"]["activeTargets"]
        arriba = sum(1 for t in targets if t["health"] == "up")
        check("Prometheus descubre cada réplica", arriba >= 15, f"{arriba}/{len(targets)} targets up")
    except Exception as exc:
        check("Prometheus descubre cada réplica", False, str(exc))

    print(f"\nResultado: {'TODO OK' if fallos == 0 else f'{fallos} fallas'}")
    sys.exit(1 if fallos else 0)


if __name__ == "__main__":
    main()
