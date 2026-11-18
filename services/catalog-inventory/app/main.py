"""catalog-inventory: catálogo de productos, sedes y stock por sede.

Comandos (escriben):  POST /api/inventory/products, POST /api/inventory/products/import (Excel),
                      POST /api/inventory/locations
Consultas (leen):     GET /api/inventory/stock, GET /api/inventory/products/{id},
                      GET /api/inventory/alerts, GET /api/inventory/locations, GET /api/inventory/categories
Eventos:              consume sale.created  -> descuenta el stock de la sede
                      publica stock.low     -> cuando un producto queda en o bajo su mínimo
"""
import io
import logging
import random
from contextlib import asynccontextmanager

import openpyxl
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from prometheus_client import Counter, Gauge
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import events
from .database import SessionLocal, db_ok, get_db, init_db
from .models import Category, Location, PosInventory, Product
from .observability import REPLICA, instrument

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("catalog-inventory")

STOCK_LOW = Counter("gestpro_stock_low_alerts_total", "Alertas de stock bajo generadas", ["location"])
UNITS_SOLD = Counter("gestpro_units_discounted_total", "Unidades descontadas del inventario por ventas")
LOW_STOCK_PRODUCTS = Gauge("gestpro_low_stock_products", "Productos en o bajo su stock mínimo", ["location"])


def metricas_negocio():
    """Se calcula desde la BD en cada scrape: es exacta aunque una réplica se reinicie."""
    db = SessionLocal()
    try:
        for loc in db.query(Location).filter(Location.active.is_(True)):
            # solo productos que la sede maneja (tienen registro de stock en ella)
            bajos = (db.query(PosInventory).join(Product, Product.id == PosInventory.product_id)
                     .filter(PosInventory.location_id == loc.id, Product.active.is_(True),
                             PosInventory.stock <= Product.min_stock).count())
            LOW_STOCK_PRODUCTS.labels(loc.name).set(bajos)
    finally:
        db.close()

CATEGORIAS_BASE = ["General", "Electrónica", "Ropa", "Alimentos", "Hogar",
                   "Deportes", "Libros", "Bebidas", "Juguetes", "Salud"]


# ── Consumidor de eventos ───────────────────────────────────
def on_sale_created(evt: dict):
    """Una venta descuenta stock en la sede donde se hizo (consistencia eventual)."""
    db = SessionLocal()
    alertas = []
    try:
        loc_id = evt["location_id"]
        for item in evt["items"]:
            fila = (db.query(PosInventory)
                    .filter_by(location_id=loc_id, product_id=item["product_id"])
                    .with_for_update().first())          # bloqueo de fila: ventas simultáneas
            if fila is None:
                fila = PosInventory(location_id=loc_id, product_id=item["product_id"], stock=0)
                db.add(fila)
            fila.stock -= int(item["quantity"])
            UNITS_SOLD.inc(int(item["quantity"]))
            producto = db.get(Product, item["product_id"])
            if producto and fila.stock <= producto.min_stock:
                alertas.append({"product_id": producto.id, "product_name": producto.name,
                                "location_id": loc_id, "location_name": evt.get("location_name", ""),
                                "stock": fila.stock, "min_stock": producto.min_stock,
                                "sale_id": evt["sale_id"]})
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    for alerta in alertas:
        events.publish("stock.low", alerta)
        STOCK_LOW.labels(alerta["location_name"] or str(alerta["location_id"])).inc()
        log.warning("Stock bajo: %s en sede %s (quedan %s)", alerta["product_name"],
                    alerta["location_name"], alerta["stock"])


def seed():
    db = SessionLocal()
    try:
        if not db.query(Category).first():
            db.add_all([Category(name=c) for c in CATEGORIAS_BASE])
        if not db.query(Location).first():
            db.add(Location(name="Principal"))
        db.commit()
    except Exception:
        db.rollback()   # otra réplica hizo el seed al mismo tiempo
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app):
    init_db()
    seed()
    events.start_consumer("inventory", {"sale.created": on_sale_created})
    yield


app = FastAPI(title="GestPro · catalog-inventory", lifespan=lifespan)
instrument(app, on_scrape=metricas_negocio)


def require_admin(x_user_role: str = Header(default="")):
    if x_user_role != "admin":
        raise HTTPException(403, "Solo un administrador puede hacer esta operación")


def _location(db: Session, location_id: int | None) -> Location:
    if location_id:
        loc = db.get(Location, location_id)
    else:
        loc = db.query(Location).filter(Location.active.is_(True)).order_by(Location.id).first()
    if not loc:
        raise HTTPException(404, "Sede no encontrada")
    return loc


