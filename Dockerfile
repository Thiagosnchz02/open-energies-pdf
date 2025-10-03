# Dockerfile
FROM python:3.11-slim

# Dependencias del sistema para Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
    libpango-1.0-0 fonts-liberation wget ca-certificates \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Instalar Chromium para Playwright
RUN python -m playwright install --with-deps chromium

COPY server.py ./server.py
COPY templates ./templates

EXPOSE 8000

# Arranque del servicio
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
