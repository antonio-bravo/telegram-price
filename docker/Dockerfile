FROM python:3.15-slim

# Evitar buffers en Python y prompts interactivos
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive \
    DATA_DIR=/app/data

# Instalar herramientas básicas del sistema y certificados
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Instalar dependencias de Python
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copiar el script monitor, el panel web y el script de arranque
COPY monitor.py .
COPY web_app.py .
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Puerto del panel web
EXPOSE 9999

# Crear directorio por defecto para datos persistentes
RUN mkdir -p /app/data

ENTRYPOINT ["/entrypoint.sh"]
