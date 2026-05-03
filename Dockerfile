FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    "fastapi>=0.111.0" \
    "uvicorn[standard]>=0.30.0" \
    "httpx>=0.27.0" \
    "apscheduler>=3.10,<4"

COPY app/ ./app/
COPY run.py ./

ENV MOEX_PROXY_HOST=0.0.0.0 \
    MOEX_PROXY_PORT=9011 \
    MOEX_PROXY_CORS="" \
    MOEX_PROXY_LOG_FILE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 9011

CMD ["python", "run.py", "--no-tray"]
