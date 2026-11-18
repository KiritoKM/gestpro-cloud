"""Métricas para Prometheus + cabecera X-Replica (qué réplica atendió la petición).

instrument(app, on_scrape=None)
    - mide la latencia de cada petición (histograma por servicio, ruta y código)
    - expone GET /metrics
    - on_scrape: función opcional que se ejecuta justo antes de cada scrape para
      actualizar métricas de negocio calculadas desde la base de datos.
"""
import logging
import os
import socket
import time

from fastapi import Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Histogram, generate_latest

SERVICE = os.getenv("SERVICE_NAME", "servicio")
REPLICA = socket.gethostname()
log = logging.getLogger("observability")

REQUEST_SECONDS = Histogram(
    "gestpro_http_request_seconds", "Latencia de las peticiones HTTP",
    ["service", "method", "route", "status"],
)


def instrument(app, on_scrape=None):
    @app.middleware("http")
    async def medir(request: Request, call_next):
        inicio = time.perf_counter()
        response = await call_next(request)
        route = request.scope.get("route")
        ruta = route.path if route else "otra"
        if ruta not in ("/metrics", "/health"):
            REQUEST_SECONDS.labels(SERVICE, request.method, ruta, response.status_code).observe(
                time.perf_counter() - inicio)
        response.headers["X-Replica"] = f"{SERVICE}@{REPLICA}"
        return response

    @app.get("/metrics", include_in_schema=False)
    def metrics():
        if on_scrape:
            try:
                on_scrape()
            except Exception as exc:     # si la BD no responde, igual se exponen las demás métricas
                log.warning("No se pudieron actualizar las métricas de negocio: %s", exc)
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
