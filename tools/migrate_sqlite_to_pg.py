"""Utility script to migrate tasting data from legacy SQLite to PostgreSQL."""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from typing import Dict

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker, selectinload

from app.db import SessionLocal
from app.models import Photo, Tasting


def _get_env_var(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} environment variable is required")
    return value


def _sqlite_engine(path: str):
    url = f"sqlite:///{path}"
    return create_engine(url, future=True)


@contextmanager
def sqlite_session(path: str):
    engine = _sqlite_engine(path)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def migrate(sqlite_path: str, pg_session: Session) -> Dict[int, int]:
    """Migrate data from SQLite into PostgreSQL.

    Returns a mapping of legacy tasting IDs to newly created PostgreSQL IDs.
    """
    id_map: Dict[int, int] = {}

    with sqlite_session(sqlite_path) as legacy_session:
        tastings = legacy_session.execute(
            select(Tasting).options(selectinload(Tasting.photos)).order_by(Tasting.id)
        ).scalars().all()

        for tasting in tastings:
            new_tasting = Tasting(
                user_id=tasting.user_id,
                title=tasting.title,
                category=tasting.category,
                aromas=tasting.aromas,
                aftertaste=tasting.aftertaste,
                note=tasting.note,
                tz=tasting.tz,
                created_at=tasting.created_at,
            )
            pg_session.add(new_tasting)
            pg_session.flush()

            id_map[tasting.id] = new_tasting.id

            for photo in tasting.photos:
                new_photo = Photo(
                    tasting_id=new_tasting.id,
                    tg_file_id=photo.tg_file_id,
                    s3_key=photo.s3_key,
                    filename=photo.filename,
                    width=photo.width,
                    height=photo.height,
                    size_bytes=photo.size_bytes,
                    created_at=photo.created_at,
                )
                pg_session.add(new_photo)

    pg_session.commit()
    return id_map


def main() -> None:
    sqlite_path = _get_env_var("OLD_SQLITE_PATH")
    pg_session = SessionLocal()

    try:
        id_map = migrate(sqlite_path, pg_session)
    except Exception:
        pg_session.rollback()
        raise
    finally:
        pg_session.close()

    if id_map:
        sys.stdout.write("Migrated tastings:\n")
        for old_id, new_id in id_map.items():
            sys.stdout.write(f"  {old_id} -> {new_id}\n")
    else:
        sys.stdout.write("No tastings found to migrate.\n")


if __name__ == "__main__":
    _ = _get_env_var("DATABASE_URL")
    main()
