# The Oběhy server image: the realtime worker, release updates and nightly jobs
# (deploy/compose.yaml). Static builds run on GitHub Actions, not from this image.
# uv's image of the official python:3.13-slim-bookworm, from GHCR: CI runners hit Docker
# Hub's anonymous pull limit.
FROM ghcr.io/astral-sh/uv:0.12.7-python3.13-bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_CACHE=1 \
    PATH=/app/.venv/bin:$PATH \
    TZ=Europe/Prague \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev
COPY deploy ./deploy

# data/ holds fetched releases, the raw archive and the GTFS-RT output (one volume).
RUN useradd --system --uid 10001 --home-dir /app obehy \
    && mkdir -p data/releases data/rt-raw data/gtfs-rt \
    && chown -R obehy:obehy data
USER obehy
ENTRYPOINT ["obehy"]
CMD ["--help"]
