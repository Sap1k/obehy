# The Oběhy server image: the realtime worker, release updates and nightly jobs
# (deploy/compose.yaml). Static builds run on GitHub Actions, not from this image.
# uv's build of the official python:3.13-slim (Debian trixie), from GHCR, so the image build
# does not depend on Docker Hub.
FROM ghcr.io/astral-sh/uv:0.12.7-python3.13-trixie-slim
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

# data/ holds fetched releases, the raw archive, the GTFS-RT output and the feed list (one volume).
RUN useradd --system --uid 10001 --home-dir /app obehy \
    && mkdir -p data/releases data/rt-raw data/gtfs-rt data/public \
    && chown -R obehy:obehy data
USER obehy
ENTRYPOINT ["obehy"]
CMD ["--help"]
