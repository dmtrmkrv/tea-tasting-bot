"""Health and diagnostic handlers."""
from __future__ import annotations

from urllib.parse import urlparse

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import text
from sqlalchemy.engine import Engine, make_url

router = Router()


@router.message(Command("health"))
async def cmd_health(message: Message) -> None:
    bot = message.bot
    engine: Engine = bot["db_engine"]
    db_status = "OK"
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - diagnostic output
        db_status = f"FAIL ({type(exc).__name__})"

    backend = bot["storage_backend"]
    if backend == "s3":
        s3_health = bot.get("s3_health")
        if s3_health and not s3_health.ok:
            s3_status = f"WARN ({s3_health.message})"
        else:
            s3_status = "OK"
    else:
        s3_status = "disabled"

    await message.answer(f"DB: {db_status}\nS3: {s3_status}")


@router.message(Command("dbinfo"))
async def cmd_dbinfo(message: Message) -> None:
    bot = message.bot
    raw_url: str = bot["db_url"]
    backend: str = bot["storage_backend"]

    driver = "-"
    db_name = "-"
    host_or_path = "-"
    sslmode = "-"

    try:
        url = make_url(raw_url)
        driver = url.drivername or "-"
        if driver.startswith("sqlite"):
            host_or_path = url.database or "-"
            db_name = url.database or "-"
        else:
            host_or_path = url.host or "-"
            db_name = url.database or "-"
            sslmode = url.query.get("sslmode") or "-"
    except Exception:
        driver = "unknown"

    lines = [
        f"driver={driver}",
        f"db={db_name}",
        f"host_or_path={host_or_path}",
        f"sslmode={sslmode}",
        f"photos_backend={backend}",
    ]

    if backend == "s3":
        settings = bot["s3_settings"]
        health = bot.get("s3_health")
        endpoint = urlparse(settings.endpoint_url)
        host = endpoint.netloc or endpoint.path or "-"
        status = "OK" if not health or health.ok else f"WARN ({health.message})"
        lines.append(f"s3_endpoint_host={host}")
        lines.append(f"s3_bucket={settings.bucket}")
        lines.append(f"s3_status={status}")
    else:
        local_dir = bot["local_storage_dir"]
        lines.append(f"photos_dir={local_dir}")
        lines.append("s3_status=disabled")

    await message.answer("\n".join(lines))
