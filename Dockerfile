# Application image: FastAPI API + web interface
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    RRC_DB_PATH=/data/components.db

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY data/seed_components.csv data/ccl_headings.json data/rohs_exemptions.json ./data/
COPY examples ./examples
COPY cli.py docker-entrypoint.sh ./

# Unprivileged user; the SQLite database lives in a volume (/data)
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data && chown appuser /data \
    && chmod +x docker-entrypoint.sh
USER appuser
VOLUME ["/data"]

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/stats', timeout=4)"

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
