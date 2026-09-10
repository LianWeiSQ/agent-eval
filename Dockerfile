FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates docker-cli docker-compose git xz-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
RUN python -m pip install "harbor==0.22.0" "PyYAML>=6,<7"

COPY agent_eval ./agent_eval
COPY scripts ./scripts
RUN python -m pip install --no-deps .

EXPOSE 8765

CMD ["python", "-m", "agent_eval", "serve", "--host", "0.0.0.0", "--port", "8765", "--data-dir", ".data"]
