"""pos-sales: registro de ventas del punto de venta (caso de uso estrella).

Comando (escribe):  POST /api/sales
    1. Consulta SÍNCRONA a catalog-inventory: precio y stock actuales (no se vende lo que no hay).
    2. Guarda la venta en su propia BD (sales_db).
    3. Publica el evento sale.created en Redis. Lo demás (descontar stock, deuda del
       cliente, caja, factura DIAN) lo hacen otros servicios de forma ASÍNCRONA.
Consultas (leen):   GET /api/sales, GET /api/sales/{id},
                    GET /api/sales/summary  (modelo de lectura CQRS)
                    GET /api/sales/export   (CSV crudo para el MapReduce -> capa bronze)
"""
import csv
import io
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone

import requests
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from prometheus_client import Counter, Gauge
from pydantic import BaseModel, Field
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from . import events
from .database import SessionLocal, db_ok, get_db, init_db
from .models import Sale, SaleItem, SalesSummary
from .observability import REPLICA, instrument

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("pos-sales")

INVENTORY_URL = os.getenv("INVENTORY_URL", "http://catalog-inventory:8000")
COLOMBIA = timezone(timedelta(hours=-5))
METODOS_PAGO = {"efectivo", "tarjeta", "transferencia", "credito"}

SALES_TOTAL = Counter("gestpro_sales_total", "Ventas registradas", ["payment_method"])
SALES_AMOUNT = Counter("gestpro_sales_amount_total", "Valor vendido (COP)")
SALES_REJECTED = Counter("gestpro_sales_rejected_total", "Ventas rechazadas", ["reason"])
for _m in METODOS_PAGO:            # series en 0 desde el arranque: increase() cuenta la primera venta
    SALES_TOTAL.labels(_m)
for _r in ("sin_stock", "inventario_caido", "bus_caido"):
    SALES_REJECTED.labels(_r)

# Métricas de negocio leídas del modelo de lectura (CQRS) en cada scrape de Prometheus
SALES_TODAY = Gauge("gestpro_sales_today", "Ventas del día por sede", ["location"])
REVENUE_TODAY = Gauge("gestpro_revenue_today_cop", "Ingresos del día por sede (COP)", ["location"])


def metricas_negocio():
    db = SessionLocal()
    try:
        hoy = datetime.now(COLOMBIA).date()
        for fila in db.query(SalesSummary).filter(SalesSummary.date == hoy):
            SALES_TODAY.labels(fila.location_name).set(fila.num_sales)
            REVENUE_TODAY.labels(fila.location_name).set(fila.revenue)
    finally:
        db.close()


# ── Lado de lectura (CQRS): proyección asíncrona ────────────
def on_sale_created(evt: dict):
    """Actualiza el resumen por día y sede (sales_summary) a partir del evento."""
    db = SessionLocal()
    try:
        stmt = pg_insert(SalesSummary).values(
            date=date.fromisoformat(evt["date"]), location_id=evt["location_id"],
            location_name=evt["location_name"], num_sales=1, revenue=evt["total"])
        stmt = stmt.on_conflict_do_update(
            index_elements=["date", "location_id"],
            set_={"num_sales": SalesSummary.num_sales + 1,
                  "revenue": SalesSummary.revenue + stmt.excluded.revenue})
        db.execute(stmt)
        db.commit()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app):
    init_db()
    events.start_consumer("sales-projection", {"sale.created": on_sale_created})
    yield


app = FastAPI(title="GestPro · pos-sales", lifespan=lifespan)
instrument(app, on_scrape=metricas_negocio)


class ItemIn(BaseModel):
    product_id: int
    quantity: int = Field(gt=0)


class CustomerIn(BaseModel):            # datos para la factura electrónica (Factus)
    doc_type: str = "CC"
    identification: str = "222222222222"
    name: str = "Consumidor Final"
    email: str = "consumidor@final.com"
    phone: str = ""
    address: str = ""


class SaleIn(BaseModel):
    location_id: int
    payment_method: str = "efectivo"
    items: list[ItemIn] = Field(min_length=1)
    discount: float = Field(default=0, ge=0)
    client_id: int | None = None
    session_id: int | None = None
    invoice: bool = False
    customer: CustomerIn | None = None
    notes: str = ""


def sale_out(s: Sale) -> dict:
    return {"id": s.id, "date": s.date.isoformat(), "time": s.time, "location_id": s.location_id,
            "location_name": s.location_name, "user_name": s.user_name, "client_id": s.client_id,
            "session_id": s.session_id, "payment_method": s.payment_method,
            "subtotal": s.subtotal, "discount": s.discount, "total": s.total,
            "invoice_requested": s.invoice_requested, "notes": s.notes,
            "items": [{"product_id": i.product_id, "product_name": i.product_name,
                       "category": i.category, "quantity": i.quantity,
                       "unit_price": i.unit_price, "subtotal": i.subtotal} for i in s.items]}


def consultar_producto(product_id: int, location_id: int) -> dict:
    """Llamada síncrona a catalog-inventory (por la red overlay, sin pasar por el gateway)."""
    try:
        resp = requests.get(f"{INVENTORY_URL}/api/inventory/products/{product_id}",
                            params={"location_id": location_id}, timeout=3)
    except requests.RequestException:
        SALES_REJECTED.labels("inventario_caido").inc()
        raise HTTPException(503, "El servicio de inventario no está disponible; la venta no se registró")
    if resp.status_code == 404:
        raise HTTPException(400, f"El producto {product_id} no existe o la sede no existe")
    resp.raise_for_status()
    return resp.json()