def _new_sku(db: Session) -> int:
    while True:
        sku = random.randint(100000, 999999)
        if not db.query(Product).filter(Product.sku == sku).first():
            return sku


def _add_stock(db: Session, location_id: int, product_id: int, cantidad: int):
    fila = db.query(PosInventory).filter_by(location_id=location_id, product_id=product_id).first()
    if fila:
        fila.stock += cantidad
    else:
        db.add(PosInventory(location_id=location_id, product_id=product_id, stock=cantidad))


class ProductIn(BaseModel):
    name: str
    category: str = "General"
    price: float = Field(gt=0)
    cost: float = Field(default=0, ge=0)
    min_stock: int = 5
    unit: str = "unidad"
    supplier: str = ""
    stock: int = Field(default=0, ge=0)
    location_id: int | None = None


class LocationIn(BaseModel):
    name: str
    address: str = ""
    phone: str = ""


@app.get("/health")
def health():
    if not db_ok():
        return JSONResponse({"status": "error", "replica": REPLICA}, status_code=503)
    return {"status": "ok", "replica": REPLICA}


# ── Comandos ───────────────────────────────────────────────
@app.post("/api/inventory/products", dependencies=[Depends(require_admin)], status_code=201)
def create_product(data: ProductIn, db: Session = Depends(get_db)):
    loc = _location(db, data.location_id)
    if not db.query(Category).filter(Category.name == data.category).first():
        db.add(Category(name=data.category))
    producto = Product(name=data.name.strip(), sku=_new_sku(db), category=data.category,
                       price=data.price, cost=data.cost, min_stock=data.min_stock,
                       unit=data.unit, supplier=data.supplier)
    db.add(producto)
    db.flush()
    _add_stock(db, loc.id, producto.id, data.stock)
    db.commit()
    return {"id": producto.id, "sku": producto.sku, "name": producto.name, "location": loc.name}


# Mismas columnas que aceptaba el importador del monolito
COLUMNAS_EXCEL = {
    "name":     ["Nombre", "nombre", "Producto", "producto"],
    "cost":     ["PrecioCompra", "precio_compra", "Precio Compra", "Costo", "costo"],
    "price":    ["PrecioVenta", "precio_venta", "Precio Venta", "Precio", "precio"],
    "stock":    ["Cantidad", "cantidad", "Stock", "stock"],
    "category": ["Categoría", "Categoria", "categoria", "Category"],
    "min_stock": ["StockMinimo", "Stock Mínimo", "stock_minimo"],
    "supplier": ["Proveedor", "proveedor"],
    "unit":     ["Unidad", "unidad", "Unit"],
}


@app.post("/api/inventory/products/import", dependencies=[Depends(require_admin)])
def import_excel(file: UploadFile = File(...), location_id: int | None = Form(None),
                 db: Session = Depends(get_db)):
    loc = _location(db, location_id)
    try:
        wb = openpyxl.load_workbook(io.BytesIO(file.file.read()), read_only=True, data_only=True)
    except Exception:
        raise HTTPException(400, "El archivo no es un Excel válido (.xlsx)")
    filas = wb.active.iter_rows(values_only=True)
    encabezado = [str(c).strip() if c is not None else "" for c in next(filas, [])]
    col = {}
    for campo, opciones in COLUMNAS_EXCEL.items():
        for opcion in opciones:
            if opcion in encabezado:
                col[campo] = encabezado.index(opcion)
                break
    if "name" not in col:
        raise HTTPException(400, "No se encontró la columna 'Nombre'")

    existentes = {n.lower(): pid for (n, pid) in db.query(Product.name, Product.id)}
    en_sede = {pid for (pid,) in db.query(PosInventory.product_id).filter_by(location_id=loc.id)}
    categorias = {c for (c,) in db.query(Category.name)}
    importados = stock_sede = duplicados = errores = 0
    for fila in filas:
        try:
            def valor(campo, defecto=None):
                v = fila[col[campo]] if campo in col else None
                return defecto if v in (None, "") else v

            nombre = str(valor("name", "")).strip()
            if not nombre:
                errores += 1
                continue
            if nombre.lower() in existentes:
                pid = existentes[nombre.lower()]
                if pid in en_sede:          # ya existe en el catálogo y en esta sede
                    duplicados += 1
                else:                       # existe en el catálogo: solo se carga el stock de esta sede
                    _add_stock(db, loc.id, pid, max(int(float(valor("stock", 0))), 0))
                    en_sede.add(pid)
                    stock_sede += 1
                continue
            costo = max(float(valor("cost", 0)), 0)
            precio = float(valor("price", costo * 1.5)) or costo * 1.5
            categoria = str(valor("category", "General")).strip()
            if categoria not in categorias:
                db.add(Category(name=categoria))
                categorias.add(categoria)
            producto = Product(name=nombre, sku=_new_sku(db), category=categoria, price=precio,
                               cost=costo, min_stock=int(valor("min_stock", 5)),
                               supplier=str(valor("supplier", "Importado")),
                               unit=str(valor("unit", "unidad")))
            db.add(producto)
            db.flush()
            _add_stock(db, loc.id, producto.id, max(int(float(valor("stock", 0))), 0))
            existentes[nombre.lower()] = producto.id
            en_sede.add(producto.id)
            importados += 1
        except Exception as exc:
            log.warning("Fila inválida en el Excel: %s", exc)
            errores += 1
    db.commit()
    log.info("Excel importado en %s: %s nuevos, %s con stock nuevo en la sede, %s duplicados, %s errores",
             loc.name, importados, stock_sede, duplicados, errores)
    return {"sede": loc.name, "importados": importados, "stock_en_sede": stock_sede,
            "duplicados": duplicados, "errores": errores}


