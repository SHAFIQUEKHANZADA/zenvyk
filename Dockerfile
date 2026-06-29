FROM python:3.11-slim

# Avoid .pyc and buffer issues; set HF cache to a writable dir.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/app/.cache/huggingface

WORKDIR /app

# System deps kept minimal.
RUN apt-get update && apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# CPU-only torch to keep the image small and avoid OOM on free tiers.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

# Pre-download the HF models at build time so first request isn't slow.
RUN python -c "from app import guardian" || true

EXPOSE 8000

# Railway injects $PORT; default to 8000 locally.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
