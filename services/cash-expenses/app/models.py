"""Tablas del servicio cash-expenses (migradas de GestPro: cash_sessions, cash_transactions, expenses).

cash_transactions guarda una COPIA local de los movimientos que llegan por eventos
(ventas y abonos). Así el cierre de caja no depende de consultar la BD de otro servicio.
"""
from sqlalchemy import Column, Date, DateTime, Float, ForeignKey, Integer, String

from .database import Base


class CashSession(Base):
    __tablename__ = "cash_sessions"           # turno de caja (apertura -> cierre)
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)
    user_name = Column(String(50), nullable=False, default="")
    location_id = Column(Integer, nullable=False)
    opened_at = Column(DateTime, nullable=False)
    closed_at = Column(DateTime)
    initial_cash = Column(Float, nullable=False, default=0)
    actual_cash = Column(Float, default=0)
    expected_cash = Column(Float, default=0)
    difference = Column(Float, default=0)
    notes = Column(String(300), nullable=False, default="")
    status = Column(String(10), nullable=False, default="open")   # open | closed


class CashTransaction(Base):
    __tablename__ = "cash_transactions"
    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("cash_sessions.id", ondelete="CASCADE"), nullable=False)
    type = Column(String(20), nullable=False)          # venta | abono | gasto
    payment_method = Column(String(20), nullable=False, default="efectivo")
    amount = Column(Float, nullable=False)
    description = Column(String(300), nullable=False, default="")
    timestamp = Column(DateTime, nullable=False)


class Expense(Base):
    __tablename__ = "expenses"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)
    session_id = Column(Integer)
    location_id = Column(Integer)
    date = Column(Date, nullable=False)
    category = Column(String(60), nullable=False)
    amount = Column(Float, nullable=False)
    description = Column(String(300), nullable=False, default="")
    timestamp = Column(DateTime, nullable=False)
