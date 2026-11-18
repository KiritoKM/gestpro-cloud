"""Coordinador MapReduce (equivalente al "runner" del lab del curso, como servicio FastAPI).

POST /api/mapreduce/jobs          lanza un job (uno a la vez)
GET  /api/mapreduce/jobs/latest   estado y resultados del último job
GET  /api/mapreduce/dataset       estado de los datos sintéticos

Fases de un job:
  1. EXTRACT  ventas reales de pos-sales (GET /api/sales/export) -> bronze/ventas_reales.csv
  2. SPLIT    divide cada archivo bronze en particiones por rango de bytes
  3. MAP      encola las particiones; los workers (réplicas) las procesan en paralelo
              y escriben la capa silver. Si un worker muere, su partición se reintenta.
  4. SHUFFLE  agrupa los resultados parciales por clave (sede|categoria)
  5. REDUCE   consolida: líneas, ingresos, ticket promedio y desviación estándar
  6. GOLD     gold/consolidado_sede_categoria.csv (+ JSON) y resultados en Redis
"""
import csv
import json
import logging
import os
import shutil
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import redis
import requests
from fastapi import FastAPI, HTTPException
from prometheus_client import Counter, Gauge

from generate_sales import generar
from observability import REPLICA, instrument
from reducer import combinar, resultado

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] coordinator: %(message)s")
log = logging.getLogger("coordinator")

DATA = Path(os.getenv("DATA_DIR", "/data"))
BRONZE, SILVER, GOLD = DATA / "bronze", DATA / "silver", DATA / "gold"
POS_SALES_URL = os.getenv("POS_SALES_URL", "http://pos-sales:8000")
FILAS_SINTETICAS = int(os.getenv("MR_SYNTHETIC_ROWS", "2000000"))
ARCHIVOS_SINTETICOS = int(os.getenv("MR_SYNTHETIC_FILES", "8"))
SPLIT_BYTES = int(os.getenv("MR_SPLIT_MB", "8")) * 1024 * 1024
TASKS, PROCESSING = "mr:tasks", "mr:processing"
COLOMBIA = timezone(timedelta(hours=-5))

r = redis.Redis(host=os.getenv("REDIS_HOST", "redis"), decode_responses=True, socket_timeout=15)
lock = threading.Lock()

JOB_SECONDS = Gauge("gestpro_mr_last_job_seconds", "Duración del último job MapReduce (s)", ["phase"])
JOB_ROWS = Gauge("gestpro_mr_last_job_rows", "Líneas del último job MapReduce", ["status"])
JOBS = Counter("gestpro_mr_jobs_total", "Jobs MapReduce ejecutados", ["status"])
RETRIES = Counter("gestpro_mr_partition_retries_total", "Particiones reintentadas (worker caído)")
for _s in ("done", "failed"):
    JOBS.labels(_s)


def guardar(job: dict):
    r.set(f"mr:job:{job['id']}", json.dumps(job))
    r.set("mr:job:latest", job["id"])


def ahora() -> str:
    return datetime.now(COLOMBIA).strftime("%Y-%m-%d %H:%M:%S")


# ── Datos sintéticos (capa bronze) ──────────────────────────
def preparar_dataset():
    if sorted(BRONZE.glob("ventas_sinteticas_*.csv")):
        r.set("mr:dataset", json.dumps({"status": "ready"}))
        return
    r.set("mr:dataset", json.dumps({"status": "generating", "rows": FILAS_SINTETICAS}))
    log.info("Generando %s líneas sintéticas en %s archivos...", FILAS_SINTETICAS, ARCHIVOS_SINTETICOS)
    inicio = time.perf_counter()
    generar(BRONZE, FILAS_SINTETICAS, ARCHIVOS_SINTETICOS, Path(__file__).with_name("catalogo_demo.xlsx"))
    r.set("mr:dataset", json.dumps({"status": "ready"}))
    log.info("Datos sintéticos listos en %.1f s", time.perf_counter() - inicio)


def dataset_status() -> dict:
    info = json.loads(r.get("mr:dataset") or '{"status": "unknown"}')
    archivos = sorted(BRONZE.glob("*.csv"))
    info["files"] = [{"name": a.name, "mb": round(a.stat().st_size / 1e6, 1)} for a in archivos]
    return info


# ── Ejecución de un job ─────────────────────────────────────
def extraer_ventas_reales(job: dict):
    destino = BRONZE / "ventas_reales.csv"
    try:
        with requests.get(f"{POS_SALES_URL}/api/sales/export", stream=True, timeout=30) as resp:
            resp.raise_for_status()
            with open(destino, "wb") as f:
                for bloque in resp.iter_content(64 * 1024):
                    f.write(bloque)
        job["real_rows"] = max(sum(1 for _ in open(destino, encoding="utf-8")) - 1, 0)
    except Exception as exc:       # si pos-sales no responde, se sigue solo con el histórico existente
        job["warnings"].append(f"No se pudieron extraer las ventas reales: {exc}")


def particionar() -> list[dict]:
    tareas = []
    for archivo in sorted(BRONZE.glob("*.csv")):
        tam = archivo.stat().st_size
        for inicio in range(0, tam, SPLIT_BYTES):
            tareas.append({"file": archivo.name, "start": inicio, "end": min(inicio + SPLIT_BYTES, tam)})
    return tareas


