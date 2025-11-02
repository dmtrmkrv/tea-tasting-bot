"""Application configuration helpers for environment variables."""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.engine import URL, make_url

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class S3Settings:
    endpoint_url: str
    region: str
    bucket: str
    access_key: str
    secret_key: str


@dataclass(frozen=True)
class LocalStorageSettings:
    base_dir: str


BOT_TOKEN_ENV = "BOT_TOKEN"
DATABASE_URL_ENV = "DATABASE_URL"
POSTGRES_ENVS = {
    "host": "POSTGRESQL_HOST",
    "port": "POSTGRESQL_PORT",
    "dbname": "POSTGRESQL_DBNAME",
    "user": "POSTGRESQL_USER",
    "password": "POSTGRESQL_PASSWORD",
    "sslmode": "POSTGRESQL_SSLMODE",
}
SQLITE_PATH_ENV = "SQLITE_PATH"

S3_REQUIRED_ENVS = {
    "endpoint": "S3_ENDPOINT_URL",
    "region": "S3_REGION",
    "bucket": "S3_BUCKET",
    "access_key": "S3_ACCESS_KEY",
    "secret_key": "S3_SECRET_KEY",
}
PHOTOS_DIR_ENV = "PHOTOS_DIR"
DEFAULT_SQLITE_PATH = "/app/tastings.db"
DEFAULT_PHOTOS_DIR = "/app/photos"


def _env(name: str) -> Optional[str]:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def get_bot_token() -> str:
    token = _env(BOT_TOKEN_ENV)
    if not token:
        logger.error("%s is not set. Bot cannot start.", BOT_TOKEN_ENV)
        sys.exit(1)
    return token


def get_db_url() -> str:
    database_url = _env(DATABASE_URL_ENV)
    if database_url:
        return database_url

    postgres_values = {key: _env(env) for key, env in POSTGRES_ENVS.items()}
    required_postgres_keys = {"host", "dbname", "user"}

    if all(postgres_values[key] for key in required_postgres_keys):
        raw_port = postgres_values["port"] or "5432"
        try:
            port = int(raw_port)
        except ValueError:
            logger.warning("Invalid POSTGRESQL_PORT=%s, falling back to 5432", raw_port)
            port = 5432
        sslmode = postgres_values["sslmode"] or "prefer"
        url = URL.create(
            drivername="postgresql+psycopg",
            username=postgres_values["user"],
            password=postgres_values["password"],
            host=postgres_values["host"],
            port=port,
            database=postgres_values["dbname"],
            query={"sslmode": sslmode},
        )
        return str(url)

    sqlite_path = _env(SQLITE_PATH_ENV) or DEFAULT_SQLITE_PATH
    return f"sqlite:///{sqlite_path}"


def get_sqlite_path() -> str:
    return _env(SQLITE_PATH_ENV) or DEFAULT_SQLITE_PATH


def is_s3_enabled() -> bool:
    return all(_env(env) for env in S3_REQUIRED_ENVS.values())


def get_s3_settings() -> S3Settings:
    if not is_s3_enabled():
        raise RuntimeError("S3 is not fully configured")
    values = {name: _env(env) for name, env in S3_REQUIRED_ENVS.items()}
    return S3Settings(
        endpoint_url=values["endpoint"],
        region=values["region"],
        bucket=values["bucket"],
        access_key=values["access_key"],
        secret_key=values["secret_key"],
    )


def get_local_storage_settings() -> LocalStorageSettings:
    return LocalStorageSettings(base_dir=_env(PHOTOS_DIR_ENV) or DEFAULT_PHOTOS_DIR)


def mask_db_url(db_url: str) -> str:
    try:
        url = make_url(db_url)
    except Exception:
        return db_url

    if not url.password:
        return db_url

    masked = url.set(password="***")
    return str(masked)
