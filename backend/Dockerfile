FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /workspace
COPY requirements*.txt ./
RUN pip install -r requirements.txt -r requirements-dev.txt
RUN useradd --create-home --uid 10001 appuser
RUN mkdir -p /data/originals && chown appuser:appuser /workspace /data/originals
COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser scripts ./scripts
COPY --chown=appuser:appuser tests ./tests
COPY --chown=appuser:appuser pyproject.toml alembic.ini ./
COPY --chown=appuser:appuser migrations ./migrations
USER appuser
CMD ["fastapi", "run", "--host", "0.0.0.0", "--port", "8000"]
