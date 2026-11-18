"""Migración de datos: monolito GestPro (SQLite) -> bases PostgreSQL de cada microservicio.

Cada tabla del monolito se copia a la base del servicio dueño de ese dominio
(Database per Service). Es idempotente: se puede ejecutar varias veces.

Uso (lo ejecuta scripts/migrate.ps1 dentro de la red overlay del stack):
    python migrate_sqlite.py /legacy/GestPro.db
"""
import os
import sqlite3
import sys

import psycopg2

CLAVES_SECRETAS = ("factus_", "license_")   # NO se migran: viajan como Docker secrets


def pg(host: str, dbname: str):
    return psycopg2.connect(host=host, dbname=dbname, user="gestpro",
                            password=os.environ["DB_PASSWORD"])


def filas(src, sql):
    try:
        return [dict(r) for r in src.execute(sql)]
    except sqlite3.OperationalError:
        return []   # tabla que no existe en versiones viejas del monolito


def reset_seq(cur, tabla):
    cur.execute(f"SELECT setval(pg_get_serial_sequence('{tabla}', 'id'), "
                f"COALESCE((SELECT MAX(id) FROM {tabla}), 0) + 1, false)")


def migrar_auth(src):
    usuarios = filas(src, "SELECT username, password, role, active FROM users")
    config = [c for c in filas(src, "SELECT key, value FROM config")
              if not c["key"].startswith(CLAVES_SECRETAS)]
    with pg("db-auth", "auth_db") as con, con.cursor() as cur:
        for u in usuarios:
            # El hash SHA-256 del monolito se conserva; auth-users lo cambia a PBKDF2 en el primer login
            cur.execute("""INSERT INTO users (username, password, role, active) VALUES (%s, %s, %s, %s)
                           ON CONFLICT (username) DO UPDATE
                           SET password = EXCLUDED.password, role = EXCLUDED.role, active = EXCLUDED.active""",
                        (u["username"], u["password"], u["role"], bool(u["active"])))
        for c in config:
            cur.execute("""INSERT INTO config (key, value) VALUES (%s, %s)
                           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""", (c["key"], c["value"]))
    print(f"[auth-users]        users={len(usuarios)}  config={len(config)} (sin secretos)")


