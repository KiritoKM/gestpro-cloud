"""Lanza un job MapReduce por el gateway, espera a que termine y muestra el resumen.
Uso:  python scripts/prueba_mapreduce.py <contraseña_admin>
Para comparar paralelismo:  docker service scale gestpro_mr-worker=1   (luego =3) y volver a correrlo.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prueba_humo import llamar  # noqa: E402


def main():
    clave = sys.argv[1] if len(sys.argv) > 1 else "admin123"
    _, login, _ = llamar("POST", "/api/auth/login", cuerpo={"username": "admin", "password": clave})
    token = login["access_token"]
    s, job, _ = llamar("POST", "/api/mapreduce/jobs", token, {})
    if s != 202:
        sys.exit(f"No se pudo lanzar el job: {job}")
    print(f"Job {job['id']} lanzado")
    while True:
        time.sleep(0.5)
        _, job, _ = llamar("GET", "/api/mapreduce/jobs/latest", token)
        print(f"\r  fase={job['phase']:<8} particiones {job['partitions_done']}/{job['partitions']} "
              f"reintentos={job['retries']}   ", end="", flush=True)
        if job["status"] != "running":
            break
    print()
    if job["status"] != "done":
        sys.exit(f"Job fallido: {job.get('error')}")
    print(f"Líneas: {job['rows_in']:,} (válidas {job['rows_valid']:,}, descartadas {job['rows_discarded']:,}) · "
          f"ventas reales: {job['real_rows']}")
    print(f"Particiones: {job['partitions']} · workers: {job['workers']} · reintentos: {job['retries']}")
    print(f"Tiempo total {job['duration_s']} s  (map {job['map_s']} s · reduce {job['reduce_s']} s)")
    for w, st in job["worker_stats"].items():
        print(f"  {w}: {st['partitions']} particiones, {st['rows']:,} líneas, {st['seconds']} s de trabajo")
    print("Top 5 del consolidado (gold):")
    for f in sorted(job["results"], key=lambda x: -x["ingresos"])[:5]:
        print(f"  {f['sede']:<10} {f['categoria']:<12} líneas={f['lineas']:>7,} ingresos={f['ingresos']:>15,.0f} "
              f"ticket={f['ticket_promedio']:>9,.0f} desv={f['desviacion']:>9,.0f}")


if __name__ == "__main__":
    main()
