FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data

VOLUME ["/app/data"]

EXPOSE 8000

ENV PYTHONUNBUFFERED=1

HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8000/api/stats || exit 1

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
