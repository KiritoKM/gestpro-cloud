# Imagen única para el coordinador y los workers (cambia solo el comando).
# Se construye desde la raíz del proyecto para incluir el catálogo de la demo.
FROM python:3.11-slim
WORKDIR /app
COPY mapreduce/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mapreduce/*.py ./
COPY datos_demo/catalogo_demo.xlsx ./catalogo_demo.xlsx
EXPOSE 8000
CMD ["python", "-u", "worker.py"]
