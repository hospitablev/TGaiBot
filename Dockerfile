FROM ghcr.io/astral-sh/uv:0.11.5 AS uv
FROM python:3.11-slim-bookworm
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/data HF_HOME=/data/cache/huggingface \
    ENABLE_TRANSCRIPTION=true UV_PROJECT_ENVIRONMENT=/opt/venv PATH="/opt/venv/bin:$PATH"
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra audio --extra calls --no-install-project
COPY tgaibot ./tgaibot
RUN uv sync --frozen --no-dev --extra audio --extra calls
CMD ["python", "-m", "tgaibot", "run"]
