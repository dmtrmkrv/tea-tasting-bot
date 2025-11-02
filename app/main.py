"""Application entrypoint for the tea tasting bot."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand
from dotenv import load_dotenv

from app import config
from app.config import get_bot_token
from app.storage.factory import get_storage_from_env

logger = logging.getLogger(__name__)


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(name)s: %(message)s")


def run_migrations_sync() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", config.get_db_url())
    command.upgrade(cfg, "head")


async def run_migrations() -> None:
    logger.info("Applying database migrations...")
    await asyncio.to_thread(run_migrations_sync)


async def setup_bot() -> tuple[Bot, Dispatcher]:
    token = get_bot_token()
    bot = Bot(token=token, parse_mode=None)
    dp = Dispatcher()
    return bot, dp


async def on_startup(bot: Bot, dp: Dispatcher) -> None:
    from app.db.engine import engine
    from app.handlers import setup_handlers

    storage, s3_health = get_storage_from_env()
    backend = getattr(storage, "backend", "local")

    bot["storage"] = storage
    bot["storage_backend"] = backend
    bot["db_engine"] = engine
    bot["db_url"] = config.get_db_url()

    if backend == "s3":
        bot["s3_settings"] = config.get_s3_settings()
        bot["s3_health"] = s3_health
    else:
        base_dir = getattr(storage, "base_path", None)
        bot["local_storage_dir"] = str(base_dir) if base_dir else config.get_local_storage_settings().base_dir

    setup_handlers(dp)

    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Начать"),
            BotCommand(command="tasting", description="Новая дегустация"),
            BotCommand(command="health", description="Проверка состояния"),
            BotCommand(command="dbinfo", description="Информация о БД"),
        ]
    )


async def main() -> None:
    load_dotenv()
    configure_logging()
    await run_migrations()

    bot, dp = await setup_bot()
    await on_startup(bot, dp)

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
