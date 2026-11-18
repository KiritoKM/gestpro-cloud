"""Tablas del servicio pos-sales.

Modelo de ESCRITURA (migrado de GestPro): sales, sale_items.
Modelo de LECTURA (CQRS): sales_summary, un resumen por día y sede que se
actualiza de forma asíncrona desde el bus de eventos. Las consultas
consolidadas leen de aquí, nunca recalculan sobre las tablas transaccionales.
"""
from sqlalchemy import Boolean, Column, Date, Float, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from .database import Base


class Sale(Base):
    __tablename__ = "sales"
    id = Column(Integer, primary_key=True)
    client_id = Column(Integer)                  # id en clients-credits (otra BD: sin FK)
    user_id = Column(Integer)                    # id en auth-users
    user_name = Column(String(50), nullable=False, default="")
    session_id = Column(Integer)                 # id de la caja en cash-expenses
    location_id = Column(Integer, nullable=False)
    location_name = Column(String(80), nullable=False, default="")
    date = Column(Date, nullable=False)
    time = Column(String(8), nullable=False)
    subtotal = Column(Float, nullable=False, default=0)
    discount = Column(Float, nullable=False, default=0)
    total = Column(Float, nullable=False, default=0)
    payment_method = Column(String(20), nullable=False, default="efectivo")
    invoice_requested = Column(Boolean, nullable=False, default=False)
    notes = Column(String(300), nullable=False, default="")
    items = relationship("SaleItem", cascade="all, delete-orphan", lazy="selectin")


class SaleItem(Base):
    __tablename__ = "sale_items"
    id = Column(Integer, primary_key=True)
    sale_id = Column(Integer, ForeignKey("sales.id", ondelete="CASCADE"), nullable=False)
    product_id = Column(Integer)
    product_name = Column(String(150), nullable=False)
    category = Column(String(80), nullable=False, default="General")
    quantity = Column(Integer, nullable=False)
    unit_price = Column(Float, nullable=False)
    subtotal = Column(Float, nullable=False)


class SalesSummary(Base):
    __tablename__ = "sales_summary"
    date = Column(Date, primary_key=True)
    location_id = Column(Integer, primary_key=True)
    location_name = Column(String(80), nullable=False, default="")
    num_sales = Column(Integer, nullable=False, default=0)
    revenue = Column(Float, nullable=False, default=0)
