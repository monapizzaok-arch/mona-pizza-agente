FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Forma shell a proposito, para que ${PORT} se expanda en tiempo de ejecucion.
# Railway asigna el puerto por variable de entorno: si lo dejamos fijo en 8000,
# el contenedor arranca pero nunca recibe trafico.
CMD uvicorn agent.main:app --host 0.0.0.0 --port ${PORT:-8000}
