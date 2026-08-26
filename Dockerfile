FROM python:3.11-slim

WORKDIR /app

# Installation des dépendances système (utiles pour la compilation de paquets scientifiques)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Render injecte la variable d'environnement PORT. On l'utilise avec 8000 par défaut.
CMD ["sh", "-c", "uvicorn api:app --host 0.0.0.0 --port ${PORT:-8000}"]