@app.post("/api/inventory/locations", dependencies=[Depends(require_admin)], status_code=201)
def create_location(data: LocationIn, db: Session = Depends(get_db)):
    if db.query(Location).filter(Location.name == data.name).first():
        raise HTTPException(409, "Ya existe una sede con ese nombre")
    loc = Location(name=data.name, address=data.address, phone=data.phone)
    db.add(loc)
    db.commit()
    return {"id": loc.id, "name": loc.name}


# ── Consultas ──────────────────────────────────────────────
@app.get("/api/inventory/locations")
def list_locations(db: Session = Depends(get_db)):
    return [{"id": l.id, "name": l.name, "address": l.address, "phone": l.phone}
            for l in db.query(Location).filter(Location.active.is_(True)).order_by(Location.id)]


@app.get("/api/inventory/categories")
def list_categories(db: Session = Depends(get_db)):
    return [c.name for c in db.query(Category).order_by(Category.name)]


def _stock_query(db: Session, loc_id: int):
    stock = func.coalesce(PosInventory.stock, 0)
    return (db.query(Product, stock.label("stock"))
            .outerjoin(PosInventory, (PosInventory.product_id == Product.id)
                       & (PosInventory.location_id == loc_id))
            .filter(Product.active.is_(True)))


def _stock_out(p: Product, stock: int) -> dict:
    return {"product_id": p.id, "sku": p.sku, "name": p.name, "category": p.category,
            "price": p.price, "stock": stock, "min_stock": p.min_stock,
            "low": stock <= p.min_stock}


@app.get("/api/inventory/stock")
def stock_by_location(location_id: int | None = None, search: str = "", db: Session = Depends(get_db)):
    loc = _location(db, location_id)
    q = _stock_query(db, loc.id)
    if search:
        q = q.filter(Product.name.ilike(f"%{search}%"))
    return {"location": {"id": loc.id, "name": loc.name},
            "items": [_stock_out(p, s) for p, s in q.order_by(Product.name)]}


@app.get("/api/inventory/alerts")
def low_stock(location_id: int | None = None, db: Session = Depends(get_db)):
    loc = _location(db, location_id)
    # solo productos que la sede maneja (con registro de stock en ella)
    filas = _stock_query(db, loc.id).filter(PosInventory.stock <= Product.min_stock)
    return {"location": loc.name, "items": [_stock_out(p, s) for p, s in filas.order_by("stock")]}


@app.get("/api/inventory/products/{product_id}")
def get_product(product_id: int, location_id: int | None = None, db: Session = Depends(get_db)):
    """Lo consulta pos-sales de forma SÍNCRONA antes de vender (precio y stock actual)."""
    p = db.get(Product, product_id)
    if not p or not p.active:
        raise HTTPException(404, "Producto no encontrado")
    loc = _location(db, location_id)
    fila = db.query(PosInventory).filter_by(location_id=loc.id, product_id=p.id).first()
    return {"id": p.id, "name": p.name, "category": p.category, "price": p.price,
            "stock": fila.stock if fila else 0, "min_stock": p.min_stock,
            "location_id": loc.id, "location_name": loc.name}
