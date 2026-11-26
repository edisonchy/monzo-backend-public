FROM python:3.13-slim-bookworm@sha256:e32be0e55814a08977501ca863271d9bd02443710db8f2afaf8372e2c4508530 AS python-base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
# Bootstrap pip from the hash published in the Python security announcement.
RUN python -m pip install --no-cache-dir --no-deps \
    "pip @ https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl#sha256=fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70"

FROM python-base AS dependencies
COPY requirements-runtime.lock ./
RUN python -m pip install --only-binary=:all: -r requirements-runtime.lock
COPY scripts/check_tls_trust.py ./scripts/check_tls_trust.py
RUN python scripts/check_tls_trust.py

FROM python-base AS build
COPY requirements-build.lock pyproject.toml ./
RUN python -m pip install --require-hashes --only-binary=:all: -r requirements-build.lock
COPY src ./src
RUN --network=none python -m pip wheel --no-index --no-build-isolation --check-build-dependencies --no-deps --wheel-dir=/wheels .

FROM dependencies AS base
COPY src ./src
COPY pyproject.toml ./
COPY migrations ./migrations
COPY alembic.ini ./
RUN --network=none --mount=type=bind,from=build,source=/wheels,target=/wheels \
    python -m pip install --no-index --no-deps /wheels/monzo_backend-*.whl && useradd --uid 10001 --create-home monzo

FROM base AS test
COPY requirements.lock ./
RUN python -m pip install --only-binary=:all: -r requirements.lock
COPY tests ./tests
COPY deploy/keycloak/finance-realm.json ./deploy/keycloak/finance-realm.json
COPY scripts ./scripts
USER monzo
CMD ["pytest", "-q", "--tb=short", "-p", "no:cacheprovider"]

FROM base AS runtime
USER monzo
EXPOSE 8080
CMD ["uvicorn", "monzo_backend.api:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--no-proxy-headers", "--timeout-graceful-shutdown", "30"]
