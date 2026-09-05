FROM python:3.12-slim

# 构建期 pip 源可覆盖（国内网络默认 pypi.org 经常被劫持/限速）：
#   docker buildx build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple .
# 为空则回退官方源；TRUSTED_HOST 仅在自签 MITM 环境下使用。
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_TRUSTED_HOST=

WORKDIR /app

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    INDEX="${PIP_INDEX_URL:-https://pypi.org/simple}" \
    && if [ -n "$PIP_TRUSTED_HOST" ]; then \
         pip install --index-url "$INDEX" --trusted-host "$PIP_TRUSTED_HOST" -r requirements.txt; \
       else \
         pip install --index-url "$INDEX" -r requirements.txt; \
       fi

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
