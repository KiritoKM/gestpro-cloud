"""Tablas del servicio catalog-inventory (migradas de GestPro).

categories, products, pos_locations, pos_inventory, inventory_transfers.
El stock vive por sede en pos_inventory; el stock total de un producto es la suma.
"""
from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint

from .database import Base


class Category(Base):
    __tablename__ = "categories"
    id = Column(Integer, primary_key=True)
    name = Column(String(80), unique=True, nullable=False)


class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True)
    name = Column(String(150), nullable=False)
    sku = Column(Integer, unique=True)
    barcode = Column(String(60), nullable=False, default="")
    category = Column(String(80), nullable=False, default="General")
    price = Column(Float, nullable=False, default=0)
    cost = Column(Float, nullable=False, default=0)
    supplier = Column(String(120), nullable=False, default="")
    min_stock = Column(Integer, nullable=False, default=5)
    unit = Column(String(30), nullable=False, default="unidad")
    active = Column(Boolean, nullable=False, default=True)


class Location(Base):
    __tablename__ = "pos_locations"      # sedes / puntos de venta
    id = Column(Integer, primary_key=True)
    name = Column(String(80), unique=True, nullable=False)
    address = Column(String(200), nullable=False, default="")
    phone = Column(String(40), nullable=False, default="")
    active = Column(Boolean, nullable=False, default=True)


class PosInventory(Base):
    __tablename__ = "pos_inventory"      # stock de cada producto en cada sede
    id = Column(Integer, primary_key=True)
    location_id = Column(Integer, ForeignKey("pos_locations.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    stock = Column(Integer, nullable=False, default=0)
    __table_args__ = (UniqueConstraint("location_id", "product_id"),)


class InventoryTransfer(Base):
    __tablename__ = "inventory_transfers"
    id = Column(Integer, primary_key=True)
    from_location_id = Column(Integer, ForeignKey("pos_locations.id"), nullable=False)
    to_location_id = Column(Integer, ForeignKey("pos_locations.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    quantity = Column(Integer, nullable=False)
    user_id = Column(Integer, nullable=False)
    timestamp = Column(DateTime, nullable=False)
    notes = Column(String(300), nullable=False, default="")
