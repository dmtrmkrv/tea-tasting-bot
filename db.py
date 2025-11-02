from sqlalchemy import create_engine
from sqlalchemy.engine import URL, make_url


def build_db_url_from_env(os):
    raw = os.getenv("DATABASE_URL")
    if raw:
        return raw

    user = os.getenv("POSTGRESQL_USER")
    host = os.getenv("POSTGRESQL_HOST")
    dbname = os.getenv("POSTGRESQL_DBNAME")

    if not all([user, host, dbname]):
        return "sqlite:///tastings.db"

    sslmode = os.getenv("POSTGRESQL_SSLMODE", "disable")
    query = {"sslmode": sslmode}

    return URL.create(
        drivername="postgresql+psycopg",  # psycopg3
        username=user,
        password=os.getenv("POSTGRESQL_PASSWORD"),
        host=host,
        port=int(os.getenv("POSTGRESQL_PORT", "5432")),
        database=dbname,
        query=query,
    )


def create_sa_engine(db_url_str):
    url = make_url(str(db_url_str))
    kw = dict(future=True, pool_pre_ping=True)
    # ВАЖНО: для SQLite — даем флаг; для Postgres — нет
    if url.drivername.startswith("sqlite"):
        kw["connect_args"] = {"check_same_thread": False}
    return create_engine(url, **kw)
