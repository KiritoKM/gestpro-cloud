"""Tabla del servicio billing-factus (migrada de GestPro: invoices)."""
from sqlalchemy import Column, Date, Float, Integer, String, Text

from .database import Base


class Invoice(Base):
    __tablename__ = "invoices"
    id = Column(Integer, primary_key=True)
    sale_id = Column(Integer, unique=True, nullable=False)     # una factura por venta (idempotencia)
    reference_code = Column(String(60), nullable=False)        # referencia única enviada a Factus
    number = Column(String(40), nullable=False, default="")    # número DIAN (p. ej. SETP990036637)
    date = Column(Date, nullable=False)
    time = Column(String(8), nullable=False)
    subtotal = Column(Float, nullable=False, default=0)
    discount = Column(Float, nullable=False, default=0)
    tax_iva = Column(Float, nullable=False, default=0)
    total = Column(Float, nullable=False, default=0)
    payment_method = Column(String(20), nullable=False, default="efectivo")
    customer_name = Column(String(150), nullable=False, default="Consumidor Final")
    customer_doc = Column(String(40), nullable=False, default="")
    cufe = Column(String(120), nullable=False, default="")     # código único de la factura ante la DIAN
    pdf_url = Column(String(300), nullable=False, default="")
    qr_url = Column(String(300), nullable=False, default="")
    dian_status = Column(String(20), nullable=False, default="pendiente")  # pendiente|emitida|error|simulada
    error = Column(String(500), nullable=False, default="")
    attempts = Column(Integer, nullable=False, default=0)
    sale_data = Column(Text, nullable=False, default="{}")     # evento original (ítems y cliente) para reintentos