def ejecutar(job: dict):
    t0 = time.perf_counter()
    try:
        job["phase"] = "extract"
        guardar(job)
        for anterior in SILVER.glob("job_*"):      # la capa silver solo guarda el último job
            shutil.rmtree(anterior, ignore_errors=True)
        extraer_ventas_reales(job)

        job["phase"] = "map"
        tareas = particionar()
        for i, t in enumerate(tareas):
            t.update(job=job["id"], part=i)
        job["partitions"] = len(tareas)
        job["bronze_mb"] = round(sum(a.stat().st_size for a in BRONZE.glob("*.csv")) / 1e6, 1)
        r.delete(TASKS, PROCESSING, f"mr:{job['id']}:partials")
        r.lpush(TASKS, *[json.dumps(t) for t in tareas])
        guardar(job)
        t_map = time.perf_counter()
        sin_lease = {}                               # tarea -> primera vez vista sin lease
        while True:
            hechas = r.hlen(f"mr:{job['id']}:partials")
            if hechas != job["partitions_done"]:
                job["partitions_done"] = hechas
                guardar(job)
            if hechas >= len(tareas):
                break
            # Tolerancia a fallos: tareas en proceso cuyo worker dejó de renovar el lease
            for crudo in r.lrange(PROCESSING, 0, -1):
                t = json.loads(crudo)
                if r.exists(f"mr:lease:{t['job']}:{t['part']}"):
                    sin_lease.pop(crudo, None)
                elif time.time() - sin_lease.setdefault(crudo, time.time()) > 3:
                    if r.lrem(PROCESSING, 1, crudo):
                        r.rpush(TASKS, crudo)        # vuelve a la cola: otro worker la toma
                        job["retries"] += 1
                        RETRIES.inc()
                        log.warning("Partición %s sin worker vivo: se reintenta", t["part"])
                    sin_lease.pop(crudo, None)
            if time.perf_counter() - t_map > 600:
                raise TimeoutError("Los workers no terminaron en 10 minutos")
            time.sleep(0.3)
        job["map_s"] = round(time.perf_counter() - t_map, 2)

        # SHUFFLE + REDUCE
        job["phase"] = "reduce"
        guardar(job)
        t_red = time.perf_counter()
        parciales = [json.loads(p) for p in r.hvals(f"mr:{job['id']}:partials")]
        por_clave, por_worker = {}, {}
        for p in parciales:
            for clave, resumen in p["groups"].items():
                por_clave.setdefault(clave, []).append(resumen)          # shuffle: agrupar por clave
            w = por_worker.setdefault(p["worker"], {"partitions": 0, "rows": 0, "seconds": 0.0})
            w["partitions"] += 1
            w["rows"] += p["rows_in"]
            w["seconds"] = round(w["seconds"] + p["seconds"], 2)
        resultados = [resultado(clave, *combinar(lista)) for clave, lista in por_clave.items()]
        resultados.sort(key=lambda x: (x["sede"], -x["ingresos"]))
        job["reduce_s"] = round(time.perf_counter() - t_red, 3)

        # GOLD
        GOLD.mkdir(parents=True, exist_ok=True)
        with open(GOLD / "consolidado_sede_categoria.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(resultados[0]))
            w.writeheader()
            w.writerows(resultados)
        (GOLD / "consolidado_sede_categoria.json").write_text(json.dumps(resultados, ensure_ascii=False, indent=2),
                                                              encoding="utf-8")
        job.update(
            status="done", phase="done", results=resultados, finished_at=ahora(),
            rows_in=sum(p["rows_in"] for p in parciales),
            rows_valid=sum(p["rows_valid"] for p in parciales),
            rows_discarded=sum(p["rows_discarded"] for p in parciales),
            workers=len(por_worker), worker_stats=por_worker,
            duration_s=round(time.perf_counter() - t0, 2))
        JOBS.labels("done").inc()
        JOB_SECONDS.labels("total").set(job["duration_s"])
        JOB_SECONDS.labels("map").set(job["map_s"])
        JOB_ROWS.labels("valida").set(job["rows_valid"])
        JOB_ROWS.labels("descartada").set(job["rows_discarded"])
        log.info("Job %s terminado: %s líneas, %s particiones, %s workers, %.2f s (map %.2f s)",
                 job["id"], job["rows_in"], job["partitions"], job["workers"], job["duration_s"], job["map_s"])
    except Exception as exc:
        log.exception("Job %s falló", job["id"])
        job.update(status="failed", error=str(exc), finished_at=ahora(),
                   duration_s=round(time.perf_counter() - t0, 2))
        JOBS.labels("failed").inc()
    finally:
        guardar(job)
        lock.release()


@asynccontextmanager
async def lifespan(app):
    for carpeta in (BRONZE, SILVER, GOLD):
        carpeta.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=preparar_dataset, daemon=True).start()
    yield


app = FastAPI(title="GestPro · mr-coordinator", lifespan=lifespan)
instrument(app)


@app.get("/health")
def health():
    r.ping()
    return {"status": "ok", "replica": REPLICA}


@app.get("/api/mapreduce/dataset")
def dataset():
    return dataset_status()


@app.post("/api/mapreduce/jobs", status_code=202)
def crear_job():
    if dataset_status().get("status") != "ready":
        raise HTTPException(409, "Todavía se están generando los datos sintéticos; intenta en unos segundos")
    if not lock.acquire(blocking=False):
        raise HTTPException(409, "Ya hay un job MapReduce en ejecución")
    job = {"id": uuid.uuid4().hex[:8], "status": "running", "phase": "starting", "started_at": ahora(),
           "partitions": 0, "partitions_done": 0, "retries": 0, "warnings": [], "real_rows": 0, "results": []}
    guardar(job)
    threading.Thread(target=ejecutar, args=(job,), daemon=True).start()
    return job


@app.get("/api/mapreduce/jobs/latest")
def ultimo_job():
    job_id = r.get("mr:job:latest")
    if not job_id:
        raise HTTPException(404, "Aún no se ha ejecutado ningún job")
    return json.loads(r.get(f"mr:job:{job_id}"))
