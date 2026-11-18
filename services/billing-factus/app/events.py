"""Bus de eventos de GestPro sobre Redis Streams.

publish(tipo, datos)
    Agrega un evento JSON al stream "gestpro:events".

start_consumer(grupo, {tipo: funcion})
    Lanza un hilo que lee el stream con un *consumer group*:
    - Cada servicio usa su PROPIO grupo  -> todos los servicios reciben cada evento.
    - Las réplicas de un servicio comparten el grupo -> cada evento se procesa
      UNA sola vez por servicio (se reparten el trabajo).
    - Un evento solo se confirma (XACK) cuando se procesó bien. Si la réplica
      muere a mitad, el evento queda pendiente y otra réplica lo reclama con
      XAUTOCLAIM. Esto corrige la pérdida de tareas que tenía BRPOP en el lab.
    - Tras MAX_ENTREGAS intentos fallidos el evento va a una cola de "dead letter".
"""
import json
import logging
import os
import socket
import threading
import time

import redis
from prometheus_client import Counter

STREAM = "gestpro:events"
DEAD_LETTER = "gestpro:events:dead"
MAX_ENTREGAS = 5
RECLAMAR_TRAS_MS = 30_000   # un pendiente sin ACK por 30 s se considera abandonado

log = logging.getLogger("events")
r = redis.Redis(host=os.getenv("REDIS_HOST", "redis"), port=6379, decode_responses=True,
                socket_timeout=10, socket_connect_timeout=5)

EVENTS_PUBLISHED = Counter("gestpro_events_published_total", "Eventos publicados en el bus", ["type"])
EVENTS_CONSUMED = Counter("gestpro_events_consumed_total", "Eventos consumidos del bus",
                          ["group", "type", "status"])


def publish(event_type: str, data: dict) -> str:
    msg_id = r.xadd(STREAM, {"type": event_type, "data": json.dumps(data, default=str)},
                    maxlen=100_000, approximate=True)
    EVENTS_PUBLISHED.labels(event_type).inc()
    log.info("Evento publicado %s (%s)", event_type, msg_id)
    return msg_id


def start_consumer(group: str, handlers: dict):
    hilo = threading.Thread(target=_loop, args=(group, handlers), daemon=True, name=f"consumer-{group}")
    hilo.start()


def _ensure_group(group: str):
    try:
        # "$" = el grupo empieza a leer desde los eventos nuevos
        r.xgroup_create(STREAM, group, id="$", mkstream=True)
        log.info("Grupo de consumo '%s' creado", group)
    except redis.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):   # BUSYGROUP = el grupo ya existe
            raise


def _loop(group: str, handlers: dict):
    consumer = socket.gethostname()   # cada réplica es un consumidor distinto del grupo
    grupo_listo = False
    while True:
        try:
            if not grupo_listo:
                _ensure_group(group)
                grupo_listo = True
            # 1) Reclamar eventos que otra réplica dejó a medias (o que fallaron antes)
            res = r.xautoclaim(STREAM, group, consumer, min_idle_time=RECLAMAR_TRAS_MS,
                               start_id="0-0", count=20)
            for msg_id, fields in res[1]:
                _process(group, handlers, msg_id, fields, reclamado=True)
            # 2) Leer eventos nuevos (espera hasta 5 s)
            resp = r.xreadgroup(group, consumer, {STREAM: ">"}, count=20, block=5000)
            for _stream, mensajes in resp or []:
                for msg_id, fields in mensajes:
                    _process(group, handlers, msg_id, fields)
        except (redis.ConnectionError, redis.TimeoutError) as exc:
            log.warning("Redis no disponible, reintentando: %s", exc)
            grupo_listo = False
            time.sleep(2)
        except Exception:
            log.exception("Error en el consumidor '%s'", group)
            time.sleep(2)


def _process(group: str, handlers: dict, msg_id: str, fields: dict, reclamado: bool = False):
    if not fields:                      # el evento ya no existe en el stream
        r.xack(STREAM, group, msg_id)
        return
    tipo = fields.get("type", "")
    handler = handlers.get(tipo)
    if handler is None:                 # evento que no le interesa a este servicio
        r.xack(STREAM, group, msg_id)
        return

    if reclamado:
        info = r.xpending_range(STREAM, group, min=msg_id, max=msg_id, count=1)
        if info and info[0]["times_delivered"] > MAX_ENTREGAS:
            r.xadd(DEAD_LETTER, {"group": group, "id": msg_id, **fields})
            r.xack(STREAM, group, msg_id)
            EVENTS_CONSUMED.labels(group, tipo, "dead_letter").inc()
            log.error("Evento %s enviado a dead-letter tras %s intentos", msg_id, MAX_ENTREGAS)
            return

    # Idempotencia básica: no procesar dos veces el mismo evento en el mismo grupo
    procesados = f"gestpro:processed:{group}"
    if r.sismember(procesados, msg_id):
        r.xack(STREAM, group, msg_id)
        return

    try:
        handler(json.loads(fields["data"]))
    except Exception:
        EVENTS_CONSUMED.labels(group, tipo, "error").inc()
        log.exception("Falló %s (%s); queda pendiente y se reintentará", tipo, msg_id)
        return                          # sin ACK -> se reintenta

    r.sadd(procesados, msg_id)
    r.xack(STREAM, group, msg_id)
    EVENTS_CONSUMED.labels(group, tipo, "ok").inc()
    log.info("Evento %s procesado por '%s'%s", tipo, group, " (reclamado)" if reclamado else "")
