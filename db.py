from sqlalchemy import create_engine
from sqlalchemy.engine import URL, make_url


def build_db_url_from_env(os, sqlite_default="/app/tastings.db"):
    raw = os.getenv("DATABASE_URL")
    if raw:
        return raw

    pg_host = os.getenv("POSTGRESQL_HOST")
    pg_db = os.getenv("POSTGRESQL_DBNAME")
    pg_user = os.getenv("POSTGRESQL_USER")
    pg_password = os.getenv("POSTGRESQL_PASSWORD")

    if all([pg_host, pg_db, pg_user, pg_password]):
        pg_port = os.getenv("POSTGRESQL_PORT") or "5432"
        pg_sslmode = os.getenv("POSTGRESQL_SSLMODE", "disable")
        return URL.create(
            drivername="postgresql+psycopg",  # psycopg3
            username=pg_user,
            password=pg_password,
            host=pg_host,
            port=int(pg_port),
            database=pg_db,
            query={"sslmode": pg_sslmode},
        )

    return URL.create(
        "sqlite",
        database=os.getenv("SQLITE_PATH", sqlite_default),
    )


def create_sa_engine(db_url_str):
    url = make_url(str(db_url_str))
    kw = dict(future=True, pool_pre_ping=True)
    # ВАЖНО: для SQLite — даем флаг; для Postgres — нет
    if url.drivername.startswith("sqlite"):
        kw["connect_args"] = {"check_same_thread": False}
    return create_engine(url, **kw)


def mask_dsn(dsn, password):
    masked = str(dsn)
    if not password:
        return masked
    return masked.replace(password, "***")
