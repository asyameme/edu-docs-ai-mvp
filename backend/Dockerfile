FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PYTHONPATH=/workspace:/workspace/backend
WORKDIR /workspace/backend
COPY requirements.txt ./
RUN pip install -r requirements.txt
RUN useradd --create-home --uid 10001 appuser
RUN mkdir -p /data/originals /data/artifacts && chown appuser:appuser /workspace /data/originals /data/artifacts
COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser contracts ./contracts
COPY --chown=appuser:appuser worker/parser ./worker/parser
COPY --chown=appuser:appuser scripts ./scripts
COPY --chown=appuser:appuser examples ./examples
COPY --chown=appuser:appuser pyproject.toml alembic.ini ./
COPY --chown=appuser:appuser migrations ./migrations
USER appuser
CMD ["fastapi", "run", "--host", "0.0.0.0", "--port", "8000"]

FROM runtime AS test
USER root
COPY requirements-dev.txt ./
RUN pip install -r requirements-dev.txt
COPY --chown=appuser:appuser tests ./tests
USER appuser

FROM runtime AS production
