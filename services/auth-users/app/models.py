"""Tablas del servicio auth-users (migradas de GestPro: users, config)."""
from sqlalchemy import Boolean, Column, Integer, String

from .database import Base


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False)
    password = Column(String(200), nullable=False)   # pbkdf2 (o sha256 heredado del monolito)
    role = Column(String(20), nullable=False, default="vendedor")   # admin | vendedor
    active = Column(Boolean, nullable=False, default=True)


class Config(Base):
    __tablename__ = "config"
    key = Column(String(80), primary_key=True)
    value = Column(String(500), nullable=False, default="")
