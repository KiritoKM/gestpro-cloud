"""Worker MapReduce: procesa particiones en paralelo con otros workers (réplicas en Swarm).

Por cada partición (un rango de bytes de un archivo bronze, como un input split de Hadoop):
  1. MAP: aplica mapper.map_line a cada línea; descarta las inválidas.
  2. Escribe las filas válidas en la capa SILVER (silver/job_<id>/part-NNNN.csv).
  3. COMBINER: resume por clave (n, suma, suma_cuadrados) para enviar poco por la red.
  4. Guarda el resultado parcial en Redis y confirma la tarea.

Cola confiable (corrige la pérdida de tareas de BRPOP en el lab del curso):
  - BLMOVE mueve la tarea de "mr:tasks" a "mr:processing" en un solo paso atómico.
  - Mientras trabaja, el worker renueva un "lease" (clave con expiración).
  - Si el worker muere, el lease expira y el coordinador devuelve la tarea a la cola.
"""
import json
import logging
import os
import socket
import time
from pathlib import Path

import redis
from prometheus_client import Counter, start_http_server

from mapper import map_line

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] worker: %(message)s")
log = logging.getLogger("worker")

TASKS, PROCESSING = "mr:tasks", "mr:processing"
LEASE_S = 8
DATA = Path(os.getenv("DATA_DIR", "/data"))
WORKER = socket.gethostname()
r = redis.Redis(host=os.getenv("REDIS_HOST", "redis"), decode_responses=True, socket_timeout=15)

PARTITIONS = Counter("gestpro_mr_partitions_total", "Particiones procesadas por este worker")
ROWS = Counter("gestpro_mr_rows_total", "Líneas procesadas por este worker", ["status"])
ROWS.labels("valida")
ROWS.labels("descartada")


def procesar(tarea: dict):
    job, part = tarea["job"], tarea["part"]
    lease = f"mr:lease:{job}:{part}"
    r.set(lease, WORKER, ex=LEASE_S)
    inicio = time.perf_counter()
    grupos, leidas, validas = {}, 0, 0
    silver = DATA / "silver" / f"job_{job}" / f"part-{part:04d}.csv"
    silver.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA / "bronze" / tarea["file"], "rb") as entrada, open(silver, "w", encoding="utf-8") as salida:
        # Split por bytes: si no empezamos al inicio del archivo, la línea cortada es de la partición anterior
        if tarea["start"] > 0:
            entrada.seek(tarea["start"] - 1)
            entrada.readline()
        while entrada.tell() < tarea["end"]:
            linea = entrada.readline()
            if not linea:
                break
            leidas += 1
            r_map = map_line(linea.decode("utf-8", errors="replace"))
            if r_map:
                clave, valor, campos = r_map
                g = grupos.setdefault(clave, [0, 0.0, 0.0])
                g[0] += 1
                g[1] += valor
                g[2] += valor * valor
                salida.write(",".join(map(str, campos)) + "\n")
                validas += 1
            if leidas % 50_000 == 0:
                r.set(lease, WORKER, ex=LEASE_S)       # sigo vivo: renuevo el lease
    encabezado = 1 if tarea["start"] == 0 else 0      # la línea de encabezado no cuenta como descartada
    descartadas = leidas - validas - encabezado
    parcial = {"part": part, "worker": WORKER, "rows_in": leidas - encabezado, "rows_valid": validas,
               "rows_discarded": descartadas, "groups": grupos,
               "seconds": round(time.perf_counter() - inicio, 3)}
    # Guardar el parcial (idempotente: si se repite la partición, se sobrescribe) y confirmar la tarea
    r.hset(f"mr:{job}:partials", str(part), json.dumps(parcial))
    r.delete(lease)
    PARTITIONS.inc()
    ROWS.labels("valida").inc(validas)
    ROWS.labels("descartada").inc(descartadas)
    log.info("Job %s · partición %s (%s) -> %s líneas válidas, %s descartadas en %.2f s",
             job, part, tarea["file"], validas, descartadas, parcial["seconds"])


def main():
    start_http_server(8000)            # /metrics para Prometheus
    log.info("Worker %s listo, esperando particiones", WORKER)
    while True:
        try:
            crudo = r.blmove(TASKS, PROCESSING, timeout=5, src="RIGHT", dest="LEFT")
            if not crudo:
                continue
            procesar(json.loads(crudo))
            r.lrem(PROCESSING, 1, crudo)
        except (redis.ConnectionError, redis.TimeoutError) as exc:
            log.warning("Redis no disponible: %s", exc)
            time.sleep(2)
        except Exception:
            # la tarea queda en "mr:processing"; al expirar el lease el coordinador la reintenta
            log.exception("Error procesando una partición")
            time.sleep(1)


if __name__ == "__main__":
    main()
