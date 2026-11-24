# Multi-arch image (linux/amd64, linux/arm64) — built with docker buildx in CI.
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY fleetctl ./fleetctl
COPY simulator ./simulator
COPY kb ./kb
RUN useradd --uid 10001 app
USER app
EXPOSE 8000 50051
CMD ["python", "-m", "fleetctl"]
