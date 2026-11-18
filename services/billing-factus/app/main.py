"""billing-factus: factura electrónica ante la DIAN a través de la API de Factus.

Reutiliza factus_api.py del monolito (OAuth2 + envío de la factura) sin reescribirlo:
solo cambia de dónde salen las credenciales (Docker secret en vez de la tabla config).

Eventos:            consume sale.created (si la venta pidió factura) -> emite en Factus
                    publica invoice.issued
                    Si Factus falla, el evento NO se confirma: se reintenta cada ~30 s
                    (hasta 5 veces) sin bloquear la venta, que ya quedó registrada.
Consultas (leen):   GET /api/billing/invoices, GET /api/billing/invoices/by-sale/{sale_id},
                    GET /api/billing/status
Comando:            POST /api/billing/invoices/{id}/retry  (reintento manual)
"""
import hashlib
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from prometheus_client import Counter, Gauge, Histogram
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import events
from .database import SessionLocal, db_ok, get_db, init_db
from .factus_api import FactusClient
from .models import Invoice
from .observability import REPLICA, instrument

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("billing-factus")
COLOMBIA = timezone(timedelta(hours=-5))

# sandbox = API real de Factus (habilitación DIAN). simulado = plan B sin internet.
FACTUS_MODE = os.getenv("FACTUS_MODE", "sandbox")
SECRET_PATH = os.getenv("FACTUS_SECRET_FILE", "/run/secrets/factus_credentials")

INVOICES = Counter("gestpro_invoices_total", "Intentos de facturación electrónica", ["status"])
FACTUS_SECONDS = Histogram("gestpro_factus_request_seconds", "Tiempo de respuesta de la API de Factus")
INVOICES_BY_STATUS = Gauge("gestpro_invoices_by_status", "Facturas por estado DIAN (desde la BD)", ["status"])
ESTADOS = ("pendiente", "emitida", "error", "simulada")
for _e in ESTADOS:
    INVOICES.labels(_e)


def metricas_negocio():
    db = SessionLocal()
    try:
        conteo = dict(db.query(Invoice.dian_status, func.count()).group_by(Invoice.dian_status).all())
        for estado in ESTADOS:
            INVOICES_BY_STATUS.labels(estado).set(conteo.get(estado, 0))
    finally:
        db.close()


class ConfigFactus:
    """Adaptador: FactusClient pide db.cfg(clave); aquí las claves salen del Docker secret."""

    def __init__(self):
        self.datos = {}
        if os.path.exists(SECRET_PATH):
            with open(SECRET_PATH, encoding="utf-8") as f:
                self.datos = json.load(f)

    def cfg(self, clave: str) -> str:
        return str(self.datos.get(clave, ""))


_cliente = None


def factus() -> FactusClient:
    global _cliente
    if _cliente is None:
        _cliente = FactusClient(ConfigFactus())   # cachea el token OAuth2 (~1 hora)
    return _cliente


def emitir(venta: dict, factura: Invoice) -> dict:
    if FACTUS_MODE == "simulado":
        return {"cufe": hashlib.sha384(factura.reference_code.encode()).hexdigest(),
                "number": f"SIM-{factura.id}", "pdf_url": "", "qr_url": "", "status": "simulada"}
    # El descuento se reparte en el precio de cada ítem para que los totales cuadren en la DIAN
    tasa = venta["discount"] / venta["subtotal"] if venta["subtotal"] else 0
    items = [{**i, "unit_price": round(i["unit_price"] * (1 - tasa), 2)} for i in venta["items"]]
    cliente = dict(venta.get("customer") or {})
    cliente["payment_method"] = venta["payment_method"].replace("credito", "crédito")
    inicio = time.perf_counter()
    try:
        resultado = factus().crear_factura(
            venta["sale_id"], items,
            {"sub": venta["total"], "disc": 0, "iva": 0, "total": venta["total"]},
            cliente, factura.reference_code)
    finally:
        FACTUS_SECONDS.observe(time.perf_counter() - inicio)
    resultado["status"] = "emitida"
    return resultado


