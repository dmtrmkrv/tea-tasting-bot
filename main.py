import asyncio
import logging
import os
import datetime
import uuid
from dataclasses import dataclass
from typing import Optional, List, Dict, Union

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery, BotCommand,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, FSInputFile,
    InputMediaPhoto,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest

from sqlalchemy import (
    create_engine,
    Integer,
    String,
    DateTime,
    ForeignKey,
    select,
    func,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)

# ---------------- ЛОГИ ----------------

logging.basicConfig(level=logging.INFO)


# ---------------- НАСТРОЙКИ ----------------

@dataclass
class Settings:
    token: str
    admin_id: Optional[int] = None
    db_url: str = "sqlite:///tastings.db"
    banner_path: Optional[str] = None


def get_settings() -> Settings:
    load_dotenv()
    token = os.getenv("BOT_TOKEN")
    admin = os.getenv("ADMIN_ID")
    db_url = os.getenv("DB_URL", "sqlite:///tastings.db")
    banner = os.getenv("BANNER_PATH")
    return Settings(
        token=token,
        admin_id=int(admin) if admin else None,
        db_url=db_url,
        banner_path=banner if banner and os.path.exists(banner) else None,
    )


cfg: Settings  # присвоим в main()


# ---------------- БД ----------------

class Base(DeclarativeBase):
    pass


class User(Base):
    """
    Таблица для пользовательских настроек.
    Сейчас используется только tz_offset_min (смещение пояса в минутах от UTC).
    """
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # telegram user_id
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow, nullable=False
    )
    tz_offset_min: Mapped[int] = mapped_column(Integer, default=0)


class Tasting(Base):
    __tablename__ = "tastings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow, nullable=False
    )

    # кто создал запись
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)

    name: Mapped[str] = mapped_column(String(200))
    year: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    region: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    category: Mapped[str] = mapped_column(String(60))

    grams: Mapped[Optional[float]] = mapped_column(nullable=True)
    temp_c: Mapped[Optional[int]] = mapped_column(nullable=True)
    tasted_at: Mapped[Optional[str]] = mapped_column(
        String(8), nullable=True
    )  # "HH:MM" локальное для юзера
    gear: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    aroma_dry: Mapped[Optional[str]] = mapped_column(nullable=True)
    aroma_warmed: Mapped[Optional[str]] = mapped_column(nullable=True)   # теперь сюда кладём «прогретый/промытый»
    aroma_after: Mapped[Optional[str]] = mapped_column(nullable=True)    # не используем, оставляем None

    effects_csv: Mapped[Optional[str]] = mapped_column(
        String(300), nullable=True
    )  # «Ощущения»
    scenarios_csv: Mapped[Optional[str]] = mapped_column(
        String(200), nullable=True
    )  # «Сценарии»

    rating: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[Optional[str]] = mapped_column(nullable=True)

    infusions: Mapped[List["Infusion"]] = relationship(
        back_populates="tasting", cascade="all, delete-orphan"
    )
    photos: Mapped[List["Photo"]] = relationship(cascade="all, delete-orphan")

    @property
    def title(self) -> str:
        parts: List[str] = [f"[{self.category}]", self.name]
        extra: List[str] = []
        if self.year:
            extra.append(str(self.year))
        if self.region:
            extra.append(self.region)
        if extra:
            parts.append("(" + ", ".join(extra) + ")")
        return " ".join(parts)


class Infusion(Base):
    __tablename__ = "infusions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tasting_id: Mapped[int] = mapped_column(
        ForeignKey("tastings.id", ondelete="CASCADE")
    )
    n: Mapped[int] = mapped_column(Integer)

    seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    liquor_color: Mapped[Optional[str]] = mapped_column(
        String(120), nullable=True
    )
    taste: Mapped[Optional[str]] = mapped_column(nullable=True)
    special_notes: Mapped[Optional[str]] = mapped_column(nullable=True)
    body: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    aftertaste: Mapped[Optional[str]] = mapped_column(nullable=True)

    tasting: Mapped[Tasting] = relationship(back_populates="infusions")


class Photo(Base):
    __tablename__ = "photos"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tasting_id: Mapped[int] = mapped_column(
        ForeignKey("tastings.id", ondelete="CASCADE")
    )
    file_id: Mapped[str] = mapped_column(String(255))


SessionLocal = None  # фабрика сессий


def setup_db(db_url: str):
    """
    Создаёт таблицы, если их нет.
    Важно: не мигрирует существующие (мы без Alembic), поэтому избегаем ломающих изменений.
    """
    global SessionLocal
    engine = create_engine(db_url, echo=False, future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


# ---------------- ЧАСОВОЙ ПОЯС ----------------

def get_or_create_user(uid: int) -> User:
    """
    Возвращает или создаёт запись о пользователе (часовой пояс и т.д.).
    """
    with SessionLocal() as s:
        u = s.get(User, uid)
        if not u:
            u = User(
                id=uid,
                created_at=datetime.datetime.utcnow(),
                tz_offset_min=0,
            )
            s.add(u)
            s.commit()
            s.refresh(u)
        return u


def set_user_tz(uid: int, offset_min: int) -> None:
    """
    Запомнить сдвиг (в минутах относительно UTC).
    """
    with SessionLocal() as s:
        u = s.get(User, uid)
        if not u:
            u = User(
                id=uid,
                created_at=datetime.datetime.utcnow(),
                tz_offset_min=offset_min,
            )
            s.add(u)
        else:
            u.tz_offset_min = offset_min
        s.commit()


def get_user_now_hm(uid: int) -> str:
    """
    Возвращает локальное время пользователя вида HH:MM
    по сохранённому смещению tz_offset_min.
    """
    u = get_or_create_user(uid)
    off = u.tz_offset_min or 0
    now_utc = datetime.datetime.utcnow()
    local_dt = now_utc + datetime.timedelta(minutes=off)
    return local_dt.strftime("%H:%M")


# ---------------- КОНСТАНТЫ UI ----------------

CATEGORIES = ["Зелёный", "Белый", "Красный", "Улун", "Шу Пуэр", "Шен Пуэр", "Хэй Ча", "Другое"]
BODY_PRESETS = ["тонкое", "лёгкое", "среднее", "плотное", "маслянистое"]

EFFECTS = [
    "Тепло",
    "Охлаждение",
    "Расслабление",
    "Фокус",
    "Бодрость",
    "Тонус",
    "Спокойствие",
    "Сонливость",
]

SCENARIOS = [
    "Отдых",
    "Работа/учеба",
    "Творчество",
    "Медитация",
    "Общение",
    "Прогулка",
]

DESCRIPTORS = [
    "сухофрукты",
    "мёд",
    "хлебные",
    "цветы",
    "орех",
    "древесный",
    "дымный",
    "ягоды",
    "фрукты",
    "травянистый",
    "овощные",
    "пряный",
    "землистый",
]

AFTERTASTE_SET = [
    "сладкий",
    "фруктовый",
    "ягодный",
    "цветочный",
    "цитрусовый",
    "кондитерский",
    "хлебный",
    "древесный",
    "пряный",
    "горький",
    "минеральный",
    "овощной",
    "землистый",
]

PAGE_SIZE = 5


# ---------------- КЛАВИАТУРЫ ----------------

def main_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Новая дегустация", callback_data="new")
    kb.button(text="🔎 Найти записи", callback_data="find")
    kb.button(text="❔ Помощь", callback_data="help")
    kb.adjust(1, 1, 1)
    return kb


def reply_main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="📝 Новая дегустация"),
                KeyboardButton(text="🔎 Найти записи"),
            ],
            [
                KeyboardButton(text="🕔 Последние 5"),
                KeyboardButton(text="❔ Помощь"),
            ],
            [KeyboardButton(text="Сброс")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выбери действие",
    )


def category_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    for c in CATEGORIES:
        kb.button(text=c, callback_data=f"cat:{c}")
    kb.adjust(2)
    return kb


def category_search_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    for c in CATEGORIES:
        kb.button(text=c, callback_data=f"scat:{c}")
    kb.button(text="Другая категория (ввести)", callback_data="scat:__other__")
    kb.adjust(2)
    return kb


def skip_kb(tag: str) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="Пропустить", callback_data=f"skip:{tag}")
    kb.adjust(1)
    return kb


def time_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="Текущее время", callback_data="time:now")
    kb.button(text="Пропустить", callback_data="skip:tasted_at")
    kb.adjust(1, 1)
    return kb


def yesno_more_infusions_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="🫖 Ещё пролив", callback_data="more_inf")
    kb.button(text="✅ Завершить", callback_data="finish_inf")
    kb.adjust(2)
    return kb


def body_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    for b in BODY_PRESETS:
        kb.button(text=b, callback_data=f"body:{b}")
    kb.button(text="Другое", callback_data="body:other")
    kb.adjust(3, 2)
    return kb


def toggle_list_kb(
    source: List[str],
    selected: List[str],
    prefix: str,
    done_text="Готово",
    include_other=False,
) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    for idx, item in enumerate(source):
        mark = "✅ " if item in selected else ""
        kb.button(text=f"{mark}{item}", callback_data=f"{prefix}:{idx}")
    if include_other:
        kb.button(text="Другое", callback_data=f"{prefix}:other")
    kb.button(text=done_text, callback_data=f"{prefix}:done")
    kb.adjust(2)
    return kb


def rating_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    for i in range(0, 11):
        kb.button(text=str(i), callback_data=f"rate:{i}")
    kb.adjust(6, 5)
    return kb


def rating_filter_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    for i in range(0, 11):
        kb.button(text=str(i), callback_data=f"frate:{i}")
    kb.adjust(6, 5)
    return kb


def search_menu_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="По названию", callback_data="s_name")
    kb.button(text="По категории", callback_data="s_cat")
    kb.button(text="По году", callback_data="s_year")
    kb.button(text="По рейтингу", callback_data="s_rating")
    kb.button(text="Последние 5", callback_data="s_last")
    kb.button(text="⬅️ Назад", callback_data="back:main")
    kb.adjust(2, 2, 2)
    return kb


