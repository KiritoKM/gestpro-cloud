"""Contraseñas y tokens JWT.

- Las contraseñas nuevas se guardan con PBKDF2-HMAC-SHA256 + sal.
- Los usuarios migrados del monolito traen SHA-256 sin sal: se aceptan una vez
  y se re-encriptan con PBKDF2 en el primer login (migración transparente).
- El JWT se firma con HS256 (HMAC-SHA256, la misma primitiva de las licencias de GestPro).
  Quien lo valida es HAProxy, con el mismo secreto (Docker secret jwt_secret).
"""
import hashlib
import hmac
import os
import time

import jwt

ITERACIONES = 100_000
TOKEN_HORAS = 8


def _jwt_secret() -> str:
    path = os.getenv("JWT_SECRET_FILE", "/run/secrets/jwt_secret")
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    return os.getenv("JWT_SECRET", "dev-secret")


def hash_password(password: str) -> str:
    sal = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), sal, ITERACIONES)
    return f"pbkdf2${ITERACIONES}${sal.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if stored.startswith("pbkdf2$"):
        _, iters, sal, digest = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(sal), int(iters))
        return hmac.compare_digest(calc.hex(), digest)
    # Formato heredado del monolito: sha256(password) en hexadecimal
    return hmac.compare_digest(hashlib.sha256(password.encode()).hexdigest(), stored)


def is_legacy_hash(stored: str) -> bool:
    return not stored.startswith("pbkdf2$")


def create_token(user_id: int, username: str, role: str) -> str:
    ahora = int(time.time())
    payload = {"sub": str(user_id), "name": username, "role": role,
               "iat": ahora, "exp": ahora + TOKEN_HORAS * 3600}
    return jwt.encode(payload, _jwt_secret(), algorithm="HS256")