def migrar_inventario(src):
    """Devuelve los mapas id_viejo -> id_nuevo que necesitan las ventas."""
    categorias = filas(src, "SELECT name FROM categories")
    sedes = filas(src, "SELECT * FROM pos_locations")
    productos = filas(src, "SELECT * FROM products")
    stock_sede = filas(src, "SELECT * FROM pos_inventory")
    traslados = filas(src, "SELECT * FROM inventory_transfers")
    mapa_sede, mapa_prod = {}, {}
    with pg("db-inventory", "inventory_db") as con, con.cursor() as cur:
        for c in categorias:
            cur.execute("INSERT INTO categories (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (c["name"],))
        for s in sedes:
            cur.execute("""INSERT INTO pos_locations (name, address, phone, active) VALUES (%s, %s, %s, %s)
                           ON CONFLICT (name) DO UPDATE SET address = EXCLUDED.address RETURNING id""",
                        (s["name"], s.get("address") or "", s.get("phone") or "", bool(s.get("active", 1))))
            mapa_sede[s["id"]] = cur.fetchone()[0]
        for p in productos:
            cur.execute("""INSERT INTO products (name, sku, barcode, category, price, cost, supplier,
                                                 min_stock, unit, active)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT (sku) DO UPDATE SET name = EXCLUDED.name RETURNING id""",
                        (p["name"], p["sku"], p.get("barcode") or "", p.get("category") or "General",
                         p["price"], p["cost"], p.get("supplier") or "", p.get("min_stock", 5),
                         p.get("unit") or "unidad", bool(p["active"])))
            mapa_prod[p["id"]] = cur.fetchone()[0]
        # Si el monolito no tenía stock por sede, el stock global va a la primera sede
        if not stock_sede and mapa_sede:
            primera = min(mapa_sede)
            stock_sede = [{"location_id": primera, "product_id": p["id"], "stock": p["stock"]}
                          for p in productos if p.get("stock", 0) > 0]
        for s in stock_sede:
            cur.execute("""INSERT INTO pos_inventory (location_id, product_id, stock) VALUES (%s, %s, %s)
                           ON CONFLICT (location_id, product_id) DO UPDATE SET stock = EXCLUDED.stock""",
                        (mapa_sede[s["location_id"]], mapa_prod[s["product_id"]], s["stock"]))
        for t in traslados:
            cur.execute("""INSERT INTO inventory_transfers (from_location_id, to_location_id, product_id,
                                                            quantity, user_id, timestamp, notes)
                           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                        (mapa_sede[t["from_location_id"]], mapa_sede[t["to_location_id"]],
                         mapa_prod[t["product_id"]], t["quantity"], t["user_id"], t["timestamp"],
                         t.get("notes") or ""))
    print(f"[catalog-inventory] categories={len(categorias)}  pos_locations={len(sedes)}  "
          f"products={len(productos)}  pos_inventory={len(stock_sede)}  transfers={len(traslados)}")
    nombres_sede = {s["id"]: s["name"] for s in sedes}
    categoria_prod = {p["id"]: p.get("category") or "General" for p in productos}
    return mapa_sede, nombres_sede, mapa_prod, categoria_prod


def migrar_ventas(src, mapa_sede, nombres_sede, mapa_prod, categoria_prod):
    ventas = filas(src, "SELECT * FROM sales")
    items = filas(src, "SELECT * FROM sale_items")
    primera = min(mapa_sede) if mapa_sede else None
    with pg("db-sales", "sales_db") as con, con.cursor() as cur:
        for v in ventas:
            sede = v.get("location_id") or primera
            metodo = (v["payment_method"] or "efectivo").replace("é", "e")
            cur.execute("""INSERT INTO sales (id, client_id, user_id, user_name, session_id, location_id,
                                              location_name, date, time, subtotal, discount, total,
                                              payment_method, invoice_requested, notes)
                           VALUES (%s, %s, %s, '', %s, %s, %s, %s, %s, %s, %s, %s, %s, false, %s)
                           ON CONFLICT (id) DO NOTHING""",
                        (v["id"], v["client_id"], v["user_id"], v.get("session_id"), mapa_sede.get(sede),
                         nombres_sede.get(sede, ""), v["date"], v["time"], v["subtotal"], v["discount"],
                         v["total"], metodo, v.get("notes") or ""))
        for i in items:
            cur.execute("""INSERT INTO sale_items (id, sale_id, product_id, product_name, category,
                                                   quantity, unit_price, subtotal)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
                        (i["id"], i["sale_id"], mapa_prod.get(i["product_id"]), i["product_name"],
                         categoria_prod.get(i["product_id"], "General"), int(i["quantity"]),
                         i["unit_price"], i["subtotal"]))
        reset_seq(cur, "sales")
        reset_seq(cur, "sale_items")
        # Reconstruir el modelo de lectura (CQRS) con las ventas históricas
        cur.execute("""INSERT INTO sales_summary (date, location_id, location_name, num_sales, revenue)
                       SELECT date, location_id, MAX(location_name), COUNT(*), SUM(total)
                       FROM sales GROUP BY date, location_id
                       ON CONFLICT (date, location_id) DO UPDATE
                       SET num_sales = EXCLUDED.num_sales, revenue = EXCLUDED.revenue""")
    print(f"[pos-sales]         sales={len(ventas)}  sale_items={len(items)}")


def copiar(cur, tabla, registros, columnas, nuevas=None):
    """Copia filas conservando el id original (las otras tablas guardan referencias a él).
    `nuevas`: columnas que no existían en el monolito, con su valor inicial."""
    nuevas = nuevas or {}
    todas = columnas + list(nuevas)
    for r in registros:
        valores = [r.get(c) for c in columnas] + list(nuevas.values())
        cur.execute(f"INSERT INTO {tabla} ({', '.join(todas)}) VALUES ({', '.join(['%s'] * len(todas))}) "
                    f"ON CONFLICT (id) DO NOTHING", valores)
    reset_seq(cur, tabla)


def migrar_clientes(src):
    clientes = filas(src, "SELECT * FROM clients")
    pagos = filas(src, "SELECT * FROM client_payments")
    with pg("db-clients", "clients_db") as con, con.cursor() as cur:
        copiar(cur, "clients", clientes,
               ["id", "name", "document", "phone", "email", "address", "notes", "debt", "created_at"],
               {"doc_type": "CC"})
        copiar(cur, "client_payments", pagos,
               ["id", "client_id", "user_id", "session_id", "amount", "notes", "timestamp"])
    print(f"[clients-credits]   clients={len(clientes)}  client_payments={len(pagos)}")


def migrar_caja(src, mapa_sede):
    primera = min(mapa_sede.values()) if mapa_sede else 1
    cajas = filas(src, "SELECT * FROM cash_sessions")
    for c in cajas:
        c["location_id"] = mapa_sede.get(c.get("location_id"), primera)
    movimientos = filas(src, "SELECT * FROM cash_transactions")
    gastos = filas(src, "SELECT * FROM expenses")
    for g in gastos:
        g["location_id"] = mapa_sede.get(g.get("location_id"), primera)
    with pg("db-cash", "cash_db") as con, con.cursor() as cur:
        copiar(cur, "cash_sessions", cajas,
               ["id", "user_id", "location_id", "opened_at", "closed_at", "initial_cash", "actual_cash",
                "expected_cash", "difference", "notes", "status"], {"user_name": ""})
        copiar(cur, "cash_transactions", movimientos,
               ["id", "session_id", "type", "amount", "description", "timestamp"],
               {"payment_method": "efectivo"})
        copiar(cur, "expenses", gastos,
               ["id", "user_id", "session_id", "location_id", "date", "category", "amount", "description",
                "timestamp"])
    print(f"[cash-expenses]     cash_sessions={len(cajas)}  cash_transactions={len(movimientos)}  "
          f"expenses={len(gastos)}")


def migrar_facturas(src):
    facturas = filas(src, "SELECT * FROM invoices")
    for f in facturas:
        f["reference_code"] = f["number"] = f.get("invoice_num") or f"FAC-{f['id']}"
        f["dian_status"] = f.get("dian_status") or ("emitida" if f.get("cufe") else "pendiente")
    with pg("db-billing", "billing_db") as con, con.cursor() as cur:
        copiar(cur, "invoices", facturas,
               ["id", "sale_id", "reference_code", "number", "date", "time", "subtotal", "discount", "tax_iva",
                "total", "payment_method", "cufe", "pdf_url", "dian_status"],
               {"customer_name": "Consumidor Final", "customer_doc": "", "qr_url": "", "error": "",
                "attempts": 0, "sale_data": "{}"})
    print(f"[billing-factus]    invoices={len(facturas)}")


def main():
    origen = sys.argv[1] if len(sys.argv) > 1 else "/legacy/GestPro.db"
    src = sqlite3.connect(origen)
    src.row_factory = sqlite3.Row
    print(f"Migrando {origen} -> PostgreSQL (una base por microservicio)")
    migrar_auth(src)
    mapas = migrar_inventario(src)
    migrar_ventas(src, *mapas)
    migrar_clientes(src)
    migrar_caja(src, mapas[0])
    migrar_facturas(src)
    print("Migración terminada (15 tablas del monolito -> 6 bases de datos).")


if __name__ == "__main__":
    main()