def open_btn_kb(t_id: int) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="Открыть", callback_data=f"open:{t_id}")
    kb.adjust(1)
    return kb


def more_btn_kb(kind: str, payload: str) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="Показать ещё", callback_data=f"more:{kind}:{payload}")
    kb.adjust(1)
    return kb


def card_actions_kb(t_id: int) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Редактировать", callback_data=f"edit:{t_id}")
    kb.button(text="🗑️ Удалить", callback_data=f"del:{t_id}")
    kb.button(text="⬅️ Назад", callback_data="back:main")
    kb.adjust(2, 1)
    return kb


def confirm_del_kb(t_id: int) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="Да, удалить", callback_data=f"delok:{t_id}")
    kb.button(text="Отмена", callback_data=f"delno:{t_id}")
    kb.adjust(2)
    return kb


def photos_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="Готово", callback_data="photos:done")
    kb.button(text="Пропустить", callback_data="skip:photos")
    kb.adjust(2)
    return kb


# ---------------- FSM ----------------

class NewTasting(StatesGroup):
    name = State()
    year = State()
    region = State()
    category = State()
    grams = State()
    temp_c = State()
    tasted_at = State()
    gear = State()
    aroma_dry = State()
    aroma_warmed = State()   # объединённый шаг «прогретый/промытый»
    # aroma_after = State()  # Больше не используем, оставлено для совместимости


class InfusionState(StatesGroup):
    seconds = State()
    color = State()
    taste = State()
    special = State()
    body = State()
    aftertaste = State()


class EffectsScenarios(StatesGroup):
    effects = State()
    scenarios = State()


class RatingSummary(StatesGroup):
    rating = State()
    summary = State()


class PhotoFlow(StatesGroup):
    photos = State()


class SearchFlow(StatesGroup):
    name = State()
    category = State()
    year = State()


class EditFlow(StatesGroup):
    waiting_text = State()


# ---------------- ХЭЛПЕРЫ UI ----------------

async def ui(target: Union[CallbackQuery, Message], text: str, reply_markup=None):
    """
    Универсальный вывод:
    - если это callback — пытаемся отредачить предыдущее сообщение,
      если не получается (альбом и т.д.) — шлём новое.
    - если это message — просто answer().
    """
    try:
        if isinstance(target, CallbackQuery):
            msg = target.message
            # если это было фото с подписью:
            if getattr(msg, "caption", None) is not None or getattr(msg, "photo", None):
                await msg.edit_caption(caption=text, reply_markup=reply_markup)
            else:
                await msg.edit_text(text, reply_markup=reply_markup)
        else:
            await target.answer(text, reply_markup=reply_markup)
    except TelegramBadRequest:
        if isinstance(target, CallbackQuery):
            await target.message.answer(text, reply_markup=reply_markup)
        else:
            await target.answer(text, reply_markup=reply_markup)


def short_row(t: Tasting) -> str:
    return f"#{t.id} [{t.category}] {t.name}"


def build_card_text(
    t: Tasting,
    infusions: List[dict],
    photo_count: Optional[int] = None,
) -> str:
    lines = [f"{t.title}"]
    lines.append(f"⭐ Оценка: {t.rating}")
    if t.grams is not None:
        lines.append(f"⚖️ Граммовка: {t.grams} г")
    if t.temp_c is not None:
        lines.append(f"🌡️ Температура: {t.temp_c} °C")
    if t.tasted_at:
        lines.append(f"⏰ Время дегустации: {t.tasted_at}")
    if t.gear:
        lines.append(f"🍶 Посуда: {t.gear}")

    if t.aroma_dry or t.aroma_warmed:
        lines.append("🌬️ Ароматы:")
        if t.aroma_dry:
            lines.append(f"  ▫️ сухой лист: {t.aroma_dry}")
        if t.aroma_warmed:
            lines.append(f"  ▫️ прогретый/промытый лист: {t.aroma_warmed}")

    if t.effects_csv:
        lines.append(f"🧘 Ощущения: {t.effects_csv}")
    if t.scenarios_csv:
        lines.append(f"🎯 Сценарии: {t.scenarios_csv}")
    if t.summary:
        lines.append(f"📝 Заметка: {t.summary}")

    if photo_count:
        lines.append(f"📷 Фото: {photo_count} шт.")

    if infusions:
        lines.append("🫖 Проливы:")
        for inf in infusions:
            lines.append(
                f"  #{inf.get('n')}: "
                f"{(inf.get('seconds') or '-') } сек; "
                f"цвет: {inf.get('liquor_color') or '-'}; "
                f"вкус: {inf.get('taste') or '-'}; "
                f"ноты: {inf.get('special_notes') or '-'}; "
                f"тело: {inf.get('body') or '-'}; "
                f"послевкусие: {inf.get('aftertaste') or '-'}"
            )
    return "\n".join(lines)


async def append_current_infusion_and_prompt(msg_or_call, state: FSMContext):
    data = await state.get_data()
    inf = {
        "n": data.get("infusion_n", 1),
        "seconds": data.get("cur_seconds"),
        "liquor_color": data.get("cur_color"),
        "taste": data.get("cur_taste"),
        "special_notes": data.get("cur_special"),
        "body": data.get("cur_body"),
        "aftertaste": data.get("cur_aftertaste"),
    }
    infusions = data.get("infusions", [])
    infusions.append(inf)
    await state.update_data(
        infusions=infusions,
        infusion_n=inf["n"] + 1,
        cur_seconds=None,
        cur_color=None,
        cur_taste=None,
        cur_special=None,
        cur_body=None,
        cur_aftertaste=None,
        cur_taste_sel=[],
        cur_aftertaste_sel=[],
        awaiting_custom_taste=False,
        awaiting_custom_after=False,
    )

    kb = yesno_more_infusions_kb().as_markup()
    text = "Добавить ещё пролив или завершаем?"
    if isinstance(msg_or_call, Message):
        await msg_or_call.answer(text, reply_markup=kb)
    else:
        await ui(msg_or_call, text, reply_markup=kb)


async def finalize_save(target_message: Message, state: FSMContext):
    """
    Финальная сборка дегустации: создаём Tasting, Infusion, Photo,
    чистим FSM, отправляем карточку.
    """
    data = await state.get_data()
    t = Tasting(
        user_id=data.get("user_id"),
        name=data.get("name"),
        year=data.get("year"),
        region=data.get("region"),
        category=data.get("category"),
        grams=data.get("grams"),
        temp_c=data.get("temp_c"),
        tasted_at=data.get("tasted_at"),
        gear=data.get("gear"),
        aroma_dry=data.get("aroma_dry"),
        aroma_warmed=data.get("aroma_warmed"),
        aroma_after=data.get("aroma_after"),
        effects_csv=",".join(data.get("effects", [])) or None,
        scenarios_csv=",".join(data.get("scenarios", [])) or None,
        rating=data.get("rating", 0),
        summary=data.get("summary") or None,
    )

    infusions_data = data.get("infusions", [])
    new_photos: List[str] = data.get("new_photos", []) or []

    with SessionLocal() as s:
        s.add(t)
        s.flush()

        for inf in infusions_data:
            s.add(
                Infusion(
                    tasting_id=t.id,
                    n=inf["n"],
                    seconds=inf["seconds"],
                    liquor_color=inf["liquor_color"],
                    taste=inf["taste"],
                    special_notes=inf["special_notes"],
                    body=inf["body"],
                    aftertaste=inf["aftertaste"],
                )
            )

        for fid in new_photos:
            s.add(Photo(tasting_id=t.id, file_id=fid))

        s.commit()
        s.refresh(t)

    await state.clear()

    text_card = build_card_text(t, infusions_data, photo_count=len(new_photos))

    if new_photos:
        # если одно фото и текст влезает в подпись
        if len(new_photos) == 1 and len(text_card) <= 1024:
            await target_message.answer_photo(
                new_photos[0],
                caption=text_card,
                reply_markup=card_actions_kb(t.id).as_markup(),
            )
        # если несколько фото и подпись ок
        elif len(new_photos) > 1 and len(text_card) <= 1024:
            media = [InputMediaPhoto(media=new_photos[0], caption=text_card)]
            media += [InputMediaPhoto(media=fid) for fid in new_photos[1:10]]
            await target_message.bot.send_media_group(
                target_message.chat.id, media
            )
            await target_message.answer(
                "Действия:", reply_markup=card_actions_kb(t.id).as_markup()
            )
        else:
            # длинный текст карточки
            await target_message.answer(
                text_card, reply_markup=card_actions_kb(t.id).as_markup()
            )
            if len(new_photos) == 1:
                await target_message.answer_photo(new_photos[0])
            else:
                media = [InputMediaPhoto(media=fid) for fid in new_photos[:10]]
                await target_message.bot.send_media_group(
                    target_message.chat.id, media
                )
    else:
        # без фото — просто текст
        await target_message.answer(
            text_card, reply_markup=card_actions_kb(t.id).as_markup()
        )


# ---------------- ФОТО ПОСЛЕ ЗАМЕТКИ ----------------

async def prompt_photos(target: Union[Message, CallbackQuery], state: FSMContext):
    await state.update_data(new_photos=[])
    txt = (
        "📷 Добавить фото? Пришли до 3 фото. "
        "Когда готов — «Готово» или «Пропустить»."
    )
    kb = photos_kb().as_markup()
    if isinstance(target, CallbackQuery):
        await ui(target, txt, reply_markup=kb)
    else:
        await target.answer(txt, reply_markup=kb)
    await state.set_state(PhotoFlow.photos)


async def photo_add(message: Message, state: FSMContext):
    data = await state.get_data()
    photos: List[str] = data.get("new_photos", []) or []
    if not message.photo:
        await message.answer(
            "Пришли фото (или жми «Готово» / «Пропустить»)."
        )
        return
    if len(photos) >= 3:
        await message.answer("Лимит 3 фото. Жми «Готово» или «Пропустить».")
        return
    fid = message.photo[-1].file_id
    photos.append(fid)
    await state.update_data(new_photos=photos)
    await message.answer(
        f"Фото сохранено ({len(photos)}/3). Можешь прислать ещё или нажми «Готово»."
    )


