import os
from logging.config import fileConfig
from sqlalchemy import engine_from_config, pool
from alembic import context
from app.db import Base
from app import models  # noqa

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata

def get_url():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set for Alembic")
    return url

def run_migrations_offline():
    context.configure(url=get_url(), target_metadata=target_metadata, literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online():
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = get_url()
    connectable = engine_from_config(configuration, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
