"""cash-expenses: apertura y cierre de caja (cuadre), movimientos y gastos.

Comandos (escriben):  POST /api/cash/sessions/open, POST /api/cash/sessions/{id}/close,
                      POST /api/cash/expenses
Consultas (leen):     GET /api/cash/sessions/current, GET /api/cash/sessions, GET /api/cash/expenses
Eventos:              consume sale.created    -> registra la venta en la caja abierta
                      consume client.payment  -> registra el abono en la caja abierta
"""
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from prometheus_client import Counter, Gauge
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from . import events
from .database import SessionLocal, db_ok, get_db, init_db
from .models import CashSession, CashTransaction, Expense
from .observability import REPLICA, instrument

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("cash-expenses")
COLOMBIA = timezone(timedelta(hours=-5))

EXPENSES = Counter("gestpro_expenses_amount_total", "Valor de gastos registrados (COP)", ["category"])
CLOSE_DIFF = Gauge("gestpro_cash_close_difference_cop", "Diferencia del último cierre de caja (COP)")
OPEN_SESSIONS = Counter("gestpro_cash_sessions_opened_total", "Cajas abiertas")


def ahora():
    return datetime.now(COLOMBIA).replace(tzinfo=None)


def _registrar(session_id, tipo, metodo, monto, descripcion):
    """Copia local del movimiento, solo si la caja existe y sigue abierta."""
    if not session_id:
        return
    db = SessionLocal()
    try:
        caja = db.get(CashSession, session_id)
        if not caja or caja.status != "open":
            log.warning("Movimiento '%s' para la caja %s, que no está abierta: se ignora", tipo, session_id)
            return
        db.add(CashTransaction(session_id=session_id, type=tipo, payment_method=metodo,
                               amount=monto, description=descripcion, timestamp=ahora()))
        db.commit()
    finally:
        db.close()


def on_sale_created(evt: dict):
    _registrar(evt.get("session_id"), "venta", evt["payment_method"], evt["total"], f"Venta #{evt['sale_id']}")


def on_client_payment(evt: dict):
    _registrar(evt.get("session_id"), "abono", "efectivo", evt["amount"], f"Abono de {evt['client_name']}")


@asynccontextmanager
async def lifespan(app):
    init_db()
    events.start_consumer("cash", {"sale.created": on_sale_created, "client.payment": on_client_payment})
    yield


app = FastAPI(title="GestPro · cash-expenses", lifespan=lifespan)
instrument(app)


class OpenIn(BaseModel):
    location_id: int
    initial_cash: float = Field(default=0, ge=0)


class CloseIn(BaseModel):
    actual_cash: float = Field(ge=0)
    notes: str = ""


class ExpenseIn(BaseModel):
    category: str
    amount: float = Field(gt=0)
    description: str = ""
    location_id: int | None = None
    session_id: int | None = None      # si se indica, el gasto sale del efectivo de la caja


def resumen(db: Session, caja: CashSession) -> dict:
    """Cuadre de caja: efectivo esperado = base + ventas en efectivo + abonos - gastos."""
    movs = db.query(CashTransaction).filter(CashTransaction.session_id == caja.id).all()
    ventas_efectivo = sum(m.amount for m in movs if m.type == "venta" and m.payment_method == "efectivo")
    ventas_otros = sum(m.amount for m in movs if m.type == "venta" and m.payment_method != "efectivo")
    abonos = sum(m.amount for m in movs if m.type == "abono")
    gastos = sum(m.amount for m in movs if m.type == "gasto")
    esperado = caja.initial_cash + ventas_efectivo + abonos - gastos
    return {"id": caja.id, "status": caja.status, "user_name": caja.user_name, "location_id": caja.location_id,
            "opened_at": caja.opened_at.isoformat(timespec="seconds"),
            "closed_at": caja.closed_at.isoformat(timespec="seconds") if caja.closed_at else None,
            "initial_cash": caja.initial_cash, "cash_sales": ventas_efectivo, "other_sales": ventas_otros,
            "payments": abonos, "expenses": gastos, "expected_cash": round(esperado, 2),
            "num_sales": sum(1 for m in movs if m.type == "venta"),
            "actual_cash": caja.actual_cash, "difference": caja.difference, "notes": caja.notes}


