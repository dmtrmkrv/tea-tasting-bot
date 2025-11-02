"""Database engine configuration and session helpers."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.engine import make_url

from app.config import get_db_url, mask_db_url

logger = logging.getLogger(__name__)

_DB_URL = get_db_url()


def create_sa_engine(db_url: str) -> Engine:
    url = make_url(db_url)
    connect_args = {}
    if url.drivername.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    engine = create_engine(
        db_url,
        pool_pre_ping=True,
        future=True,
        connect_args=connect_args,
    )
    return engine


logger.info("[DB] Using: %s", mask_db_url(_DB_URL))
engine: Engine = create_sa_engine(_DB_URL)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)


try:
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    logger.info("[DB] OK")
except SQLAlchemyError as exc:
    logger.exception("[DB] FAIL: %s", exc)
    raise


@contextmanager
def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
