# DashDashGo application image: Python 3.12 + Playwright Chromium, non-root.
#
# python:3.12-slim + `playwright install --with-deps chromium` installs only the
# one browser we use (and exactly the system libraries it needs), keeping the
# image far smaller than the all-browsers mcr.microsoft.com/playwright image.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PATH="/opt/venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:0.7.20 /uv /usr/local/bin/uv

WORKDIR /app

# 1) Third-party dependencies (cached until pyproject/uv.lock change).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev --group test

# 2) Chromium + its OS libraries (cached until the Playwright version changes).
RUN playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R a+rX /ms-playwright

# 3) The application itself.
COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --group test
COPY demo ./demo
COPY tests ./tests
COPY reports ./reports

# Report configs live on a volume at /data/reports so they can be edited from the
# UI/CLI and survive restarts. Docker seeds a new named volume from this directory.
RUN useradd --create-home --uid 10001 dashdashgo \
    && mkdir -p /data/storage \
    && cp -r /app/reports /data/reports \
    && chown -R dashdashgo:dashdashgo /data
USER dashdashgo

ENV REPORTS_DIR=/data/reports \
    STORAGE_ROOT=/data/storage \
    API_PORT=8000 \
    PYTEST_ADDOPTS="-p no:cacheprovider"

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"

CMD ["dashdashgo", "serve"]
