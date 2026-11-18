# GestPro Cloud

Migración de **GestPro v2.0** (aplicación de escritorio monolítica: Python + CustomTkinter + SQLite con 15 tablas)
a **6 microservicios en FastAPI orquestados con Docker Swarm**.

Proyecto final — Redes e Infraestructura (Cloud Computing), UAO · Grupo 5
José Emanuel Morcillo · José David Buitrago

## Arquitectura

| Componente | Tecnología | Réplicas | Responsabilidad |
|---|---|---|---|
| `gateway` | HAProxy 3.4 | 2 | Única entrada (`:8080`): ACLs por ruta, round-robin, health checks, **validación del JWT** |
| `frontend` | nginx + HTML/JS | 2 | Interfaz web (POS, inventario, clientes, caja, facturas, reportes) |
| `auth-users` | FastAPI + PostgreSQL `auth_db` | 2 | Login con JWT (HS256), usuarios y roles, configuración del negocio |
| `catalog-inventory` | FastAPI + PostgreSQL `inventory_db` | 2 | Catálogo, sedes, stock por sede, importación de Excel, alertas de stock bajo |
| `pos-sales` | FastAPI + PostgreSQL `sales_db` | 3 | Registro de ventas (comando), modelo de lectura CQRS, export para MapReduce |
| `cash-expenses` | FastAPI + PostgreSQL `cash_db` | 2 | Apertura/cierre de caja con cuadre, gastos |
| `clients-credits` | FastAPI + PostgreSQL `clients_db` | 2 | Clientes, deuda por ventas a crédito, abonos |
| `billing-factus` | FastAPI + PostgreSQL `billing_db` | 2 | Factura electrónica DIAN vía API de **Factus** (sandbox real) |
| `redis` | Redis 7.4 Streams | 1 | Bus de eventos asíncrono `gestpro:events` (y cola de tareas MapReduce) |
| `mr-coordinator` | FastAPI | 1 | MapReduce: extract, split por bytes, reintentos, shuffle + reduce, capa gold |
| `mr-worker` | Python | 3 | MapReduce: map + combiner en paralelo, capa silver |
| `prometheus` / `grafana` | Prometheus 3.15 / Grafana 13.2 | 1 / 1 | Métricas por réplica y dashboard de negocio + infraestructura |

Las 15 tablas del monolito quedaron repartidas en 6 bases de datos (*Database per Service*).

### Eventos del bus (Redis Streams)

| Evento | Lo publica | Lo consumen (cada uno con su *consumer group*) |
|---|---|---|
| `sale.created` | pos-sales | catalog-inventory (descuenta stock), clients-credits (deuda si es a crédito), cash-expenses (movimiento de caja), billing-factus (factura DIAN), pos-sales (resumen CQRS) |
| `stock.low` | catalog-inventory | — (alerta; métrica en Grafana) |
| `client.payment` | clients-credits | cash-expenses (el abono entra a la caja) |
| `invoice.issued` | billing-factus | — |

- Las réplicas de un servicio comparten el grupo: **cada evento se procesa una sola vez por servicio**.
- Un evento solo se confirma (`XACK`) si se procesó bien. Si una réplica muere a mitad, otra lo reclama (`XAUTOCLAIM`);
  tras 5 intentos fallidos pasa a `gestpro:events:dead`.
- Si un servicio está caído, sus eventos esperan en el stream y se procesan cuando vuelve (**consistencia eventual**).

### Flujo de una venta

1. `POST /api/sales` → HAProxy valida el JWT y balancea entre las 3 réplicas de `pos-sales`.
2. `pos-sales` consulta **síncronamente** a `catalog-inventory` el precio y el stock (no vende lo que no hay).
3. Guarda la venta en `sales_db` y publica `sale.created`.
4. **Asíncronamente**: se descuenta el stock, se registra en la caja, se suma la deuda (si es a crédito) y se emite
   la factura en Factus (si se pidió). La interfaz muestra esta línea de tiempo en vivo.

### Procesamiento paralelo (MapReduce)

Evolución del *Lab 2 - MapReduce* y del repo `map_reduce` del curso, sobre el histórico de ventas:

| Capa | Contenido |
|---|---|
| **Bronze** | `ventas_reales.csv` (extraído de `GET /api/sales/export`) + 2 M líneas sintéticas en 8 CSV (~165 MB, ~1 % de filas sucias a propósito) |
| **Silver** | Filas validadas por `mapper.py`, una carpeta por job (`silver/job_<id>/part-NNNN.csv`) |
| **Gold** | `consolidado_sede_categoria.csv/.json`: líneas, ingresos, ticket promedio y desviación estándar por sede y categoría |

