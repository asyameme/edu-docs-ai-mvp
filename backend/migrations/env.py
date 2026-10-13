from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.engine import URL

from app.config import get_settings

settings = get_settings()
if settings.postgres_db != "education":
    raise RuntimeError("Application migrations may only target education")
url = URL.create(
    "postgresql+psycopg",
    username=settings.postgres_user,
    password=settings.postgres_password.get_secret_value(),
    host=settings.postgres_host,
    database=settings.postgres_db,
)
engine = create_engine(url)
with engine.connect() as connection:
    # Фиксируем блокировку сессии, чтобы транзакцией изменения схемы управлял Alembic.
    connection.exec_driver_sql("SELECT pg_advisory_lock(79002312)")
    connection.commit()
    try:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()
    finally:
        connection.exec_driver_sql("SELECT pg_advisory_unlock(79002312)")
        connection.commit()
engine.dispose()
