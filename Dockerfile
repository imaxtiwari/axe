# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

# Build arguments for non-root user
ARG UID=1000
ARG GID=1000

# Install system dependencies for SQLite, ChromaDB, OCR, and security updates.
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
    libsqlite3-0 \
    tesseract-ocr \
    libtesseract-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Upgrade pip/setuptools and install Python dependencies in a separate layer
# to maximise cache reuse during code-only changes.
COPY pyproject.toml README.md ./
ENV PIP_DEFAULT_TIMEOUT=300 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN pip install --upgrade pip setuptools wheel \
    && pip install -e ".[prod,ocr]"

# Copy application code
COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic.ini ./
COPY scripts/ ./scripts/
COPY fly.toml ./

# Make the package importable from the installed editable metadata.
ENV PYTHONPATH=/app/src

# Create non-root user and writable data directory (backed by a Fly volume).
RUN groupadd -g ${GID} axe \
    && useradd -m -u ${UID} -g axe axe \
    && mkdir -p /data && chown -R axe:axe /data

USER axe

EXPOSE 8000

# Default command; overridden by docker-compose for development hot-reload.
CMD ["uvicorn", "axe.main:app", "--host", "0.0.0.0", "--port", "8000"]
