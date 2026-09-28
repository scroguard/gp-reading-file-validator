FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install . && useradd --system --uid 10001 --no-create-home appuser

USER appuser
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')" || exit 1

# Listens on all interfaces *inside* the container. What the host exposes is
# decided by the port mapping in docker-compose.yml (localhost-only by default).
CMD ["uvicorn", "rsvalidator.web:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
