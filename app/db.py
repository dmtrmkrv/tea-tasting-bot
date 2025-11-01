import os
import urllib.parse
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base


def _build_db_url_from_parts():
    host = os.getenv("POSTGRESQL_HOST")
    port = os.getenv("POSTGRESQL_PORT", "5432")
    user = os.getenv("POSTGRESQL_USER")
    password = os.getenv("POSTGRESQL_PASSWORD")
    dbname = os.getenv("POSTGRESQL_DBNAME")
    sslmode = os.getenv("POSTGRESQL_SSLMODE")  # 'require' | 'disable' | None
    query = f"?sslmode={sslmode}" if sslmode else ""
    if all([host, user, password, dbname]):
        pw = urllib.parse.quote_plus(password)
        return f"postgresql+psycopg://{user}:{pw}@{host}:{port}/{dbname}{query}"
    return None


DATABASE_URL = os.getenv("DATABASE_URL") or _build_db_url_from_parts()
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set and POSTGRESQL_* parts are missing")

engine = create_engine(DATABASE_URL, pool_size=5, max_overflow=10, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()
