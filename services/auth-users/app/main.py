"""auth-users: login con JWT, usuarios y roles, configuración del negocio.

Comandos (escriben):  POST /api/auth/login, POST /api/auth/users
Consultas (leen):     GET  /api/auth/me, GET /api/auth/users, GET /api/auth/config
"""
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from prometheus_client import Counter
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .database import SessionLocal, db_ok, get_db, init_db
from .models import Config, User
from .observability import REPLICA, instrument
from .security import create_token, hash_password, is_legacy_hash, verify_password

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("auth-users")

LOGINS = Counter("gestpro_logins_total", "Intentos de inicio de sesión", ["result"])


def seed():
    """Si la BD está vacía, crea el admin por defecto (igual que el monolito)."""
    db = SessionLocal()
    try:
        if not db.query(User).first():
            db.add(User(username="admin", password=hash_password("admin123"), role="admin"))
            db.commit()
            log.info("Usuario admin creado por defecto")
    except Exception:
        db.rollback()  # otra réplica lo creó al mismo tiempo
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app):
    init_db()
    seed()
    yield


app = FastAPI(title="GestPro · auth-users", lifespan=lifespan)
instrument(app)


def require_admin(x_user_role: str = Header(default="")):
    # El rol viene del JWT, validado y copiado en la cabecera por HAProxy
    if x_user_role != "admin":
        raise HTTPException(403, "Solo un administrador puede hacer esta operación")


class LoginIn(BaseModel):
    username: str
    password: str


class UserIn(BaseModel):
    username: str
    password: str
    role: str = "vendedor"


def user_out(u: User) -> dict:
    return {"id": u.id, "username": u.username, "role": u.role, "active": u.active}


@app.get("/health")
def health():
    if not db_ok():
        return JSONResponse({"status": "error", "replica": REPLICA}, status_code=503)
    return {"status": "ok", "replica": REPLICA}


# ── Comandos ───────────────────────────────────────────────
@app.post("/api/auth/login")
def login(data: LoginIn, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == data.username, User.active.is_(True)).first()
    if not user or not verify_password(data.password, user.password):
        LOGINS.labels("fallido").inc()
        raise HTTPException(401, "Usuario o contraseña incorrectos")
    if is_legacy_hash(user.password):          # usuario migrado del monolito
        user.password = hash_password(data.password)
        db.commit()
    LOGINS.labels("exitoso").inc()
    return {"access_token": create_token(user.id, user.username, user.role),
            "token_type": "bearer", "user": user_out(user)}


@app.post("/api/auth/users", dependencies=[Depends(require_admin)], status_code=201)
def create_user(data: UserIn, db: Session = Depends(get_db)):
    if data.role not in ("admin", "vendedor"):
        raise HTTPException(400, "Rol inválido (admin | vendedor)")
    if db.query(User).filter(User.username == data.username).first():
        raise HTTPException(409, "El usuario ya existe")
    user = User(username=data.username, password=hash_password(data.password), role=data.role)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user_out(user)


# ── Consultas ──────────────────────────────────────────────
@app.get("/api/auth/me")
def me(x_user_id: str = Header(default=""), x_user_name: str = Header(default=""),
       x_user_role: str = Header(default="")):
    return {"id": x_user_id, "username": x_user_name, "role": x_user_role}


@app.get("/api/auth/users", dependencies=[Depends(require_admin)])
def list_users(db: Session = Depends(get_db)):
    return [user_out(u) for u in db.query(User).order_by(User.username)]


@app.get("/api/auth/config")
def get_config(db: Session = Depends(get_db)):
    return {c.key: c.value for c in db.query(Config)}
