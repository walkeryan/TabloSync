FROM python:3.12-slim AS base

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

FROM base AS test

COPY tests ./tests
RUN pip install --no-cache-dir -e '.[dev]' \
    && ruff check . \
    && ruff format --check . \
    && pytest -q

FROM base AS final

RUN pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1 \
    TABLOSYNC_PORT=5004

EXPOSE 5004

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('TABLOSYNC_PORT', '5004') + '/healthz', timeout=3)"

CMD ["tablosync"]
