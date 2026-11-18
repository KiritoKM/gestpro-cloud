"""Conexión a PostgreSQL (una base de datos propia por microservicio)."""
import logging
import os
import time
from urllib.parse import quote_plus

from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

log = logging.getLogger("database")


def _read_password() -> str:
    # En Swarm la contraseña llega como Docker secret (archivo), nunca en el código
    path = os.getenv("DB_PASSWORD_FILE", "/run/secrets/db_password")
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    return os.getenv("DB_PASSWORD", "gestpro")


DATABASE_URL = "postgresql://{user}:{pwd}@{host}:5432/{name}".format(
    user=os.getenv("DB_USER", "gestpro"),
    pwd=quote_plus(_read_password()),
    host=os.getenv("DB_HOST", "localhost"),
    name=os.getenv("DB_NAME", "postgres"),
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=5, max_overflow=5)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db(retries: int = 30):
    """Crea las tablas. Reintenta porque en Swarm la BD puede arrancar después que el servicio."""
    for intento in range(1, retries + 1):
        try:
            Base.metadata.create_all(bind=engine)
            return
        except Exception as exc:  # BD aún no lista, u otra réplica creando las tablas a la vez
            log.warning("BD no disponible (intento %s/%s): %s", intento, retries, exc)
            time.sleep(2)
    raise RuntimeError("No fue posible conectar con la base de datos")


def db_ok() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
