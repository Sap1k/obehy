# The Oběhy server images (deploy/compose.yaml). Static builds run on GitHub Actions, not from
# these images. Targets:
# - `server` (the default, the last stage): the Python package: migrations, release updates,
#   the nightly and hourly jobs and the API;
# - `worker`: the realtime worker, `obehy-rt worker` (rust/, docs/RUST_PORT.md), with the config
#   and data files it reads at the paths it finds them by.
# Nothing is pulled from Docker Hub: GHCR, and Google's Docker Hub mirror for the official images.

# --- obehy-rt ----------------------------------------------------------------------------------
# glibc (Debian trixie, as the images below), not a musl static build: f64 sin/cos/atan2 come
# from the platform libm (rust/docs/PORTING_HAZARDS.md, "Platform libm"). The toolchain is the
# image's (RUSTUP_TOOLCHAIN overrides rust/rust-toolchain.toml's `stable`, so the build downloads
# no other); it must be at least the workspace's rust-version.
FROM mirror.gcr.io/library/rust:1.100-slim-trixie AS rust-build
WORKDIR /build
COPY rust ./rust
# Compiled in (include_str!): the SQL the worker shares with the Python side.
COPY src/obehy/data/realtime/sql ./src/obehy/data/realtime/sql
WORKDIR /build/rust
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/build/rust/target \
    RUSTUP_TOOLCHAIN="$RUST_VERSION" cargo build --release --locked -p obehy-rt --bin obehy-rt \
    && cp target/release/obehy-rt /usr/local/bin/obehy-rt

FROM mirror.gcr.io/library/debian:trixie-slim AS worker
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*
ENV TZ=Europe/Prague
WORKDIR /app
COPY --from=rust-build /usr/local/bin/obehy-rt /usr/local/bin/obehy-rt
# obehy-rt finds these below the working directory, as in the repository: the connector
# manifests (data/realtime/sources/), the policy and the SR70 catalogue, the vehicle attribute
# vocabulary. (Europe/Prague is compiled in from rust/obehy-core/tzdata/.)
COPY src/obehy/data/realtime ./src/obehy/data/realtime
COPY src/obehy/data/vehicles/attributes.toml ./src/obehy/data/vehicles/
# data/ is the volume the worker writes: the raw archive and the GTFS-RT output (nginx serves
# data/gtfs-rt as /srv/gtfs-rt).
RUN useradd --system --uid 10001 --home-dir /app obehy \
    && mkdir -p data/rt-raw data/gtfs-rt \
    && chown -R obehy:obehy data
USER obehy
ENTRYPOINT ["obehy-rt"]
CMD ["worker"]

# --- server ------------------------------------------------------------------------------------
# uv's build of the official python:3.13-slim (Debian trixie), from GHCR.
FROM ghcr.io/astral-sh/uv:0.12.7-python3.13-trixie-slim AS server
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

# data/ holds fetched releases, the raw archive, the GTFS-RT output and the feed list (one volume,
# shared with the worker).
RUN useradd --system --uid 10001 --home-dir /app obehy \
    && mkdir -p data/releases data/rt-raw data/gtfs-rt data/public \
    && chown -R obehy:obehy data
USER obehy
ENTRYPOINT ["obehy"]
CMD ["--help"]