async def photos_done(call: CallbackQuery, state: FSMContext):
    await finalize_save(call.message, state)
    await call.answer()


async def photos_skip(call: CallbackQuery, state: FSMContext):
    await state.update_data(new_photos=[])
    await finalize_save(call.message, state)
    await call.answer()


async def show_pics(call: CallbackQuery):
    try:
        _, sid = call.data.split(":", 1)
        tid = int(sid)
    except Exception:
        await call.answer()
        return

    with SessionLocal() as s:
        t = s.get(Tasting, tid)
        if not t or t.user_id != call.from_user.id:
            await ui(call, "Фото не найдены.")
            await call.answer()
            return
        pics = [p.file_id for p in (t.photos or [])]

    if not pics:
        await ui(call, "Фото нет.")
        await call.answer()
        return

    if len(pics) == 1:
        await call.message.answer_photo(pics[0])
    else:
        media = [InputMediaPhoto(media=fid) for fid in pics[:10]]
        await call.message.bot.send_media_group(call.message.chat.id, media)
    await call.answer()


# ---------------- СОЗДАНИЕ НОВОЙ ЗАПИСИ (опросник) ----------------

async def start_new(state: FSMContext, uid: int):
    await state.clear()
    await state.update_data(
        user_id=uid,
        infusions=[],
        effects=[],
        scenarios=[],
        infusion_n=1,
        aroma_dry_sel=[],
        aroma_warmed_sel=[],
        cur_taste_sel=[],
        cur_aftertaste_sel=[],
    )
    await state.set_state(NewTasting.name)


async def new_cmd(message: Message, state: FSMContext):
    uid = message.from_user.id
    get_or_create_user(uid)  # создадим запись юзера (для таймзоны)
    await start_new(state, uid)
    await message.answer("🍵 Название чая?")


