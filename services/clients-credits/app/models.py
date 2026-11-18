"""Tablas del servicio clients-credits (migradas de GestPro: clients, client_payments)."""
from sqlalchemy import Column, Date, DateTime, Float, ForeignKey, Integer, String

from .database import Base


class Client(Base):
    __tablename__ = "clients"
    id = Column(Integer, primary_key=True)
    name = Column(String(150), nullable=False)
    document = Column(String(40), nullable=False, default="")
    doc_type = Column(String(5), nullable=False, default="CC")    # para la factura DIAN
    phone = Column(String(40), nullable=False, default="")
    email = Column(String(120), nullable=False, default="")
    address = Column(String(200), nullable=False, default="")
    notes = Column(String(300), nullable=False, default="")
    debt = Column(Float, nullable=False, default=0)                # saldo a crédito
    created_at = Column(Date, nullable=False)


class ClientPayment(Base):
    __tablename__ = "client_payments"                              # abonos a la deuda
    id = Column(Integer, primary_key=True)
    client_id = Column(Integer, ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer)
    session_id = Column(Integer)                                   # caja abierta (cash-expenses)
    amount = Column(Float, nullable=False)
    notes = Column(String(300), nullable=False, default="")
    timestamp = Column(DateTime, nullable=False)
