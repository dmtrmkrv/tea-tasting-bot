#!/usr/bin/env bash
set -euo pipefail

echo "[start] Waiting for Postgres ${POSTGRESQL_HOST:-<via DATABASE_URL>}:${POSTGRESQL_PORT:-5432} ..."
TRY=0
until python - <<'PY'
import os
from urllib.parse import quote_plus
from sqlalchemy import create_engine, text

def build_url():
    # 1) приоритет — готовая строка
    url = os.getenv("DATABASE_URL")
    if url:
        return url

    # 2) иначе склеиваем из POSTGRESQL_*
    host = os.getenv("POSTGRESQL_HOST")
    user = os.getenv("POSTGRESQL_USER")
    password = os.getenv("POSTGRESQL_PASSWORD")
    dbname = os.getenv("POSTGRESQL_DBNAME")
    port = os.getenv("POSTGRESQL_PORT", "5432")
    if not all([host, user, password, dbname]):
        raise SystemExit("DB env missing: set DATABASE_URL or POSTGRESQL_HOST/USER/PASSWORD/DBNAME")

    ssl = os.getenv("POSTGRESQL_SSLMODE")  # 'require' | 'disable' | None
    query = f"?sslmode={ssl}" if ssl else ""
    return f"postgresql+psycopg://{user}:{quote_plus(password)}@{host}:{port}/{dbname}{query}"

url = build_url()
try:
    with create_engine(url, future=True, pool_pre_ping=True).connect() as conn:
        conn.execute(text("SELECT 1"))
    print("OK")
    raise SystemExit(0)
except Exception as e:
    print("NOT READY:", e)
    raise SystemExit(1)
PY
do
  TRY=$((TRY+1))
  if [ "$TRY" -ge 30 ]; then
    echo "[start] Postgres is not ready after 30 tries, abort."
    exit 1
  fi
  sleep 2
done

echo "[start] Running migrations…"
alembic upgrade head

echo "[start] Starting bot…"
exec python -u main.py