async def new_cb(call: CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    get_or_create_user(uid)
    await start_new(state, uid)
    await ui(call, "🍵 Название чая?")
    await call.answer()


async def name_in(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer(
        "📅 Год сбора? Можно пропустить.",
        reply_markup=skip_kb("year").as_markup(),
    )
    await state.set_state(NewTasting.year)


async def year_skip(call: CallbackQuery, state: FSMContext):
    await state.update_data(year=None)
    await ui(
        call,
        "🗺️ Регион? Можно пропустить.",
        reply_markup=skip_kb("region").as_markup(),
    )
    await state.set_state(NewTasting.region)
    await call.answer()


async def year_in(message: Message, state: FSMContext):
    txt = message.text.strip()
    year = int(txt) if txt.isdigit() else None
    await state.update_data(year=year)
    await message.answer(
        "🗺️ Регион? Можно пропустить.",
        reply_markup=skip_kb("region").as_markup(),
    )
    await state.set_state(NewTasting.region)


async def region_skip(call: CallbackQuery, state: FSMContext):
    await state.update_data(region=None)
    await ui(call, "🏷️ Категория?", reply_markup=category_kb().as_markup())
    await state.set_state(NewTasting.category)
    await call.answer()


async def region_in(message: Message, state: FSMContext):
    region = message.text.strip()
    await state.update_data(region=region if region else None)
    await message.answer(
        "🏷️ Категория?", reply_markup=category_kb().as_markup()
    )
    await state.set_state(NewTasting.category)


async def cat_pick(call: CallbackQuery, state: FSMContext):
    _, val = call.data.split(":", 1)
    if val == "Другое":
        await ui(call, "Введи категорию текстом:")
        await state.update_data(awaiting_custom_cat=True)
        await call.answer()
        return
    await state.update_data(category=val)
    await ask_optional_grams_edit(call, state)
    await call.answer()


async def cat_custom_in(message: Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("awaiting_custom_cat"):
        return
    await state.update_data(
        category=message.text.strip(), awaiting_custom_cat=False
    )
    await ask_optional_grams_msg(message, state)


async def ask_optional_grams_edit(call: CallbackQuery, state: FSMContext):
    await ui(
        call,
        "⚖️ Граммовка? Можно пропустить.",
        reply_markup=skip_kb("grams").as_markup(),
    )
    await state.set_state(NewTasting.grams)


async def ask_optional_grams_msg(message: Message, state: FSMContext):
    await message.answer(
        "⚖️ Граммовка? Можно пропустить.",
        reply_markup=skip_kb("grams").as_markup(),
    )
    await state.set_state(NewTasting.grams)


async def grams_skip(call: CallbackQuery, state: FSMContext):
    await state.update_data(grams=None)
    await ui(
        call,
        "🌡️ Температура, °C? Можно пропустить.",
        reply_markup=skip_kb("temp").as_markup(),
    )
    await state.set_state(NewTasting.temp_c)
    await call.answer()


async def grams_in(message: Message, state: FSMContext):
    txt = message.text.replace(",", ".").strip()
    try:
        grams = float(txt)
    except Exception:
        grams = None
    await state.update_data(grams=grams)
    await message.answer(
        "🌡️ Температура, °C? Можно пропустить.",
        reply_markup=skip_kb("temp").as_markup(),
    )
    await state.set_state(NewTasting.temp_c)


async def temp_skip(call: CallbackQuery, state: FSMContext):
    await state.update_data(temp_c=None)
    now_hm = get_user_now_hm(call.from_user.id)
    await ui(
        call,
        f"⏰ Время дегустации? Сейчас {now_hm}. "
        "Введи HH:MM, нажми «Текущее время» или пропусти.",
        reply_markup=time_kb().as_markup(),
    )
    await state.set_state(NewTasting.tasted_at)
    await call.answer()


async def temp_in(message: Message, state: FSMContext):
    txt = message.text.strip()
    temp_val = None
    try:
        temp_val = int(float(txt))
    except Exception:
        temp_val = None
    await state.update_data(temp_c=temp_val)

    now_hm = get_user_now_hm(message.from_user.id)
    await message.answer(
        f"⏰ Время дегустации? Сейчас {now_hm}. "
        "Введи HH:MM, нажми «Текущее время» или пропусти.",
        reply_markup=time_kb().as_markup(),
    )
    await state.set_state(NewTasting.tasted_at)


async def time_now(call: CallbackQuery, state: FSMContext):
    now_hm = get_user_now_hm(call.from_user.id)
    await state.update_data(tasted_at=now_hm)
    await ui(
        call,
        "🍶 Посудa дегустации? Можно пропустить.",
        reply_markup=skip_kb("gear").as_markup(),
    )
    await state.set_state(NewTasting.gear)
    await call.answer()


async def tasted_at_skip(call: CallbackQuery, state: FSMContext):
    await state.update_data(tasted_at=None)
    await ui(
        call,
        "🍶 Посудa дегустации? Можно пропустить.",
        reply_markup=skip_kb("gear").as_markup(),
    )
    await state.set_state(NewTasting.gear)
    await call.answer()


async def tasted_at_in(message: Message, state: FSMContext):
    text_val = message.text.strip()
    ta = text_val[:5] if ":" in text_val else None
    await state.update_data(tasted_at=ta)
    await message.answer(
        "🍶 Посудa дегустации? Можно пропустить.",
        reply_markup=skip_kb("gear").as_markup(),
    )
    await state.set_state(NewTasting.gear)


async def gear_skip(call: CallbackQuery, state: FSMContext):
    await state.update_data(gear=None)
    await ask_aroma_dry_call(call, state)
    await call.answer()


async def gear_in(message: Message, state: FSMContext):
    await state.update_data(gear=message.text.strip())
    await ask_aroma_dry_msg(message, state)


# --- ароматы (мультивыбор с "Другое"). Объединено: «прогретый/промытый» в один шаг.

async def ask_aroma_dry_msg(message: Message, state: FSMContext):
    await state.update_data(aroma_dry_sel=[])
    kb = toggle_list_kb(DESCRIPTORS, [], "ad", include_other=True)
    await message.answer(
        "🌬️ Аромат сухого листа: выбери дескрипторы и нажми «Готово», или «Другое».",
        reply_markup=kb.as_markup(),
    )
    await state.set_state(NewTasting.aroma_dry)


async def ask_aroma_dry_call(call: CallbackQuery, state: FSMContext):
    await state.update_data(aroma_dry_sel=[])
    kb = toggle_list_kb(DESCRIPTORS, [], "ad", include_other=True)
    await ui(
        call,
        "🌬️ Аромат сухого листа: выбери дескрипторы и нажми «Готово», или «Другое».",
        reply_markup=kb.as_markup(),
    )
    await state.set_state(NewTasting.aroma_dry)


async def aroma_dry_toggle(call: CallbackQuery, state: FSMContext):
    _, tail = call.data.split(":", 1)
    data = await state.get_data()
    selected = data.get("aroma_dry_sel", [])
    if tail == "done":
        await state.update_data(aroma_dry=", ".join(selected) if selected else None)
        kb = toggle_list_kb(DESCRIPTORS, [], "aw", include_other=True)
        await ui(
            call,
            "🌬️ Аромат прогретого/промытого листа: выбери и нажми «Готово».",
            reply_markup=kb.as_markup(),
        )
        await state.set_state(NewTasting.aroma_warmed)
        await call.answer()
        return
    if tail == "other":
        await state.update_data(awaiting_custom_ad=True)
        await ui(call, "Введи аромат сухого листа текстом:")
        await call.answer()
        return
    idx = int(tail)
    item = DESCRIPTORS[idx]
    if item in selected:
        selected.remove(item)
    else:
        selected.append(item)
    await state.update_data(aroma_dry_sel=selected)
    kb = toggle_list_kb(DESCRIPTORS, selected, "ad", include_other=True)
    try:
        await call.message.edit_reply_markup(reply_markup=kb.as_markup())
    except TelegramBadRequest:
        pass
    await call.answer()


async def aroma_dry_custom(message: Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("awaiting_custom_ad"):
        return
    selected = data.get("aroma_dry_sel", [])
    if message.text.strip():
        selected.append(message.text.strip())
    await state.update_data(
        aroma_dry=", ".join(selected) if selected else None,
        awaiting_custom_ad=False,
    )
    kb = toggle_list_kb(DESCRIPTORS, [], "aw", include_other=True)
    await message.answer(
        "🌬️ Аромат прогретого/промытого листа: выбери и нажми «Готово».",
        reply_markup=kb.as_markup(),
    )
    await state.set_state(NewTasting.aroma_warmed)


async def aroma_warmed_toggle(call: CallbackQuery, state: FSMContext):
    _, tail = call.data.split(":", 1)
    data = await state.get_data()
    selected = data.get("aroma_warmed_sel", [])
    if tail == "done":
        await state.update_data(
            aroma_warmed=", ".join(selected) if selected else None
        )
        await start_infusion_block_call(call, state)
        await call.answer()
        return
    if tail == "other":
        await state.update_data(awaiting_custom_aw=True)
        await ui(call, "Введи аромат прогретого/промытого листа текстом:")
        await call.answer()
        return
    idx = int(tail)
    item = DESCRIPTORS[idx]
    if item in selected:
        selected.remove(item)
    else:
        selected.append(item)
    await state.update_data(aroma_warmed_sel=selected)
    kb = toggle_list_kb(DESCRIPTORS, selected, "aw", include_other=True)
    try:
        await call.message.edit_reply_markup(reply_markup=kb.as_markup())
    except TelegramBadRequest:
        pass
    await call.answer()


async def aroma_warmed_custom(message: Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("awaiting_custom_aw"):
        return
    selected = data.get("aroma_warmed_sel", [])
    if message.text.strip():
        selected.append(message.text.strip())
    await state.update_data(
        aroma_warmed=", ".join(selected) if selected else None,
        awaiting_custom_aw=False,
    )
    await start_infusion_block_msg(message, state)


# --- проливы

async def start_infusion_block_msg(message: Message, state: FSMContext):
    data = await state.get_data()
    n = data.get("infusion_n", 1)
    await message.answer(f"🫖 Пролив {n}. Время, сек?")
    await state.set_state(InfusionState.seconds)


async def start_infusion_block_call(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    n = data.get("infusion_n", 1)
    await ui(call, f"🫖 Пролив {n}. Время, сек?")
    await state.set_state(InfusionState.seconds)
    await call.answer()


async def inf_seconds(message: Message, state: FSMContext):
    txt = message.text.strip()
    val = int(txt) if txt.isdigit() else None
    await state.update_data(cur_seconds=val)
    await message.answer(
        "Цвет настоя пролива? Можно пропустить.",
        reply_markup=skip_kb("color").as_markup(),
    )
    await state.set_state(InfusionState.color)


async def color_skip(call: CallbackQuery, state: FSMContext):
    await state.update_data(cur_color=None)
    await state.update_data(cur_taste_sel=[])
    kb = toggle_list_kb(DESCRIPTORS, [], "taste", include_other=True)
    await ui(
        call,
        "Вкус настоя: выбери дескрипторы и нажми «Готово», или «Другое».",
        reply_markup=kb.as_markup(),
    )
    await state.set_state(InfusionState.taste)
    await call.answer()


async def inf_color(message: Message, state: FSMContext):
    await state.update_data(cur_color=message.text.strip())
    await state.update_data(cur_taste_sel=[])
    kb = toggle_list_kb(DESCRIPTORS, [], "taste", include_other=True)
    await message.answer(
        "Вкус настоя: выбери дескрипторы и нажми «Готово», или «Другое».",
        reply_markup=kb.as_markup(),
    )
    await state.set_state(InfusionState.taste)


async def taste_toggle(call: CallbackQuery, state: FSMContext):
    _, tail = call.data.split(":", 1)
    data = await state.get_data()
    selected = data.get("cur_taste_sel", [])
    if tail == "done":
        text_val = ", ".join(selected) if selected else None
        await state.update_data(cur_taste=text_val, awaiting_custom_taste=False)
        await ui(
            call,
            "✨ Особенные ноты пролива? (можно пропустить)",
            reply_markup=skip_kb("special").as_markup(),
        )
        await state.set_state(InfusionState.special)
        await call.answer()
        return
    if tail == "other":
        await state.update_data(awaiting_custom_taste=True)
        await ui(call, "Введи вкус текстом:")
        await call.answer()
        return
    idx = int(tail)
    item = DESCRIPTORS[idx]
    if item in selected:
        selected.remove(item)
    else:
        selected.append(item)
    await state.update_data(cur_taste_sel=selected)
    kb = toggle_list_kb(DESCRIPTORS, selected, "taste", include_other=True)
    try:
        await call.message.edit_reply_markup(reply_markup=kb.as_markup())
    except TelegramBadRequest:
        pass
    await call.answer()


async def taste_custom(message: Message, state: FSMContext):
    data = await state.get_data()
    # если человек сразу шлёт текст вместо выбора, просто примем
    if not data.get("awaiting_custom_taste"):
        await state.update_data(cur_taste=message.text.strip() or None)
        await message.answer(
            "✨ Особенные ноты пролива? (можно пропустить)",
            reply_markup=skip_kb("special").as_markup(),
        )
        await state.set_state(InfusionState.special)
        return

    await state.update_data(
        cur_taste=message.text.strip() or None,
        awaiting_custom_taste=False,
    )
    await message.answer(
        "✨ Особенные ноты пролива? (можно пропустить)",
        reply_markup=skip_kb("special").as_markup(),
    )
    await state.set_state(InfusionState.special)


async def inf_taste(message: Message, state: FSMContext):
    await state.update_data(
        cur_taste=message.text.strip() or None,
        awaiting_custom_taste=False,
    )
    await message.answer(
        "✨ Особенные ноты пролива? (можно пропустить)",
        reply_markup=skip_kb("special").as_markup(),
    )
    await state.set_state(InfusionState.special)


async def special_skip(call: CallbackQuery, state: FSMContext):
    await state.update_data(cur_special=None)
    await ui(call, "Тело настоя?", reply_markup=body_kb().as_markup())
    await state.set_state(InfusionState.body)
    await call.answer()


async def inf_special(message: Message, state: FSMContext):
    await state.update_data(cur_special=message.text.strip())
    await message.answer("Тело настоя?", reply_markup=body_kb().as_markup())
    await state.set_state(InfusionState.body)


async def inf_body_pick(call: CallbackQuery, state: FSMContext):
    _, val = call.data.split(":", 1)
    if val == "other":
        await ui(call, "Введи тело настоя текстом:")
        await state.update_data(awaiting_custom_body=True)
        await state.set_state(InfusionState.body)
        await call.answer()
        return
    await state.update_data(cur_body=val)
    await state.update_data(cur_aftertaste_sel=[])
    kb = toggle_list_kb(AFTERTASTE_SET, [], "aft", include_other=True)
    await ui(
        call,
        "Характер послевкусия: выбери пункты и нажми «Готово», или «Другое».",
        reply_markup=kb.as_markup(),
    )
    await state.set_state(InfusionState.aftertaste)
    await call.answer()


async def inf_body_custom(message: Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("awaiting_custom_body"):
        return
    await state.update_data(
        cur_body=message.text.strip(), awaiting_custom_body=False
    )
    kb = toggle_list_kb(AFTERTASTE_SET, [], "aft", include_other=True)
    await message.answer(
        "Характер послевкусия: выбери пункты и нажми «Готово», или «Другое».",
        reply_markup=kb.as_markup(),
    )
    await state.set_state(InfusionState.aftertaste)


async def aftertaste_toggle(call: CallbackQuery, state: FSMContext):
    _, tail = call.data.split(":", 1)
    data = await state.get_data()
    selected = data.get("cur_aftertaste_sel", [])
    if tail == "done":
        await state.update_data(
            cur_aftertaste=", ".join(selected) if selected else None,
            awaiting_custom_after=False,
        )
        await append_current_infusion_and_prompt(call, state)
        await call.answer()
        return
    if tail == "other":
        await state.update_data(awaiting_custom_after=True)
        await ui(call, "Введи характер послевкусия текстом:")
        await call.answer()
        return
    idx = int(tail)
    item = AFTERTASTE_SET[idx]
    if item in selected:
        selected.remove(item)
    else:
        selected.append(item)
    await state.update_data(cur_aftertaste_sel=selected)
    kb = toggle_list_kb(AFTERTASTE_SET, selected, "aft", include_other=True)
    try:
        await call.message.edit_reply_markup(reply_markup=kb.as_markup())
    except TelegramBadRequest:
        pass
    await call.answer()


async def aftertaste_custom(message: Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("awaiting_custom_after"):
        await state.update_data(cur_aftertaste=message.text.strip() or None)
        await append_current_infusion_and_prompt(message, state)
        return
    await state.update_data(
        cur_aftertaste=message.text.strip() or None,
        awaiting_custom_after=False,
    )
    await append_current_infusion_and_prompt(message, state)


async def more_infusions(call: CallbackQuery, state: FSMContext):
    await start_infusion_block_call(call, state)


async def finish_infusions(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = data.get("effects", [])
    kb = toggle_list_kb(
        EFFECTS, selected, prefix="eff", include_other=True
    )
    await ui(
        call,
        "Ощущения (мультивыбор). Жми пункты, затем «Готово», либо «Другое».",
        reply_markup=kb.as_markup(),
    )
    await state.set_state(EffectsScenarios.effects)
    await call.answer()


# --- ощущения / сценарии / оценка / заметка

async def eff_toggle_or_done(call: CallbackQuery, state: FSMContext):
    _, tail = call.data.split(":", 1)
    data = await state.get_data()
    selected = data.get("effects", [])
    if tail == "done":
        kb = toggle_list_kb(
            SCENARIOS,
            data.get("scenarios", []),
            prefix="scn",
            include_other=True,
        )
        await ui(
            call,
            "Сценарии (мультивыбор). Жми пункты, затем «Готово», либо «Другое».",
            reply_markup=kb.as_markup(),
        )
        await state.set_state(EffectsScenarios.scenarios)
        await call.answer()
        return
    if tail == "other":
        await state.update_data(awaiting_custom_eff=True)
        await ui(call, "Введи ощущение текстом:")
        await call.answer()
        return
    idx = int(tail)
    item = EFFECTS[idx]
    if item in selected:
        selected.remove(item)
    else:
        selected.append(item)
    await state.update_data(effects=selected)
    kb = toggle_list_kb(
        EFFECTS, selected, prefix="eff", include_other=True
    )
    try:
        await call.message.edit_reply_markup(reply_markup=kb.as_markup())
    except TelegramBadRequest:
        pass
    await call.answer()


async def eff_custom(message: Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("awaiting_custom_eff"):
        return
    selected = data.get("effects", [])
    txt = message.text.strip()
    if txt:
        selected.append(txt)
    await state.update_data(effects=selected, awaiting_custom_eff=False)
    kb = toggle_list_kb(
        EFFECTS, selected, prefix="eff", include_other=True
    )
    await message.answer(
        "Добавил. Можешь выбрать ещё и нажать «Готово».",
        reply_markup=kb.as_markup(),
    )
    await state.set_state(EffectsScenarios.effects)


async def scn_toggle_or_done(call: CallbackQuery, state: FSMContext):
    _, tail = call.data.split(":", 1)
    data = await state.get_data()
    selected = data.get("scenarios", [])
    if tail == "done":
        await ui(
            call,
            "Оценка сорта 0..10?",
            reply_markup=rating_kb().as_markup(),
        )
        await state.set_state(RatingSummary.rating)
        await call.answer()
        return
    if tail == "other":
        await state.update_data(awaiting_custom_scn=True)
        await ui(call, "Введи сценарий текстом:")
        await call.answer()
        return
    idx = int(tail)
    item = SCENARIOS[idx]
    if item in selected:
        selected.remove(item)
    else:
        selected.append(item)
    await state.update_data(scenarios=selected)
    kb = toggle_list_kb(
        SCENARIOS, selected, prefix="scn", include_other=True
    )
    try:
        await call.message.edit_reply_markup(reply_markup=kb.as_markup())
    except TelegramBadRequest:
        pass
    await call.answer()


async def scn_custom(message: Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("awaiting_custom_scn"):
        return
    selected = data.get("scenarios", [])
    txt = message.text.strip()
    if txt:
        selected.append(txt)
    await state.update_data(scenarios=selected, awaiting_custom_scn=False)
    kb = toggle_list_kb(
        SCENARIOS, selected, prefix="scn", include_other=True
    )
    await message.answer(
        "Добавил. Можешь выбрать ещё и нажать «Готово».",
        reply_markup=kb.as_markup(),
    )
    await state.set_state(EffectsScenarios.scenarios)


async def rate_pick(call: CallbackQuery, state: FSMContext):
    _, val = call.data.split(":", 1)
    await state.update_data(rating=int(val))
    await ui(
        call,
        "📝 Заметка по дегустации? (можно пропустить)",
        reply_markup=skip_kb("summary").as_markup(),
    )
    await state.set_state(RatingSummary.summary)
    await call.answer()


async def rating_in(message: Message, state: FSMContext):
    txt = message.text.strip()
    rating = int(txt) if txt.isdigit() else 0
    rating = max(0, min(10, rating))
    await state.update_data(rating=rating)
    await message.answer(
        "📝 Заметка по дегустации? (можно пропустить)",
        reply_markup=skip_kb("summary").as_markup(),
    )
    await state.set_state(RatingSummary.summary)


async def summary_in(message: Message, state: FSMContext):
    await state.update_data(summary=message.text.strip())
    await prompt_photos(message, state)


async def summary_skip(call: CallbackQuery, state: FSMContext):
    await state.update_data(summary=None)
    await prompt_photos(call, state)
    await call.answer()


# ---------------- ПОИСК / ЛЕНТА ----------------

SEARCH_CTX: Dict[str, dict] = {}


def new_ctx(data: dict) -> str:
    token = uuid.uuid4().hex[:8]
    SEARCH_CTX[token] = data
    return token


def get_ctx(token: str) -> Optional[dict]:
    return SEARCH_CTX.get(token)


def has_more_last(min_id: int, uid: Optional[int] = None) -> bool:
    with SessionLocal() as s:
        q = select(Tasting.id).where(Tasting.id < min_id)
        if uid is not None:
            q = q.where(Tasting.user_id == uid)
        nxt = (
            s.execute(q.order_by(Tasting.id.desc()).limit(1))
            .scalars()
            .first()
        )
        return nxt is not None


async def find_cb(call: CallbackQuery):
    await ui(
        call,
        "Выбери способ поиска:",
        reply_markup=search_menu_kb().as_markup(),
    )
    await call.answer()


async def find_cmd(message: Message):
    await message.answer(
        "Выбери способ поиска:",
        reply_markup=search_menu_kb().as_markup(),
    )


async def s_last(call: CallbackQuery):
    uid = call.from_user.id

    with SessionLocal() as s:
        rows = (
            s.execute(
                select(Tasting)
                .where(Tasting.user_id == uid)
                .order_by(Tasting.id.desc())
                .limit(PAGE_SIZE)
            )
            .scalars()
            .all()
        )

    if not rows:
        await call.message.answer(
            "Пока пусто.", reply_markup=search_menu_kb().as_markup()
        )
        await call.answer()
        return

    await call.message.answer("Последние записи:")
    for t in rows:
        await call.message.answer(
            short_row(t),
            reply_markup=open_btn_kb(t.id).as_markup(),
        )

    min_id = rows[-1].id
    if has_more_last(min_id, uid):
        payload = f"{uid}:{min_id}"
        await call.message.answer(
            "Показать ещё:",
            reply_markup=more_btn_kb("last", payload).as_markup(),
        )

    await call.message.answer(
        "Ещё варианты:", reply_markup=search_menu_kb().as_markup()
    )
    await call.answer()


async def last_cmd(message: Message):
    uid = message.from_user.id
    with SessionLocal() as s:
        rows = (
            s.execute(
                select(Tasting)
                .where(Tasting.user_id == uid)
                .order_by(Tasting.id.desc())
                .limit(PAGE_SIZE)
            )
            .scalars()
            .all()
        )

    if not rows:
        await message.answer(
            "Пока пусто.", reply_markup=search_menu_kb().as_markup()
        )
        return

    await message.answer("Последние записи:")
    for t in rows:
        await message.answer(
            short_row(t),
            reply_markup=open_btn_kb(t.id).as_markup(),
        )

    min_id = rows[-1].id
    if has_more_last(min_id, uid):
        payload = f"{uid}:{min_id}"
        await message.answer(
            "Показать ещё:",
            reply_markup=more_btn_kb("last", payload).as_markup(),
        )

    await message.answer(
        "Ещё варианты:", reply_markup=search_menu_kb().as_markup()
    )


async def more_last(call: CallbackQuery):
    # more:last:<uid>:<cursor>
    _, _, payload = call.data.split(":", 2)
    try:
        uid_str, cursor_str = payload.split(":", 1)
        uid_payload = int(uid_str)
        cursor = int(cursor_str)
    except Exception:
        await call.answer()
        return

    # защита: чужие uid не разрешаем
    if uid_payload != call.from_user.id:
        try:
            await call.message.edit_reply_markup()
        except TelegramBadRequest:
            pass
        await call.message.answer(
            "Контекст поиска устарел. Запусти поиск заново.",
            reply_markup=search_menu_kb().as_markup(),
        )
        await call.answer()
        return

    with SessionLocal() as s:
        rows = (
            s.execute(
                select(Tasting)
                .where(Tasting.user_id == call.from_user.id, Tasting.id < cursor)
                .order_by(Tasting.id.desc())
                .limit(PAGE_SIZE)
            )
            .scalars()
            .all()
        )

    try:
        await call.message.edit_reply_markup()
    except TelegramBadRequest:
        pass

    if not rows:
        await call.message.answer(
            "Больше записей нет.", reply_markup=search_menu_kb().as_markup()
        )
        await call.answer()
        return

    for t in rows:
        await call.message.answer(
            short_row(t),
            reply_markup=open_btn_kb(t.id).as_markup(),
        )

    min_id = rows[-1].id
    if has_more_last(min_id, call.from_user.id):
        payload2 = f"{call.from_user.id}:{min_id}"
        await call.message.answer(
            "Показать ещё:",
            reply_markup=more_btn_kb("last", payload2).as_markup(),
        )

    await call.answer()


# --- поиск по названию

async def s_name(call: CallbackQuery, state: FSMContext):
    await ui(call, "Введи часть названия чая:")
    await state.set_state(SearchFlow.name)
    await call.answer()


async def s_name_run(message: Message, state: FSMContext):
    q = message.text.strip()
    uid = message.from_user.id

    token = new_ctx({"type": "name", "q": q, "uid": uid})

    with SessionLocal() as s:
        rows = (
            s.execute(
                select(Tasting)
                .where(
                    Tasting.user_id == uid,
                    func.lower(Tasting.name).like(f"%{q.lower()}%"),
                )
                .order_by(Tasting.id.desc())
                .limit(PAGE_SIZE)
            )
            .scalars()
            .all()
        )

    await state.clear()

    if not rows:
        await message.answer(
            "Ничего не нашёл.",
            reply_markup=search_menu_kb().as_markup(),
        )
        return

    await message.answer("Найдено:")
    for t in rows:
        await message.answer(
            short_row(t),
            reply_markup=open_btn_kb(t.id).as_markup(),
        )

    min_id = rows[-1].id

    with SessionLocal() as s:
        more = (
            s.execute(
                select(Tasting.id)
                .where(
                    Tasting.user_id == uid,
                    func.lower(Tasting.name).like(f"%{q.lower()}%"),
                    Tasting.id < min_id,
                )
                .order_by(Tasting.id.desc())
                .limit(1)
            )
            .scalars()
            .first()
            is not None
        )

    if more:
        await message.answer(
            "Показать ещё:",
            reply_markup=more_btn_kb(
                "name", f"{token}:{min_id}"
            ).as_markup(),
        )

    await message.answer(
        "Ещё варианты:", reply_markup=search_menu_kb().as_markup()
    )


async def more_name(call: CallbackQuery):
    # more:name:<token>:<cursor>
    _, _, payload = call.data.split(":", 2)
    try:
        token, sid = payload.split(":", 1)
        cursor = int(sid)
    except Exception:
        await call.answer()
        return

    ctx = get_ctx(token)
    if not ctx or ctx.get("type") != "name" or ctx.get("uid") != call.from_user.id:
        try:
            await call.message.edit_reply_markup()
        except TelegramBadRequest:
            pass
        await call.message.answer(
            "Контекст поиска устарел. Запусти поиск заново.",
            reply_markup=search_menu_kb().as_markup(),
        )
        await call.answer()
        return

    q = ctx["q"]
    uid = ctx["uid"]

    with SessionLocal() as s:
        rows = (
            s.execute(
                select(Tasting)
                .where(
                    Tasting.user_id == uid,
                    func.lower(Tasting.name).like(f"%{q.lower()}%"),
                    Tasting.id < cursor,
                )
                .order_by(Tasting.id.desc())
                .limit(PAGE_SIZE)
            )
            .scalars()
            .all()
        )

    try:
        await call.message.edit_reply_markup()
    except TelegramBadRequest:
        pass

    if not rows:
        await call.message.answer(
            "Больше результатов нет.",
            reply_markup=search_menu_kb().as_markup(),
        )
        await call.answer()
        return

    for t in rows:
        await call.message.answer(
            short_row(t),
            reply_markup=open_btn_kb(t.id).as_markup(),
        )

    min_id = rows[-1].id

    with SessionLocal() as s:
        more = (
            s.execute(
                select(Tasting.id)
                .where(
                    Tasting.user_id == uid,
                    func.lower(Tasting.name).like(f"%{q.lower()}%"),
                    Tasting.id < min_id,
                )
                .order_by(Tasting.id.desc())
                .limit(1)
            )
            .scalars()
            .first()
            is not None
        )
    if more:
        await call.message.answer(
            "Показать ещё:",
            reply_markup=more_btn_kb(
                "name", f"{token}:{min_id}"
            ).as_markup(),
        )

    await call.answer()


# --- поиск по категории

async def s_cat(call: CallbackQuery, state: FSMContext):
    await ui(
        call,
        "Выбери категорию или укажи вручную:",
        reply_markup=category_search_kb().as_markup(),
    )
    await state.clear()
    await call.answer()


async def s_cat_pick(call: CallbackQuery):
    _, val = call.data.split(":", 1)
    uid = call.from_user.id

    if val == "__other__":
        await ui(call, "Введи категорию текстом:")
        # переиспользуем SearchFlow.category
        fsm = FSMContext(storage=None, key=None)  # заглушка для типизации
        # но контекст уже есть у dp — поэтому просто выставим через call.message.bot не нужно.
        # В aiogram v3 нам нужен state из хендлера — обойдёмся отдельным message-хендлером:
        # Здесь просто подскажем пользователю прислать текст, а само состояние установлено выше не требуется.
        # Реальное состояние ставим явным хендлером ниже (см. register).
        await call.answer()
        return

    token = new_ctx({"type": "cat", "cat": val, "uid": uid})

    with SessionLocal() as s:
        rows = (
            s.execute(
                select(Tasting)
                .where(
                    Tasting.user_id == uid,
                    func.lower(Tasting.category) == val.lower(),
                )
                .order_by(Tasting.id.desc())
                .limit(PAGE_SIZE)
            )
            .scalars()
            .all()
        )

    if not rows:
        await call.message.answer(
            "Ничего не нашёл.",
            reply_markup=search_menu_kb().as_markup(),
        )
        await call.answer()
        return

    await call.message.answer(f"Найдено по категории «{val}»:")
    for t in rows:
        await call.message.answer(short_row(t), reply_markup=open_btn_kb(t.id).as_markup())

    min_id = rows[-1].id

    with SessionLocal() as s:
        more = (
            s.execute(
                select(Tasting.id)
                .where(
                    Tasting.user_id == uid,
                    func.lower(Tasting.category) == val.lower(),
                    Tasting.id < min_id,
                )
                .order_by(Tasting.id.desc()).limit(1)
            ).scalars().first()
            is not None
        )
    if more:
        await call.message.answer(
            "Показать ещё:",
            reply_markup=more_btn_kb("cat", f"{token}:{min_id}").as_markup(),
        )
    await call.answer()


async def s_cat_text(message: Message, state: FSMContext):
    # ручной ввод категории
    q = (message.text or "").strip()
    uid = message.from_user.id

    token = new_ctx({"type": "cat", "cat": q, "uid": uid})

    with SessionLocal() as s:
        rows = (
            s.execute(
                select(Tasting)
                .where(
                    Tasting.user_id == uid,
                    func.lower(Tasting.category) == q.lower(),
                )
                .order_by(Tasting.id.desc())
                .limit(PAGE_SIZE)
            ).scalars().all()
        )

    if not rows:
        await message.answer("Ничего не нашёл.", reply_markup=search_menu_kb().as_markup())
        return

    await message.answer(f"Найдено по категории «{q}»:")
    for t in rows:
        await message.answer(short_row(t), reply_markup=open_btn_kb(t.id).as_markup())

    min_id = rows[-1].id

    with SessionLocal() as s:
        more = (
            s.execute(
                select(Tasting.id)
                .where(
                    Tasting.user_id == uid,
                    func.lower(Tasting.category) == q.lower(),
                    Tasting.id < min_id,
                )
                .order_by(Tasting.id.desc()).limit(1)
            ).scalars().first()
            is not None
        )
    if more:
        await message.answer(
            "Показать ещё:",
            reply_markup=more_btn_kb("cat", f"{token}:{min_id}").as_markup(),
        )


async def more_cat(call: CallbackQuery):
    # more:cat:<token>:<cursor>
    _, _, payload = call.data.split(":", 2)
    try:
        token, sid = payload.split(":", 1)
        cursor = int(sid)
    except Exception:
        await call.answer()
        return

    ctx = get_ctx(token)
    if not ctx or ctx.get("type") != "cat" or ctx.get("uid") != call.from_user.id:
        try:
            await call.message.edit_reply_markup()
        except TelegramBadRequest:
            pass
        await call.message.answer(
            "Контекст поиска устарел. Запусти поиск заново.",
            reply_markup=search_menu_kb().as_markup(),
        )
        await call.answer()
        return

    cat = ctx["cat"]
    uid = ctx["uid"]

    with SessionLocal() as s:
        rows = (
            s.execute(
                select(Tasting)
                .where(
                    Tasting.user_id == uid,
                    func.lower(Tasting.category) == cat.lower(),
                    Tasting.id < cursor,
                )
                .order_by(Tasting.id.desc()).limit(PAGE_SIZE)
            ).scalars().all()
        )

    try:
        await call.message.edit_reply_markup()
    except TelegramBadRequest:
        pass

    if not rows:
        await call.message.answer("Больше результатов нет.", reply_markup=search_menu_kb().as_markup())
        await call.answer()
        return

    for t in rows:
        await call.message.answer(short_row(t), reply_markup=open_btn_kb(t.id).as_markup())

    min_id = rows[-1].id
    with SessionLocal() as s:
        more = (
            s.execute(
                select(Tasting.id)
                .where(
                    Tasting.user_id == uid,
                    func.lower(Tasting.category) == cat.lower(),
                    Tasting.id < min_id,
                )
                .order_by(Tasting.id.desc()).limit(1)
            ).scalars().first()
            is not None
        )
    if more:
        await call.message.answer(
            "Показать ещё:",
            reply_markup=more_btn_kb("cat", f"{token}:{min_id}").as_markup(),
        )
    await call.answer()


# --- поиск по году

async def s_year(call: CallbackQuery, state: FSMContext):
    await ui(
        call,
        "Введи год (4 цифры):",
    )
    await state.set_state(SearchFlow.year)
    await call.answer()


async def s_year_run(message: Message, state: FSMContext):
    txt = (message.text or "").strip()
    if not txt.isdigit():
        await message.answer("Нужно число, например 2020.", reply_markup=search_menu_kb().as_markup())
        await state.clear()
        return
    year = int(txt)
    uid = message.from_user.id
    token = new_ctx({"type": "year", "year": year, "uid": uid})

    with SessionLocal() as s:
        rows = (
            s.execute(
                select(Tasting)
                .where(Tasting.user_id == uid, Tasting.year == year)
                .order_by(Tasting.id.desc()).limit(PAGE_SIZE)
            ).scalars().all()
        )
    await state.clear()

    if not rows:
        await message.answer("Ничего не нашёл.", reply_markup=search_menu_kb().as_markup())
        return

    await message.answer(f"Найдено за {year}:")
    for t in rows:
        await message.answer(short_row(t), reply_markup=open_btn_kb(t.id).as_markup())

    min_id = rows[-1].id
    with SessionLocal() as s:
        more = (
            s.execute(
                select(Tasting.id)
                .where(Tasting.user_id == uid, Tasting.year == year, Tasting.id < min_id)
                .order_by(Tasting.id.desc()).limit(1)
            ).scalars().first()
            is not None
        )
    if more:
        await message.answer(
            "Показать ещё:",
            reply_markup=more_btn_kb("year", f"{token}:{min_id}").as_markup(),
        )


async def more_year(call: CallbackQuery):
    # more:year:<token>:<cursor>
    _, _, payload = call.data.split(":", 2)
    try:
        token, sid = payload.split(":", 1)
        cursor = int(sid)
    except Exception:
        await call.answer()
        return

    ctx = get_ctx(token)
    if not ctx or ctx.get("type") != "year" or ctx.get("uid") != call.from_user.id:
        try:
            await call.message.edit_reply_markup()
        except TelegramBadRequest:
            pass
        await call.message.answer(
            "Контекст поиска устарел. Запусти поиск заново.",
            reply_markup=search_menu_kb().as_markup(),
        )
        await call.answer()
        return

    year = ctx["year"]
    uid = ctx["uid"]

    with SessionLocal() as s:
        rows = (
            s.execute(
                select(Tasting)
                .where(Tasting.user_id == uid, Tasting.year == year, Tasting.id < cursor)
                .order_by(Tasting.id.desc()).limit(PAGE_SIZE)
            ).scalars().all()
        )

    try:
        await call.message.edit_reply_markup()
    except TelegramBadRequest:
        pass

    if not rows:
        await call.message.answer("Больше результатов нет.", reply_markup=search_menu_kb().as_markup())
        await call.answer()
        return

    for t in rows:
        await call.message.answer(short_row(t), reply_markup=open_btn_kb(t.id).as_markup())

    min_id = rows[-1].id
    with SessionLocal() as s:
        more = (
            s.execute(
                select(Tasting.id)
                .where(Tasting.user_id == uid, Tasting.year == year, Tasting.id < min_id)
                .order_by(Tasting.id.desc()).limit(1)
            ).scalars().first()
            is not None
        )
    if more:
        await call.message.answer(
            "Показать ещё:",
            reply_markup=more_btn_kb("year", f"{token}:{min_id}").as_markup(),
        )
    await call.answer()


# --- поиск по рейтингу (не ниже X)

async def s_rating(call: CallbackQuery):
    await ui(call, "Минимальная оценка?", reply_markup=rating_filter_kb().as_markup())
    await call.answer()


async def rating_filter_pick(call: CallbackQuery):
    _, val = call.data.split(":", 1)
    try:
        thr = int(val)
    except Exception:
        await call.answer()
        return

    uid = call.from_user.id
    token = new_ctx({"type": "rating", "thr": thr, "uid": uid})

    with SessionLocal() as s:
        rows = (
            s.execute(
                select(Tasting)
                .where(Tasting.user_id == uid, Tasting.rating >= thr)
                .order_by(Tasting.id.desc()).limit(PAGE_SIZE)
            ).scalars().all()
        )

    if not rows:
        await call.message.answer("Ничего не нашёл.", reply_markup=search_menu_kb().as_markup())
        await call.answer()
        return

    await call.message.answer(f"Найдено с оценкой ≥ {thr}:")
    for t in rows:
        await call.message.answer(short_row(t), reply_markup=open_btn_kb(t.id).as_markup())

    min_id = rows[-1].id
    with SessionLocal() as s:
        more = (
            s.execute(
                select(Tasting.id)
                .where(Tasting.user_id == uid, Tasting.rating >= thr, Tasting.id < min_id)
                .order_by(Tasting.id.desc()).limit(1)
            ).scalars().first()
            is not None
        )
    if more:
        await call.message.answer(
            "Показать ещё:",
            reply_markup=more_btn_kb("rating", f"{token}:{min_id}").as_markup(),
        )
    await call.answer()


async def more_rating(call: CallbackQuery):
    # more:rating:<token>:<cursor>
    _, _, payload = call.data.split(":", 2)
    try:
        token, sid = payload.split(":", 1)
        cursor = int(sid)
    except Exception:
        await call.answer()
        return

    ctx = get_ctx(token)
    if not ctx or ctx.get("type") != "rating" or ctx.get("uid") != call.from_user.id:
        try:
            await call.message.edit_reply_markup()
        except TelegramBadRequest:
            pass
        await call.message.answer(
            "Контекст поиска устарел. Запусти поиск заново.",
            reply_markup=search_menu_kb().as_markup(),
        )
        await call.answer()
        return

    thr = ctx["thr"]
    uid = ctx["uid"]

    with SessionLocal() as s:
        rows = (
            s.execute(
                select(Tasting)
                .where(Tasting.user_id == uid, Tasting.rating >= thr, Tasting.id < cursor)
                .order_by(Tasting.id.desc()).limit(PAGE_SIZE)
            ).scalars().all()
        )

    try:
        await call.message.edit_reply_markup()
    except TelegramBadRequest:
        pass

    if not rows:
        await call.message.answer("Больше результатов нет.", reply_markup=search_menu_kb().as_markup())
        await call.answer()
        return

    for t in rows:
        await call.message.answer(short_row(t), reply_markup=open_btn_kb(t.id).as_markup())

    min_id = rows[-1].id
    with SessionLocal() as s:
        more = (
            s.execute(
                select(Tasting.id)
                .where(Tasting.user_id == uid, Tasting.rating >= thr, Tasting.id < min_id)
                .order_by(Tasting.id.desc()).limit(1)
            ).scalars().first()
            is not None
        )
    if more:
        await call.message.answer(
            "Показать ещё:",
            reply_markup=more_btn_kb("rating", f"{token}:{min_id}").as_markup(),
        )
    await call.answer()


# ---------------- ОТКРЫТИЕ / РЕДАКТ / УДАЛЕНИЕ ----------------

async def open_card(call: CallbackQuery):
    try:
        _, sid = call.data.split(":", 1)
        tid = int(sid)
    except Exception:
        await call.answer()
        return

    with SessionLocal() as s:
        t = s.get(Tasting, tid)
        if not t or t.user_id != call.from_user.id:
            await call.message.answer("Запись не найдена.")
            await call.answer()
            return

        inf_list = (
            s.execute(
                select(Infusion)
                .where(Infusion.tasting_id == tid)
                .order_by(Infusion.n)
            )
            .scalars()
            .all()
        )
        infusions_data = [
            {
                "n": inf.n,
                "seconds": inf.seconds,
                "liquor_color": inf.liquor_color,
                "taste": inf.taste,
                "special_notes": inf.special_notes,
                "body": inf.body,
                "aftertaste": inf.aftertaste,
            }
            for inf in inf_list
        ]

        photo_count = (
            s.execute(
                select(func.count(Photo.id)).where(Photo.tasting_id == tid)
            )
            .scalar_one()
        )

    card_text = build_card_text(
        t, infusions_data, photo_count=photo_count or 0
    )
    await call.message.answer(
        card_text,
        reply_markup=card_actions_kb(t.id).as_markup(),
    )
    await call.answer()


async def edit_cb(call: CallbackQuery, state: FSMContext):
    try:
        _, sid = call.data.split(":", 1)
        tid = int(sid)
    except Exception:
        await call.answer()
        return

    with SessionLocal() as s:
        t = s.get(Tasting, tid)
        if not t or t.user_id != call.from_user.id:
            await call.message.answer("Нет доступа к этой записи.")
            await call.answer()
            return

    await state.update_data(edit_t_id=tid)
    await state.set_state(EditFlow.waiting_text)
    await call.message.answer(
        "Пришли новый текст заметки. Старое значение перезапишется."
    )
    await call.answer()


async def del_cb(call: CallbackQuery):
    try:
        _, sid = call.data.split(":", 1)
        tid = int(sid)
    except Exception:
        await call.answer()
        return
    with SessionLocal() as s:
        t = s.get(Tasting, tid)
        if not t or t.user_id != call.from_user.id:
            await call.message.answer("Нет доступа к этой записи.")
            await call.answer()
            return
    await call.message.answer(
        f"Удалить #{tid}?",
        reply_markup=confirm_del_kb(tid).as_markup(),
    )
    await call.answer()


async def del_ok_cb(call: CallbackQuery):
    try:
        _, sid = call.data.split(":", 1)
        tid = int(sid)
    except Exception:
        await call.answer()
        return
    with SessionLocal() as s:
        t = s.get(Tasting, tid)
        if not t or t.user_id != call.from_user.id:
            await call.message.answer("Нет доступа к этой записи.")
            await call.answer()
            return
        s.delete(t)
        s.commit()
    await call.message.answer("Удалил.")
    await call.answer()


async def del_no_cb(call: CallbackQuery):
    await call.message.answer("Ок, не удаляю.")
    await call.answer()


async def edit_flow_msg(message: Message, state: FSMContext):
    data = await state.get_data()
    tid = data.get("edit_t_id")
    if not tid:
        await message.answer("Не знаю, что редактировать.")
        await state.clear()
        return
    with SessionLocal() as s:
        t = s.get(Tasting, tid)
        if not t or t.user_id != message.from_user.id:
            await message.answer("Нет доступа к этой записи.")
            await state.clear()
            return
        t.summary = (message.text or "").strip()
        s.commit()
    await message.answer("Обновил заметку.")
    await state.clear()


# команды /edit и /delete напрямую

async def edit_cmd(message: Message, state: FSMContext):
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: /edit <id>")
        return
    tid = int(parts[1])
    with SessionLocal() as s:
        t = s.get(Tasting, tid)
        if not t or t.user_id != message.from_user.id:
            await message.answer("Нет доступа к этой записи.")
            return
    await state.update_data(edit_t_id=tid)
    await state.set_state(EditFlow.waiting_text)
    await message.answer(
        f"Редактирование #{tid}. Пришли новый текст заметки."
    )


async def delete_cmd(message: Message):
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: /delete <id>")
        return
    tid = int(parts[1])
    with SessionLocal() as s:
        t = s.get(Tasting, tid)
        if not t or t.user_id != message.from_user.id:
            await message.answer("Нет доступа к этой записи.")
            return
    await message.answer(
        f"Удалить #{tid}?",
        reply_markup=confirm_del_kb(tid).as_markup(),
    )


# ---------------- КОМАНДЫ /start /help /tz и т.п. ----------------

async def show_main_menu(bot: Bot, chat_id: int):
    caption = "Привет! Что делаем — создать новую запись или найти уже созданную?"
    await bot.send_message(
        chat_id=chat_id,
        text=caption,
        reply_markup=main_kb().as_markup(),
    )


async def on_start(message: Message):
    await show_main_menu(message.bot, message.chat.id)


async def help_cmd(message: Message):
    await message.answer(
        "/start — меню\n"
        "/new — новая дегустация\n"
        "/find — поиск (по названию, категории, году, рейтингу, последние 5)\n"
        "/last — последние 5\n"
        "/tz — часовой пояс\n"
        "/menu — включить кнопки под вводом (сквозное меню)\n"
        "/hide — скрыть кнопки\n"
        "/cancel — сброс текущего действия\n"
        "/edit <id> — редактировать заметку\n"
        "/delete <id> — удалить запись"
    )


async def cancel_cmd(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Ок, сбросил. Возвращаю в меню.",
        reply_markup=main_kb().as_markup(),
    )


async def menu_cmd(message: Message):
    await message.answer(
        "Включил кнопки под полем ввода.",
        reply_markup=reply_main_kb(),
    )


async def hide_cmd(message: Message):
    await message.answer("Скрываю кнопки.", reply_markup=ReplyKeyboardRemove())


async def reply_buttons_router(message: Message, state: FSMContext):
    t = (message.text or "").strip()
    if "Новая дегустация" in t:
        await new_cmd(message, state)
    elif "Найти записи" in t:
        await find_cmd(message)
    elif "Последние 5" in t:
        await last_cmd(message)
    elif "Помощь" in t or "О боте" in t:
        await help_cmd(message)
    elif t == "Сброс" or t == "Отмена":
        await cancel_cmd(message, state)


async def help_cb(call: CallbackQuery):
    await call.message.answer(
        "/start — меню\n"
        "/new — новая дегустация\n"
        "/find — поиск (по названию, категории, году, рейтингу, последние 5)\n"
        "/last — последние 5\n"
        "/tz — часовой пояс\n"
        "/menu — включить кнопки под вводом (сквозное меню)\n"
        "/hide — скрыть кнопки\n"
        "/cancel — сброс текущего действия\n"
        "/edit <id> — редактировать заметку\n"
        "/delete <id> — удалить запись",
        reply_markup=search_menu_kb().as_markup(),
    )
    await call.answer()


async def back_main(call: CallbackQuery):
    await show_main_menu(call.message.bot, call.message.chat.id)
    await call.answer()


async def tz_cmd(message: Message):
    """
    /tz -> показать текущий сдвиг
    /tz +3    /tz -5.5 -> сохранить новый сдвиг
    """
    parts = (message.text or "").split(maxsplit=1)
    uid = message.from_user.id

    # просто посмотреть
    if len(parts) == 1:
        u = get_or_create_user(uid)
        hours_float = (u.tz_offset_min or 0) / 60.0
        sign = "+" if hours_float >= 0 else ""
        await message.answer(
            "Твой локальный сдвиг (UTC): "
            f"UTC{sign}{hours_float:g}\n\n"
            "Чтобы поменять:\n"
            "/tz +3\n"
            "/tz -5.5"
        )
        return

    raw = parts[1].strip()
    raw = raw.replace("UTC", "").replace("utc", "")
    try:
        hours_float = float(raw)
    except Exception:
        await message.answer(
            "Не понял формат. Пример: /tz +3 или /tz -5.5"
        )
        return

    offset_min = int(round(hours_float * 60))
    set_user_tz(uid, offset_min)
    sign = "+" if hours_float >= 0 else ""
    await message.answer(
        f"Запомнил UTC{sign}{hours_float:g}. "
        "Теперь буду подставлять твоё локальное время."
    )


# ---------------- РЕГИСТРАЦИЯ ХЭНДЛЕРОВ В DISPATCHER ----------------

def setup_handlers(dp: Dispatcher):
    # команды
    dp.message.register(on_start, CommandStart())
    dp.message.register(help_cmd, Command("help"))
    dp.message.register(cancel_cmd, Command("cancel"))
    dp.message.register(menu_cmd, Command("menu"))
    dp.message.register(hide_cmd, Command("hide"))
    dp.message.register(new_cmd, Command("new"))
    dp.message.register(find_cmd, Command("find"))
    dp.message.register(last_cmd, Command("last"))
    dp.message.register(edit_cmd, Command("edit"))
    dp.message.register(delete_cmd, Command("delete"))
    dp.message.register(tz_cmd, Command("tz"))

    # опросник новой дегустации (STATE-handlers — идут раньше любых «общих» router'ов!)
    dp.message.register(name_in, NewTasting.name)
    dp.message.register(year_in, NewTasting.year)
    dp.message.register(region_in, NewTasting.region)
    dp.message.register(cat_custom_in, NewTasting.category)
    dp.message.register(grams_in, NewTasting.grams)
    dp.message.register(temp_in, NewTasting.temp_c)
    dp.message.register(tasted_at_in, NewTasting.tasted_at)
    dp.message.register(gear_in, NewTasting.gear)
    dp.message.register(aroma_dry_custom, NewTasting.aroma_dry)
    dp.message.register(aroma_warmed_custom, NewTasting.aroma_warmed)

    dp.message.register(inf_seconds, InfusionState.seconds)
    dp.message.register(inf_color, InfusionState.color)
    dp.message.register(taste_custom, InfusionState.taste)
    dp.message.register(inf_taste, InfusionState.taste)
    dp.message.register(inf_special, InfusionState.special)
    dp.message.register(inf_body_custom, InfusionState.body)

    dp.message.register(rating_in, RatingSummary.rating)
    dp.message.register(summary_in, RatingSummary.summary)

    dp.message.register(eff_custom, EffectsScenarios.effects)
    dp.message.register(scn_custom, EffectsScenarios.scenarios)

    dp.message.register(photo_add, PhotoFlow.photos)

    # поиск (message)
    dp.message.register(s_name_run, SearchFlow.name)
    dp.message.register(s_cat_text, SearchFlow.category)
    dp.message.register(s_year_run, SearchFlow.year)

    # редактирование заметки
    dp.message.register(edit_flow_msg, EditFlow.waiting_text)

    # reply-кнопки под полем ввода — В САМОМ КОНЦЕ message-хендлеров!
    dp.message.register(reply_buttons_router)

    # callbacks
    dp.callback_query.register(new_cb, F.data == "new")
    dp.callback_query.register(find_cb, F.data == "find")
    dp.callback_query.register(help_cb, F.data == "help")
    dp.callback_query.register(back_main, F.data == "back:main")

    dp.callback_query.register(year_skip, F.data == "skip:year")
    dp.callback_query.register(region_skip, F.data == "skip:region")
    dp.callback_query.register(grams_skip, F.data == "skip:grams")
    dp.callback_query.register(temp_skip, F.data == "skip:temp")
    dp.callback_query.register(time_now, F.data == "time:now")
    dp.callback_query.register(tasted_at_skip, F.data == "skip:tasted_at")
    dp.callback_query.register(gear_skip, F.data == "skip:gear")

    dp.callback_query.register(aroma_dry_toggle, F.data.startswith("ad:"))
    dp.callback_query.register(aroma_warmed_toggle, F.data.startswith("aw:"))

    dp.callback_query.register(color_skip, F.data == "skip:color")
    dp.callback_query.register(taste_toggle, F.data.startswith("taste:"))
    dp.callback_query.register(special_skip, F.data == "skip:special")
    dp.callback_query.register(inf_body_pick, F.data.startswith("body:"))
    dp.callback_query.register(aftertaste_toggle, F.data.startswith("aft:"))

    dp.callback_query.register(more_infusions, F.data == "more_inf")
    dp.callback_query.register(finish_infusions, F.data == "finish_inf")

    dp.callback_query.register(eff_toggle_or_done, F.data.startswith("eff:"))
    dp.callback_query.register(scn_toggle_or_done, F.data.startswith("scn:"))

    dp.callback_query.register(rate_pick, F.data.startswith("rate:"))
    dp.callback_query.register(summary_skip, F.data == "skip:summary")

    dp.callback_query.register(photos_done, F.data == "photos:done")
    dp.callback_query.register(photos_skip, F.data == "skip:photos")
    dp.callback_query.register(show_pics, F.data.startswith("pics:"))

    # поиск / меню / пагинация
    dp.callback_query.register(s_last, F.data == "s_last")
    dp.callback_query.register(s_name, F.data == "s_name")
    dp.callback_query.register(s_cat, F.data == "s_cat")
    dp.callback_query.register(s_year, F.data == "s_year")
    dp.callback_query.register(s_rating, F.data == "s_rating")

    dp.callback_query.register(rating_filter_pick, F.data.startswith("frate:"))
    dp.callback_query.register(more_last, F.data.startswith("more:last:"))
    dp.callback_query.register(more_name, F.data.startswith("more:name:"))
    dp.callback_query.register(more_cat, F.data.startswith("more:cat:"))
    dp.callback_query.register(more_year, F.data.startswith("more:year:"))
    dp.callback_query.register(more_rating, F.data.startswith("more:rating:"))

    # карточка
    dp.callback_query.register(open_card, F.data.startswith("open:"))
    dp.callback_query.register(edit_cb, F.data.startswith("edit:"))
    dp.callback_query.register(del_cb, F.data.startswith("del:"))
    dp.callback_query.register(del_ok_cb, F.data.startswith("delok:"))
    dp.callback_query.register(del_no_cb, F.data.startswith("delno:"))


async def set_bot_commands(bot: Bot):
    commands = [
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="new", description="Новая дегустация"),
        BotCommand(command="find", description="Поиск"),
        BotCommand(command="last", description="Последние 5"),
        BotCommand(command="tz", description="Часовой пояс"),
        BotCommand(command="help", description="Помощь"),
    ]
    await bot.set_my_commands(commands)


# ---------------- MAIN ----------------

async def main():
    global cfg
    cfg = get_settings()
    setup_db(cfg.db_url)

    bot = Bot(cfg.token)
    dp = Dispatcher()
    setup_handlers(dp)
    await set_bot_commands(bot)

    logging.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
