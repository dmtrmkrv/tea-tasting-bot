"""Start command handler."""
from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Привет! Отправь команду /tasting, чтобы сохранить новую дегустацию, "
        "или /health для проверки состояния сервиса."
    )