- El coordinador divide cada archivo en particiones de 8 MB por **rango de bytes** (como los input splits de Hadoop): 25 particiones.
- Los workers toman particiones con `BLMOVE` (cola confiable) y renuevan un *lease*; si un worker muere, su partición vuelve a la cola.
- Cada worker envía un resumen parcial por clave `(n, suma, suma²)` (combiner) y el reducer calcula promedio y desviación exactos.
- Medido: 2 M líneas en ~13 s con 1 worker y ~5–7 s con 3 (no es 3× porque comparten el disco).
- `mapper.py` y `reducer.py` también funcionan como en el Lab 2:
  `docker exec <worker> sh -c "head -100000 /data/bronze/ventas_sinteticas_01.csv | python mapper.py | sort | python reducer.py"`

## Cómo ejecutarlo

Requisitos: Docker Desktop (Windows, PowerShell) y Python 3 (los scripts de apoyo solo usan la biblioteca estándar).
Los scripts activan Swarm si hace falta.

**Desde cero, recién clonado** (sin la base del monolito): usuario inicial `admin / admin123` y facturación en modo simulado.

```powershell
.\scripts\reset_demo.ps1 -AdminPassword admin123
```

**Migrando el monolito** (con `GestPro.db` de GestPro v2.0): las credenciales de Factus se extraen de esa base como
Docker secret y la facturación va contra el sandbox real de Factus.

```powershell
.\scripts\reset_demo.ps1 -AdminPassword <clave del admin del monolito> -Database <ruta>\GestPro.db
```

Otros scripts:

```powershell
.\scripts\deploy.ps1                       # construye imágenes y despliega/actualiza el stack "gestpro"
.\scripts\redeploy.ps1 pos-sales frontend  # actualiza solo algunos servicios
.\scripts\revivir.ps1                      # fuerza el reinicio de servicios con réplicas incompletas
.\scripts\migrate.ps1 -Db <ruta>\GestPro.db   # solo la migración SQLite -> PostgreSQL
python scripts\cargar_demo.py <clave admin>   # sedes Norte/Sur, catálogo por Excel en cada sede, clientes
python scripts\prueba_humo.py <clave admin>   # prueba de punta a punta (24 verificaciones)
python scripts\prueba_mapreduce.py <clave admin>   # lanza un job MapReduce y muestra el resumen
.\scripts\demo_alta_disponibilidad.ps1        # demo guiada: autorecuperación, escalado, eventos pendientes, rollback, worker caído
```

El diagrama de arquitectura está en `docs/arquitectura_gestpro.drawio` (abrir en app.diagrams.net; hecho sobre la plantilla del curso).

| URL | Qué es |
|---|---|
| http://localhost:8080 | Interfaz web + API (gateway) |
| http://localhost:8404/stats | Estado de cada réplica en HAProxy |
| http://localhost:3000 | Grafana (dashboard "GestPro Cloud — Operación y Negocio") |
| http://localhost:9090/targets | Prometheus: réplicas descubiertas por DNS de Swarm |

Usuarios: `admin` (contraseña del monolito, se re-encripta con PBKDF2 en el primer login) y `cajero1 / cajero123` (vendedor).

**Sin internet o sin credenciales de Factus**: `docker service update --env-add FACTUS_MODE=simulado gestpro_billing-factus`
(o desplegar con `$env:FACTUS_MODE="simulado"`). Las facturas quedan marcadas como `simulada`.

## Secretos (Docker secrets)

Se generan en `secrets/` (excluido de git) con `deploy.ps1`: contraseña de las bases de datos, secreto del JWT
(lo comparten auth-users y HAProxy), contraseña de Grafana y credenciales de Factus (extraídas de la tabla `config`
del monolito, donde estaban en texto plano). Las credenciales de Factus solo las monta `billing-factus`.

## Estructura

```
gestpro-cloud/
├── docker-compose.yml        # stack de Swarm (docker stack deploy)
├── gateway/                  # HAProxy: haproxy.cfg + Dockerfile
├── services/<servicio>/      # Dockerfile, requirements.txt y app/
│   └── app/  main.py · models.py · database.py · events.py · observability.py
├── frontend/                 # index.html · app.js · styles.css (nginx)
├── mapreduce/                # generate_sales · mapper · reducer · worker · coordinator
├── monitoring/               # prometheus.yml · Grafana (provisioning + dashboard)
├── scripts/                  # deploy, redeploy, reset, migración, carga demo, pruebas, demo de HA
├── docs/                     # diagrama de arquitectura (.drawio)
└── datos_demo/               # catalogo_demo.xlsx (40 productos)
```

Cada servicio tiene su propia copia de `events.py`, `database.py` y `observability.py`: no hay librería compartida,
para no acoplar los servicios entre sí.

## Limitaciones conocidas (y qué haríamos en producción)

- Swarm de un solo nodo en la demo; en producción: 3 managers y un registry de imágenes.
- PostgreSQL y Redis con 1 réplica (stateful); en producción: Patroni / Redis Sentinel.
- Bronze/silver/gold en un volumen local compartido; en varios nodos: HDFS u objeto (S3/MinIO).
- Sin TLS en el gateway (demo local).
