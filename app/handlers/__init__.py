"""Register application routers."""
from aiogram import Dispatcher

from . import info, start, tasting


def setup_handlers(dp: Dispatcher) -> None:
    dp.include_router(start.router)
    dp.include_router(info.router)
    dp.include_router(tasting.router)
