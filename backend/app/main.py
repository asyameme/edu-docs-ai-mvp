from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import psycopg
from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg_pool import ConnectionPool, PoolTimeout
from temporalio.client import Client

from app.config import get_settings
from app.errors import DomainError
from app.health import Resources, check_dependencies
from app.import_api import router as import_router
from app.models import LiveResponse, ReadyResponse
from app.program_api import router as program_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    pool = ConnectionPool(
        kwargs={
            "host": settings.postgres_host,
            "dbname": settings.postgres_db,
            "user": settings.postgres_user,
            "password": settings.postgres_password.get_secret_value(),
            "connect_timeout": 2,
        },
        min_size=0,
        max_size=5,
        timeout=2,
        open=False,
        check=ConnectionPool.check_connection,
    )
    temporal = await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        lazy=True,
    )
    http = httpx.AsyncClient(timeout=2, trust_env=False)
    pool.open()
    app.state.resources = Resources(pool, temporal, http, settings.chroma_url.rstrip("/"))
    try:
        yield
    finally:
        await http.aclose()
        pool.close()


app = FastAPI(
    title="Educational Documents",
    version="0.4.0",
    description="Хранение программ и версий, пакетный импорт с подключаемым внешним парсером.",
    lifespan=lifespan,
)
health = APIRouter(prefix="/health", tags=["Health"])


@health.get("/live")
def live() -> LiveResponse:
    return LiveResponse()


@health.get("/ready", responses={503: {"model": ReadyResponse}})
async def ready(request: Request, response: Response) -> ReadyResponse:
    result = await check_dependencies(request.app.state.resources)
    if result.status != "ready":
        response.status_code = 503
    return result


app.include_router(health)
app.include_router(program_router)
app.include_router(import_router)


@app.exception_handler(DomainError)
async def domain_error_handler(request: Request, error: DomainError):
    return JSONResponse(status_code=error.status, content={"detail": error.detail})


async def storage_error_handler(request: Request, error: Exception):
    return JSONResponse(status_code=503, content={"detail": "Storage temporarily unavailable"})


for error_type in (psycopg.Error, PoolTimeout, OSError):
    app.add_exception_handler(error_type, storage_error_handler)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, error: RequestValidationError):
    # Не возвращаем входные данные: бесконечности и NaN нельзя сериализовать в JSON,
    # а содержимое запроса не нужно для объяснения ошибок проверки полей.
    details = [
        {"loc": item["loc"], "msg": item["msg"], "type": item["type"]} for item in error.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": details})
