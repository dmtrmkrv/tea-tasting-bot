import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
DATABASE_URL = os.getenv("DATABASE_URL") or ""
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")
engine = create_engine(DATABASE_URL, pool_size=5, max_overflow=10, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()
