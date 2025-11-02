from sqlalchemy import create_engine
from sqlalchemy.engine import URL, make_url


def build_db_url_from_env(os):
    raw = os.getenv("DATABASE_URL")
    if raw:
        return raw
    return URL.create(
        drivername="postgresql+psycopg",  # psycopg3
        username=os.getenv("POSTGRESQL_USER"),
        password=os.getenv("POSTGRESQL_PASSWORD"),
        host=os.getenv("POSTGRESQL_HOST"),
        port=int(os.getenv("POSTGRESQL_PORT", "5432")),
        database=os.getenv("POSTGRESQL_DBNAME"),
        query={"sslmode": os.getenv("POSTGRESQL_SSLMODE", "disable")},
    )


def create_sa_engine(db_url_str):
    url = make_url(str(db_url_str))
    kw = dict(future=True, pool_pre_ping=True)
    # ВАЖНО: для SQLite — даем флаг; для Postgres — нет
    if url.drivername.startswith("sqlite"):
        kw["connect_args"] = {"check_same_thread": False}
    return create_engine(url, **kw)
