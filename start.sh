#!/usr/bin/env bash
set -euo pipefail

echo "[start] Waiting for Postgres ${POSTGRESQL_HOST}:${POSTGRESQL_PORT:-5432} ..."
TRY=0
until python - <<'PY'
import os
from sqlalchemy import create_engine, text
user = os.environ["POSTGRESQL_USER"]
pwd  = os.environ["POSTGRESQL_PASSWORD"]
host = os.environ["POSTGRESQL_HOST"]
port = os.environ.get("POSTGRESQL_PORT","5432")
db   = os.environ["POSTGRESQL_DBNAME"]
ssl  = os.getenv("POSTGRESQL_SSLMODE")
query = f"?sslmode={ssl}" if ssl else ""
url  = f"postgresql+psycopg://{user}:{pwd}@{host}:{port}/{db}{query}"
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
