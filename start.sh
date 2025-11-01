#!/usr/bin/env bash
set -euo pipefail

if [[ -n "${POSTGRESQL_HOST:-}" ]]; then
  WAIT_TARGET="${POSTGRESQL_HOST}:${POSTGRESQL_PORT:-5432}"
elif [[ -n "${DATABASE_URL:-}" ]]; then
  WAIT_TARGET="${DATABASE_URL}"
else
  echo "[start] PostgreSQL connection details are not configured." >&2
  exit 2
fi

echo "[start] Waiting for Postgres ${WAIT_TARGET} ..."
TRY=0
until python - <<'PY'
import os
import sys
from sqlalchemy import create_engine, text


def build_url() -> str:
    user = os.getenv("POSTGRESQL_USER")
    pwd = os.getenv("POSTGRESQL_PASSWORD")
    host = os.getenv("POSTGRESQL_HOST")
    db = os.getenv("POSTGRESQL_DBNAME")
    port = os.getenv("POSTGRESQL_PORT", "5432")
    ssl = os.getenv("POSTGRESQL_SSLMODE")
    if all([user, pwd, host, db]):
        query = f"?sslmode={ssl}" if ssl else ""
        return f"postgresql+psycopg://{user}:{pwd}@{host}:{port}/{db}{query}"

    url = os.getenv("DATABASE_URL")
    if url:
        return url

    print("[start] PostgreSQL connection details are not configured.", file=sys.stderr)
    raise SystemExit(2)


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
