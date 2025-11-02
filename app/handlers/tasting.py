"""Handlers responsible for tasting creation flow."""
from __future__ import annotations

import asyncio
from io import BytesIO

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, PhotoSize

from app.db.engine import get_session
from app.db.models import Photo, Tasting, User
from app.storage.base import SavePhotoResult, Storage

router = Router()


class TastingForm(StatesGroup):
    waiting_title = State()
    waiting_category = State()
    waiting_notes = State()
    waiting_photos = State()


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Диалог отменён.")


@router.message(Command("tasting"))
async def cmd_tasting(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        await message.answer("Не удалось определить пользователя.")
        return
    await state.clear()
    await _ensure_user(message.from_user.id)
    await message.answer(
        "Создаем новую дегустацию. Введи название чая или дегустации."\
    )
    await state.set_state(TastingForm.waiting_title)


@router.message(TastingForm.waiting_title)
async def process_title(message: Message, state: FSMContext) -> None:
    title = message.text.strip() if message.text else ""
    if not title:
        await message.answer("Название не может быть пустым. Попробуй еще раз.")
        return
    await state.update_data(title=title)
    await message.answer(
        "Категория (например, шэн, улун). Можно пропустить командой /skip."\
    )
    await state.set_state(TastingForm.waiting_category)


@router.message(TastingForm.waiting_category)
async def process_category(message: Message, state: FSMContext) -> None:
    category = None
    if message.text and message.text.strip().lower() not in {"/skip", "skip"}:
        category = message.text.strip()
    await state.update_data(category=category)
    await message.answer(
        "Добавь заметки к дегустации. Можно пропустить командой /skip."
    )
    await state.set_state(TastingForm.waiting_notes)


@router.message(TastingForm.waiting_notes)
async def process_notes(message: Message, state: FSMContext) -> None:
    notes = None
    if message.text and message.text.strip().lower() not in {"/skip", "skip"}:
        notes = message.text.strip()

    data = await state.get_data()
    if not message.from_user:
        await message.answer("Не удалось определить пользователя.")
        await state.clear()
        return
    user_id = message.from_user.id
    tasting_id = await _create_tasting(
        user_id=user_id,
        title=data["title"],
        category=data.get("category"),
        notes=notes,
    )
    await state.update_data(tasting_id=tasting_id)
    await message.answer(
        "Отправь фото дегустации. Можно отправить несколько фото. Когда закончишь — напиши /done или /skip."
    )
    await state.set_state(TastingForm.waiting_photos)


@router.message(TastingForm.waiting_photos, Command("done"))
@router.message(TastingForm.waiting_photos, Command("skip"))
async def finish_photos(message: Message, state: FSMContext) -> None:
    await message.answer("Дегустация сохранена. Спасибо!")
    await state.clear()


@router.message(TastingForm.waiting_photos)
async def process_photo(message: Message, state: FSMContext) -> None:
    if not message.photo:
        await message.answer("Пожалуйста, отправь фото или команду /done.")
        return

    photo = message.photo[-1]
    data = await state.get_data()
    tasting_id = data.get("tasting_id")
    if not tasting_id:
        await message.answer("Не удалось найти дегустацию. Попробуй снова.")
        await state.clear()
        return

    storage: Storage = message.bot["storage"]
    try:
        photo_bytes, content_type = await _download_photo(message, photo)
    except TelegramBadRequest:
        await message.answer("Не удалось скачать фото. Попробуй другое изображение.")
        return

    result = await storage.save_photo(
        user_id=message.from_user.id if message.from_user else 0,
        tasting_id=tasting_id,
        data=photo_bytes,
        content_type=content_type,
    )
    await _store_photo_metadata(tasting_id, result, content_type)

    await message.answer("Фото сохранено. Отправь еще или используй /done.")


async def _ensure_user(user_id: int) -> None:
    def _sync() -> None:
        with get_session() as session:
            user = session.get(User, user_id)
            if user is None:
                session.add(User(id=user_id))

    await asyncio.to_thread(_sync)


async def _create_tasting(
    user_id: int,
    title: str,
    category: str | None,
    notes: str | None,
) -> int:
    def _sync() -> int:
        with get_session() as session:
            tasting = Tasting(
                user_id=user_id,
                title=title,
                category=category,
                notes=notes,
            )
            session.add(tasting)
            session.flush()
            return tasting.id

    return await asyncio.to_thread(_sync)


async def _store_photo_metadata(
    tasting_id: int,
    result: SavePhotoResult,
    content_type: str,
) -> None:
    def _sync() -> None:
        with get_session() as session:
            photo = Photo(
                tasting_id=tasting_id,
                backend=result.backend,
                key=result.key,
                content_type=content_type,
                url=result.url,
            )
            session.add(photo)

    await asyncio.to_thread(_sync)


async def _download_photo(message: Message, photo: PhotoSize) -> tuple[bytes, str]:
    buffer = BytesIO()
    await message.bot.download(photo, destination=buffer)
    content_type = photo.mime_type if hasattr(photo, "mime_type") and photo.mime_type else "image/jpeg"
    return buffer.getvalue(), content_type
