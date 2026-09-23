FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd --create-home --uid 1000 jobradar \
    && mkdir -p /data \
    && chown -R jobradar:jobradar /app /data
USER jobradar

ENV DB_PATH=/data/jobradar.db

EXPOSE 8000

# Overridden per service in docker-compose.yml. Threaded workers because the
# handlers are SQLite/IO-bound and WAL handles the concurrency.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "4", \
     "--timeout", "120", "--access-logfile", "-", "app:app"]
