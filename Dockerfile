FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt

COPY . .

RUN mkdir -p /app/data /app/seed \
    && if [ -f /app/data/tracker.db ]; then \
        cp /app/data/tracker.db /app/seed/tracker.db; \
        rm -f /app/data/tracker.db; \
    fi \
    && sed -i 's/\r$//' /app/docker-entrypoint.sh \
    && chmod +x /app/docker-entrypoint.sh

VOLUME ["/app/data"]

EXPOSE 8000

ENV PYTHONUNBUFFERED=1

HEALTHCHECK --interval=300s --timeout=3s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=2)" || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