@app.get("/health")
def health():
    # Demo de rollback: docker service update --env-add SIMULAR_FALLO=1 gestpro_pos-sales
    # La "nueva versión" nunca queda sana y Swarm vuelve sola a la anterior (failure_action: rollback).
    if os.getenv("SIMULAR_FALLO") == "1":
        return JSONResponse({"status": "fallo simulado", "replica": REPLICA}, status_code=503)
    if not db_ok():
        return JSONResponse({"status": "error", "replica": REPLICA}, status_code=503)
    return {"status": "ok", "replica": REPLICA}


# ── Comando ────────────────────────────────────────────────
@app.post("/api/sales", status_code=201)
def create_sale(data: SaleIn, db: Session = Depends(get_db),
                x_user_id: str = Header(default=""), x_user_name: str = Header(default="")):
    metodo = data.payment_method.lower().replace("é", "e")
    if metodo not in METODOS_PAGO:
        raise HTTPException(400, f"Método de pago inválido: {sorted(METODOS_PAGO)}")
    if metodo == "credito" and not data.client_id:
        raise HTTPException(400, "Una venta a crédito necesita un cliente")

    # Agrupar cantidades por producto y validar stock en la sede
    cantidades: dict[int, int] = {}
    for item in data.items:
        cantidades[item.product_id] = cantidades.get(item.product_id, 0) + item.quantity
    lineas, sede = [], ""
    for product_id, cantidad in cantidades.items():
        p = consultar_producto(product_id, data.location_id)
        if p["stock"] < cantidad:
            SALES_REJECTED.labels("sin_stock").inc()
            raise HTTPException(409, f"Stock insuficiente de '{p['name']}' en {p['location_name']} "
                                     f"(disponible: {p['stock']})")
        sede = p["location_name"]
        lineas.append(SaleItem(product_id=product_id, product_name=p["name"], category=p["category"],
                               quantity=cantidad, unit_price=p["price"], subtotal=p["price"] * cantidad))

    subtotal = sum(l.subtotal for l in lineas)
    descuento = min(data.discount, subtotal)
    ahora = datetime.now(COLOMBIA)
    venta = Sale(client_id=data.client_id, user_id=int(x_user_id) if x_user_id.isdigit() else None,
                 user_name=x_user_name, session_id=data.session_id, location_id=data.location_id,
                 location_name=sede, date=ahora.date(), time=ahora.strftime("%H:%M:%S"),
                 subtotal=subtotal, discount=descuento, total=subtotal - descuento,
                 payment_method=metodo, invoice_requested=data.invoice, notes=data.notes, items=lineas)
    db.add(venta)
    db.flush()                       # obtiene el id de la venta sin confirmar todavía

    evento = sale_out(venta)
    evento["sale_id"] = venta.id
    evento["user_id"] = venta.user_id
    evento["customer"] = (data.customer or CustomerIn()).model_dump() if data.invoice else None
    try:
        events.publish("sale.created", evento)
    except Exception:
        db.rollback()                # sin evento no hay venta: evita inconsistencias
        SALES_REJECTED.labels("bus_caido").inc()
        raise HTTPException(503, "El bus de eventos no está disponible; la venta no se registró")
    db.commit()

    SALES_TOTAL.labels(metodo).inc()
    SALES_AMOUNT.inc(venta.total)
    log.info("Venta #%s en %s: %s ítems, total %.0f (%s)", venta.id, sede, len(lineas), venta.total, metodo)
    return {**sale_out(venta), "replica": REPLICA}


# ── Consultas ──────────────────────────────────────────────
@app.get("/api/sales/summary")
def summary(days: int = 30, location_id: int | None = None, db: Session = Depends(get_db)):
    desde = datetime.now(COLOMBIA).date() - timedelta(days=days)
    q = db.query(SalesSummary).filter(SalesSummary.date >= desde)
    if location_id:
        q = q.filter(SalesSummary.location_id == location_id)
    filas = q.order_by(SalesSummary.date).all()
    return {"num_sales": sum(f.num_sales for f in filas),
            "revenue": sum(f.revenue for f in filas),
            "by_day": [{"date": f.date.isoformat(), "location": f.location_name,
                        "num_sales": f.num_sales, "revenue": f.revenue} for f in filas]}


@app.get("/api/sales/export")
def export_csv(db: Session = Depends(get_db)):
    """Histórico de ventas en crudo (una fila por línea de venta) para la capa bronze del MapReduce."""
    def generar():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["sale_id", "fecha", "sede", "categoria", "producto", "cantidad",
                    "precio_unitario", "total_linea", "metodo_pago", "origen"])
        filas = (db.query(Sale, SaleItem).join(SaleItem, SaleItem.sale_id == Sale.id)
                 .order_by(Sale.id).yield_per(1000))
        for venta, item in filas:
            w.writerow([venta.id, venta.date.isoformat(), venta.location_name, item.category,
                        item.product_name, item.quantity, item.unit_price, item.subtotal,
                        venta.payment_method, "real"])
            if buf.tell() > 64_000:
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate()
        yield buf.getvalue()
    return StreamingResponse(generar(), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=ventas_reales.csv"})


@app.get("/api/sales")
def list_sales(date_from: date | None = None, date_to: date | None = None,
               location_id: int | None = None, limit: int = 50, db: Session = Depends(get_db)):
    q = db.query(Sale)
    if date_from:
        q = q.filter(Sale.date >= date_from)
    if date_to:
        q = q.filter(Sale.date <= date_to)
    if location_id:
        q = q.filter(Sale.location_id == location_id)
    return [sale_out(s) for s in q.order_by(Sale.id.desc()).limit(min(limit, 500))]


@app.get("/api/sales/{sale_id}")
def get_sale(sale_id: int, db: Session = Depends(get_db)):
    venta = db.get(Sale, sale_id)
    if not venta:
        raise HTTPException(404, "Venta no encontrada")
    return sale_out(venta)
