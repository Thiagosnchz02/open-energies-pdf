# Usa la imagen oficial de Playwright que ya incluye Python, Chromium y todas las dependencias del sistema.
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

# Establece el directorio de trabajo dentro del contenedor.
WORKDIR /app

# Playwright viene preinstalado; solo necesitamos las dependencias de nuestra aplicación.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copia el código de tu aplicación.
COPY server.py ./server.py
COPY templates ./templates

# Cloud Run inyectará la variable de entorno PORT. Uvicorn debe usarla.
# El valor por defecto 8080 se usará si ejecutas el contenedor localmente.
ENV PYTHONUNBUFFERED=1 PORT=8080
EXPOSE 8080
CMD ["bash","-lc","exec uvicorn server:app --host 0.0.0.0 --port ${PORT:-8080}"]