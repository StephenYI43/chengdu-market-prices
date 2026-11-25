FROM ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d AS uv
FROM apache/spark:4.3.0-java21-python3@sha256:3dedc5c8665a6f472e2f0a3a828207a79a30785090ef325d3af219cb90a73d63 AS app
USER root
COPY --from=uv /uv /uvx /usr/local/bin/
ENV UV_PYTHON_INSTALL_DIR=/opt/managed-python UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv python install 3.13.18 && uv sync --frozen --no-dev
COPY src ./src
COPY scripts ./scripts
COPY data/samples ./data/samples
COPY contracts ./contracts
COPY config ./config
COPY frontend/dist ./static
ENV PATH="/app/.venv/bin:${PATH}" PYTHONPATH=/app/src
ENV MARKET_WEB_DIR=/app/static
ENV PYSPARK_PYTHON=/app/.venv/bin/python PYSPARK_DRIVER_PYTHON=/app/.venv/bin/python
RUN mkdir -p /app/runtime /app/artifacts /opt/spark/ivy

FROM app AS ml
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --group ml
ENV MLFLOW_ENABLE_TELEMETRY=false MPLCONFIGDIR=/tmp/matplotlib GIT_PYTHON_REFRESH=quiet