def _user(x_user_id: str) -> int:
    if not x_user_id.isdigit():
        raise HTTPException(401, "Usuario no identificado")
    return int(x_user_id)


@app.get("/health")
def health():
    if not db_ok():
        return JSONResponse({"status": "error", "replica": REPLICA}, status_code=503)
    return {"status": "ok", "replica": REPLICA}


# ── Comandos ───────────────────────────────────────────────
@app.post("/api/cash/sessions/open", status_code=201)
def open_session(data: OpenIn, db: Session = Depends(get_db),
                 x_user_id: str = Header(default=""), x_user_name: str = Header(default="")):
    user_id = _user(x_user_id)
    abierta = db.query(CashSession).filter_by(user_id=user_id, location_id=data.location_id, status="open").first()
    if abierta:
        raise HTTPException(409, f"Ya tienes la caja #{abierta.id} abierta en esta sede")
    caja = CashSession(user_id=user_id, user_name=x_user_name, location_id=data.location_id,
                       opened_at=ahora(), initial_cash=data.initial_cash)
    db.add(caja)
    db.commit()
    OPEN_SESSIONS.inc()
    return resumen(db, caja)


@app.post("/api/cash/sessions/{session_id}/close")
def close_session(session_id: int, data: CloseIn, db: Session = Depends(get_db)):
    caja = db.get(CashSession, session_id)
    if not caja or caja.status != "open":
        raise HTTPException(404, "La caja no existe o ya está cerrada")
    cuadre = resumen(db, caja)
    caja.expected_cash = cuadre["expected_cash"]
    caja.actual_cash = data.actual_cash
    caja.difference = round(data.actual_cash - cuadre["expected_cash"], 2)
    caja.notes = data.notes
    caja.status = "closed"
    caja.closed_at = ahora()
    db.commit()
    CLOSE_DIFF.set(caja.difference)
    log.info("Caja #%s cerrada: esperado %.0f, contado %.0f, diferencia %.0f",
             caja.id, caja.expected_cash, caja.actual_cash, caja.difference)
    return resumen(db, caja)


@app.post("/api/cash/expenses", status_code=201)
def create_expense(data: ExpenseIn, db: Session = Depends(get_db), x_user_id: str = Header(default="")):
    momento = ahora()
    gasto = Expense(user_id=_user(x_user_id), session_id=data.session_id, location_id=data.location_id,
                    date=momento.date(), category=data.category, amount=data.amount,
                    description=data.description, timestamp=momento)
    db.add(gasto)
    if data.session_id:
        caja = db.get(CashSession, data.session_id)
        if not caja or caja.status != "open":
            raise HTTPException(400, "La caja indicada no está abierta")
        db.add(CashTransaction(session_id=caja.id, type="gasto", amount=data.amount,
                               description=f"{data.category}: {data.description}", timestamp=momento))
    db.commit()
    EXPENSES.labels(data.category).inc(data.amount)
    return {"id": gasto.id, "category": gasto.category, "amount": gasto.amount}


# ── Consultas ──────────────────────────────────────────────
@app.get("/api/cash/sessions/current")
def current_session(location_id: int, db: Session = Depends(get_db), x_user_id: str = Header(default="")):
    caja = db.query(CashSession).filter_by(user_id=_user(x_user_id), location_id=location_id,
                                           status="open").first()
    if not caja:
        raise HTTPException(404, "No tienes una caja abierta en esta sede")
    return resumen(db, caja)


@app.get("/api/cash/sessions")
def list_sessions(limit: int = 20, db: Session = Depends(get_db)):
    cajas = db.query(CashSession).order_by(CashSession.id.desc()).limit(min(limit, 100))
    return [resumen(db, c) for c in cajas]


@app.get("/api/cash/expenses")
def list_expenses(days: int = 30, db: Session = Depends(get_db)):
    desde = ahora().date() - timedelta(days=days)
    gastos = db.query(Expense).filter(Expense.date >= desde).order_by(Expense.id.desc())
    return [{"id": g.id, "date": g.date.isoformat(), "category": g.category, "amount": g.amount,
             "description": g.description, "location_id": g.location_id} for g in gastos]