def procesar(db: Session, venta: dict, factura: Invoice):
    factura.attempts += 1
    try:
        r = emitir(venta, factura)
    except Exception as exc:          # FactusError, sin internet, timeout...
        factura.dian_status = "error"
        factura.error = str(exc)[:500]
        db.commit()
        INVOICES.labels("error").inc()
        raise
    factura.cufe, factura.number = r["cufe"], r["number"]
    factura.pdf_url, factura.qr_url = r.get("pdf_url", ""), r.get("qr_url", "")
    factura.dian_status, factura.error = r["status"], ""
    db.commit()
    INVOICES.labels(r["status"]).inc()
    log.info("Factura %s (%s) para la venta #%s | CUFE %s...", factura.number, factura.dian_status,
             factura.sale_id, factura.cufe[:16])
    events.publish("invoice.issued", {"invoice_id": factura.id, "sale_id": factura.sale_id,
                                      "number": factura.number, "cufe": factura.cufe,
                                      "status": factura.dian_status})


# ── Consumidor de eventos ───────────────────────────────────
def on_sale_created(evt: dict):
    if not evt.get("invoice_requested"):
        return
    db = SessionLocal()
    try:
        factura = db.query(Invoice).filter_by(sale_id=evt["sale_id"]).first()
        if factura and factura.dian_status in ("emitida", "simulada"):
            return                                     # ya estaba facturada
        if not factura:
            cliente = evt.get("customer") or {}
            factura = Invoice(sale_id=evt["sale_id"], reference_code=f"GPC-{evt['sale_id']}-{uuid.uuid4().hex[:6]}",
                              date=datetime.fromisoformat(evt["date"]).date(), time=evt["time"],
                              subtotal=evt["subtotal"], discount=evt["discount"], total=evt["total"],
                              payment_method=evt["payment_method"],
                              customer_name=cliente.get("name", "Consumidor Final"),
                              customer_doc=cliente.get("identification", ""),
                              sale_data=json.dumps(evt, default=str))
            db.add(factura)
            db.commit()
        procesar(db, evt, factura)
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app):
    init_db()
    events.start_consumer("billing", {"sale.created": on_sale_created})
    yield


app = FastAPI(title="GestPro · billing-factus", lifespan=lifespan)
instrument(app, on_scrape=metricas_negocio)


def invoice_out(f: Invoice) -> dict:
    return {"id": f.id, "sale_id": f.sale_id, "number": f.number, "reference_code": f.reference_code,
            "date": f.date.isoformat(), "time": f.time, "total": f.total, "payment_method": f.payment_method,
            "customer_name": f.customer_name, "customer_doc": f.customer_doc, "cufe": f.cufe,
            "pdf_url": f.pdf_url, "qr_url": f.qr_url, "dian_status": f.dian_status,
            "error": f.error, "attempts": f.attempts}


@app.get("/health")
def health():
    if not db_ok():
        return JSONResponse({"status": "error", "replica": REPLICA}, status_code=503)
    return {"status": "ok", "replica": REPLICA}


# ── Consultas ──────────────────────────────────────────────
@app.get("/api/billing/status")
def status():
    return {"mode": FACTUS_MODE, "configured": factus().is_configured(), "environment": factus().base_url}


@app.get("/api/billing/invoices")
def list_invoices(limit: int = 50, db: Session = Depends(get_db)):
    return [invoice_out(f) for f in db.query(Invoice).order_by(Invoice.id.desc()).limit(min(limit, 200))]


@app.get("/api/billing/invoices/by-sale/{sale_id}")
def invoice_by_sale(sale_id: int, db: Session = Depends(get_db)):
    factura = db.query(Invoice).filter_by(sale_id=sale_id).first()
    if not factura:
        raise HTTPException(404, "La venta todavía no tiene factura (se procesa de forma asíncrona)")
    return invoice_out(factura)


# ── Comando ────────────────────────────────────────────────
@app.post("/api/billing/invoices/{invoice_id}/retry")
def retry(invoice_id: int, db: Session = Depends(get_db), x_user_role: str = Header(default="")):
    if x_user_role != "admin":
        raise HTTPException(403, "Solo un administrador puede reintentar facturas")
    factura = db.get(Invoice, invoice_id)
    if not factura or factura.dian_status in ("emitida", "simulada"):
        raise HTTPException(400, "La factura no existe o ya fue emitida")
    try:
        procesar(db, json.loads(factura.sale_data), factura)
    except Exception as exc:
        raise HTTPException(502, f"Factus rechazó la factura: {exc}")
    return invoice_out(factura)
