"""Saca las credenciales de Factus de la tabla config del monolito y las guarda como
archivo para un Docker secret (secrets/factus_credentials.json).

En el monolito las credenciales estaban en texto plano dentro de la BD; en GestPro Cloud
viajan como Docker secret y solo las ve el servicio billing-factus.
Si no está la BD del monolito, crea un secreto vacío y la facturación queda en modo simulado.
Uso:  python scripts/extraer_secreto_factus.py [ruta a GestPro.db]
"""
import json
import sqlite3
import sys
from pathlib import Path

raiz = Path(__file__).resolve().parent.parent
origen = Path(sys.argv[1]) if len(sys.argv) > 1 else raiz.parent / "dist" / "GestPro.db"
destino = raiz / "secrets" / "factus_credentials.json"
destino.parent.mkdir(exist_ok=True)

datos = {}
if origen.is_file():
    con = sqlite3.connect(f"file:{origen}?mode=ro", uri=True)
    try:
        datos = dict(con.execute("SELECT key, value FROM config WHERE key LIKE 'factus_%'").fetchall())
    except sqlite3.Error as e:
        print(f"No se pudieron leer las credenciales de {origen}: {e}")
    finally:
        con.close()

destino.write_text(json.dumps(datos, indent=2), encoding="utf-8")
if datos:
    print(f"Secreto de Factus creado: {destino.relative_to(raiz)} ({len(datos)} claves, entorno "
          f"{datos.get('factus_environment', '?')})")
else:
    print(f"Sin credenciales de Factus ({origen} no existe o no las tiene): secreto vacío, "
          "la facturación quedará en modo simulado.")
