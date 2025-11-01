import os, sys
from urllib.parse import quote_plus
from alembic import context
from sqlalchemy import engine_from_config, pool

# Добавляем корень проекта в sys.path
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# Импортируем модели после фикса пути
from app.db import Base
from app import models  # noqa

# Не вызываем fileConfig(config.config_file_name) — в нашем alembic.ini нет секций логгера
config = context.config
target_metadata = Base.metadata

def build_db_url():
    # 1) берём готовую строку, если задана
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    # 2) иначе склеиваем из POSTGRESQL_* переменных
    host = os.getenv("POSTGRESQL_HOST")
    port = os.getenv("POSTGRESQL_PORT", "5432")
    user = os.getenv("POSTGRESQL_USER")
    password = os.getenv("POSTGRESQL_PASSWORD")
    dbname = os.getenv("POSTGRESQL_DBNAME")
    if all([host, user, password, dbname]):
        return f"postgresql+psycopg://{user}:{quote_plus(password)}@{host}:{port}/{dbname}"
    raise RuntimeError("Set DATABASE_URL or POSTGRESQL_HOST/PORT/USER/PASSWORD/DBNAME")

def run_migrations_offline():
    context.configure(
        url=build_db_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online():
    cfg = config.get_section(config.config_ini_section) or {}
    cfg["sqlalchemy.url"] = build_db_url()
    connectable = engine_from_config(cfg, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
