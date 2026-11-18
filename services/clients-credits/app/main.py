"""clients-credits: clientes, ventas a crédito (deuda) y abonos.

Comandos (escriben):  POST /api/clients, POST /api/clients/{id}/payments
Consultas (leen):     GET /api/clients, GET /api/clients/{id}
Eventos:              consume sale.created     -> si fue a crédito, suma el total a la deuda
                      publica client.payment   -> un abono (cash-expenses lo suma a la caja)
"""
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from prometheus_client import Counter, Gauge
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from . import events
from .database import SessionLocal, db_ok, get_db, init_db
from .models import Client, ClientPayment
from .observability import REPLICA, instrument

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("clients-credits")
COLOMBIA = timezone(timedelta(hours=-5))

CREDIT_SALES = Counter("gestpro_credit_sales_amount_total", "Valor vendido a crédito (COP)")
PAYMENTS = Counter("gestpro_client_payments_amount_total", "Valor abonado por clientes (COP)")
DEBT = Gauge("gestpro_clients_debt_cop", "Deuda total de los clientes (COP)")


def _actualizar_gauge(db: Session):
    DEBT.set(db.query(func.coalesce(func.sum(Client.debt), 0)).scalar())


# ── Consumidor de eventos ───────────────────────────────────
def on_sale_created(evt: dict):
    if evt.get("payment_method") != "credito" or not evt.get("client_id"):
        return                                  # solo interesan las ventas a crédito
    db = SessionLocal()
    try:
        cliente = db.query(Client).filter(Client.id == evt["client_id"]).with_for_update().first()
        if not cliente:
            log.warning("Venta #%s a crédito de un cliente inexistente (%s)", evt["sale_id"], evt["client_id"])
            return
        cliente.debt += evt["total"]
        db.commit()
        CREDIT_SALES.inc(evt["total"])
        _actualizar_gauge(db)
        log.info("Venta #%s a crédito: deuda de %s ahora es %.0f", evt["sale_id"], cliente.name, cliente.debt)
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app):
    init_db()
    db = SessionLocal()
    try:
        _actualizar_gauge(db)
    finally:
        db.close()
    events.start_consumer("clients", {"sale.created": on_sale_created})
    yield


app = FastAPI(title="GestPro · clients-credits", lifespan=lifespan)
instrument(app)


class ClientIn(BaseModel):
    name: str
    document: str = ""
    doc_type: str = "CC"
    phone: str = ""
    email: str = ""
    address: str = ""
    notes: str = ""


class PaymentIn(BaseModel):
    amount: float = Field(gt=0)
    session_id: int | None = None
    notes: str = ""


def client_out(c: Client) -> dict:
    return {"id": c.id, "name": c.name, "document": c.document, "doc_type": c.doc_type,
            "phone": c.phone, "email": c.email, "address": c.address, "notes": c.notes,
            "debt": c.debt, "created_at": c.created_at.isoformat()}


@app.get("/health")
def health():
    if not db_ok():
        return JSONResponse({"status": "error", "replica": REPLICA}, status_code=503)
    return {"status": "ok", "replica": REPLICA}


# ── Comandos ───────────────────────────────────────────────
@app.post("/api/clients", status_code=201)
def create_client(data: ClientIn, db: Session = Depends(get_db)):
    if data.doc_type not in ("CC", "NIT", "CE", "PP", "TI"):
        raise HTTPException(400, "Tipo de documento inválido")
    cliente = Client(**data.model_dump(), created_at=datetime.now(COLOMBIA).date())
    db.add(cliente)
    db.commit()
    return client_out(cliente)


@app.post("/api/clients/{client_id}/payments", status_code=201)
def register_payment(client_id: int, data: PaymentIn, db: Session = Depends(get_db),
                     x_user_id: str = Header(default="")):
    cliente = db.query(Client).filter(Client.id == client_id).with_for_update().first()
    if not cliente:
        raise HTTPException(404, "Cliente no encontrado")
    if data.amount > cliente.debt + 0.01:
        raise HTTPException(400, f"El abono supera la deuda ({cliente.debt:.0f})")
    pago = ClientPayment(client_id=cliente.id, user_id=int(x_user_id) if x_user_id.isdigit() else None,
                         session_id=data.session_id, amount=data.amount, notes=data.notes,
                         timestamp=datetime.now(COLOMBIA).replace(tzinfo=None))
    cliente.debt -= data.amount
    db.add(pago)
    db.flush()
    try:
        events.publish("client.payment", {"payment_id": pago.id, "client_id": cliente.id,
                                          "client_name": cliente.name, "amount": data.amount,
                                          "session_id": data.session_id, "user_id": pago.user_id})
    except Exception:
        db.rollback()
        raise HTTPException(503, "El bus de eventos no está disponible; el abono no se registró")
    db.commit()
    PAYMENTS.inc(data.amount)
    _actualizar_gauge(db)
    return {"payment_id": pago.id, "client": client_out(cliente)}


# ── Consultas ──────────────────────────────────────────────
@app.get("/api/clients")
def list_clients(search: str = "", with_debt: bool = False, db: Session = Depends(get_db)):
    q = db.query(Client)
    if search:
        q = q.filter(or_(Client.name.ilike(f"%{search}%"), Client.document.ilike(f"%{search}%")))
    if with_debt:
        q = q.filter(Client.debt > 0)
    return [client_out(c) for c in q.order_by(Client.name)]


@app.get("/api/clients/{client_id}")
def get_client(client_id: int, db: Session = Depends(get_db)):
    cliente = db.get(Client, client_id)
    if not cliente:
        raise HTTPException(404, "Cliente no encontrado")
    pagos = (db.query(ClientPayment).filter(ClientPayment.client_id == client_id)
             .order_by(ClientPayment.id.desc()).limit(50))
    return {**client_out(cliente),
            "payments": [{"id": p.id, "amount": p.amount, "notes": p.notes,
                          "timestamp": p.timestamp.isoformat(timespec="seconds")} for p in pagos]}
