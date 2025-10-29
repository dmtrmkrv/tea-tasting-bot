import asyncio
import logging
import os
import datetime
import uuid
from dataclasses import dataclass
from typing import Optional, List, Dict, Union, Any

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery, BotCommand,
    InputMediaPhoto,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
    # fmt: off
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
)
from sqlalchemy.engine import make_url
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)
# fmt: on

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
    aroma_warmed: Mapped[Optional[str]] = mapped_column(nullable=True)   # объединённый «прогретый/промытый»
    aroma_after: Mapped[Optional[str]] = mapped_column(nullable=True)    # оставлено для совместимости

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
    + Твики для SQLite: WAL, NORMAL, кэши — меньше блокировок на дешёвом хостинге.
    """
    global SessionLocal
    url = make_url(db_url)
    is_sqlite = url.get_backend_name() == "sqlite"

    engine_kwargs: Dict[str, Any] = {
        "echo": False,
        "future": True,
    }
    if is_sqlite:
        engine_kwargs["connect_args"] = {
            "check_same_thread": False
        }  # безопасно и уменьшает «залипания»

    engine = create_engine(
        db_url,
        **engine_kwargs,
    )

    # PRAGMA для SQLite
    if is_sqlite:
        with engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA journal_mode=WAL;")
            conn.exec_driver_sql("PRAGMA synchronous=NORMAL;")
            conn.exec_driver_sql("PRAGMA temp_store=MEMORY;")
            conn.exec_driver_sql("PRAGMA cache_size=-20000;")  # ~20MB кэша

    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


# ---------------- ЧАСОВОЙ ПОЯС ----------------

def get_or_create_user(uid: int) -> User:
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
    u = get_or_create_user(uid)
    off = u.tz_offset_min or 0
    now_utc = datetime.datetime.utcnow()
    local_dt = now_utc + datetime.timedelta(minutes=off)
    return local_dt.strftime("%H:%M")


# ---------------- КОНСТАНТЫ UI ----------------

CATEGORIES = ["Зелёный", "Белый", "Красный", "Улун", "Шу Пуэр", "Шен Пуэр", "Хэй Ча", "Другое"]
DEFAULT_CATEGORIES = [c for c in CATEGORIES if c != "Другое"]
DEFAULT_CATEGORIES_CF = {c.casefold() for c in DEFAULT_CATEGORIES}
CUSTOM_CATEGORY_ALIASES = {"другое", "другая", "другая категория", "other"}
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


def category_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    for c in CATEGORIES:
        callback = "cat:__other__" if c == "Другое" else f"cat:{c}"
        kb.button(text=c, callback_data=callback)
    kb.adjust(2)
    return kb


def category_search_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    for c in CATEGORIES:
        callback = "scat:__other__" if c == "Другое" else f"scat:{c}"
        kb.button(text=c, callback_data=callback)
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


def back_only_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="back:main")
    kb.adjust(1)
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


def edit_menu_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="Название", callback_data="efld:name")
    kb.button(text="Год", callback_data="efld:year")
    kb.button(text="Регион", callback_data="efld:region")
    kb.button(text="Категория", callback_data="efld:category")
    kb.button(text="Граммовка", callback_data="efld:grams")
    kb.button(text="Температура", callback_data="efld:temp_c")
    kb.button(text="Время", callback_data="efld:tasted_at")
    kb.button(text="Посуда", callback_data="efld:gear")
    kb.button(text="Аромат (сухой)", callback_data="efld:aroma_dry")
    kb.button(text="Аромат (прогретый)", callback_data="efld:aroma_warmed")
    kb.button(text="Ощущения", callback_data="efld:effects")
    kb.button(text="Сценарии", callback_data="efld:scenarios")
    kb.button(text="Оценка", callback_data="efld:rating")
    kb.button(text="Заметка", callback_data="efld:summary")
    kb.button(text="Отмена", callback_data="efld:cancel")
    kb.adjust(2)
    return kb


def edit_category_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    for c in CATEGORIES:
        kb.button(text=c, callback_data=f"ecat:{c}")
    kb.button(text="Другое (ввести)", callback_data="ecat:__other__")
    kb.button(text="⬅️ Назад", callback_data="efld:menu")
    kb.adjust(2)
    return kb


def edit_rating_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    for i in range(0, 11):
        kb.button(text=str(i), callback_data=f"erate:{i}")
    kb.button(text="⬅️ Назад", callback_data="efld:menu")
    kb.adjust(6, 5, 1)
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
    choosing = State()
    waiting_text = State()


# ---------------- ХЭЛПЕРЫ UI ----------------

async def ui(target: Union[CallbackQuery, Message], text: str, reply_markup=None):
    try:
        if isinstance(target, CallbackQuery):
            msg = target.message
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
    return t.title


def build_card_text(
    t: Tasting,
    infusions: List[dict],
    photo_count: Optional[int] = None,
) -> str:
    lines = [t.title]
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
        if len(new_photos) == 1 and len(text_card) <= 1024:
            await target_message.answer_photo(
                new_photos[0],
                caption=text_card,
                reply_markup=card_actions_kb(t.id).as_markup(),
            )
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
    if val == "__other__":
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


# --- ароматы

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


def fetch_user_tastings(uid: int) -> List[Tasting]:
    with SessionLocal() as s:
        return (
            s.execute(
                select(Tasting)
                .where(Tasting.user_id == uid)
                .order_by(Tasting.id.desc())
            )
            .scalars()
            .all()
        )


def match_name(t: Tasting, q_cf: str) -> bool:
    return (t.name or "").casefold().find(q_cf) >= 0


def is_custom_category(cat: Optional[str]) -> bool:
    if not cat:
        return False
    return cat.casefold() not in DEFAULT_CATEGORIES_CF


def filter_by_category(tastings: List[Tasting], key: str) -> List[Tasting]:
    if key == "__other__":
        return [t for t in tastings if is_custom_category(t.category)]
    q_cf = key.casefold()
    return [
        t
        for t in tastings
        if (t.category or "").casefold() == q_cf
    ]


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
    _, _, payload = call.data.split(":", 2)
    try:
        uid_str, cursor_str = payload.split(":", 1)
        uid_payload = int(uid_str)
        cursor = int(cursor_str)
    except Exception:
        await call.answer()
        return

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
    q = (message.text or "").strip()
    if not q:
        await message.answer(
            "Нужно указать часть названия.",
            reply_markup=search_menu_kb().as_markup(),
        )
        await state.clear()
        return
    uid = message.from_user.id

    q_cf = q.casefold()
    tastings = fetch_user_tastings(uid)
    matches = [t for t in tastings if match_name(t, q_cf)]

    await state.clear()

    if not matches:
        await message.answer(
            "Ничего не нашёл.",
            reply_markup=search_menu_kb().as_markup(),
        )
        return

    await message.answer("Найдено:")
    first_page = matches[:PAGE_SIZE]
    for t in first_page:
        await message.answer(
            short_row(t),
            reply_markup=open_btn_kb(t.id).as_markup(),
        )

    if len(matches) > PAGE_SIZE:
        ctx = {
            "type": "name",
            "uid": uid,
            "q": q_cf,
            "ids": [t.id for t in matches],
        }
        token = new_ctx(ctx)
        await message.answer(
            "Показать ещё:",
            reply_markup=more_btn_kb(
                "name", f"{token}:{PAGE_SIZE}"
            ).as_markup(),
        )

    await message.answer(
        "Ещё варианты:", reply_markup=search_menu_kb().as_markup()
    )


async def more_name(call: CallbackQuery):
    _, _, payload = call.data.split(":", 2)
    try:
        token, sid = payload.split(":", 1)
        offset = int(sid)
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

    ids: List[int] = ctx.get("ids", [])
    if offset >= len(ids):
        try:
            await call.message.edit_reply_markup()
        except TelegramBadRequest:
            pass
        await call.message.answer(
            "Больше результатов нет.",
            reply_markup=search_menu_kb().as_markup(),
        )
        await call.answer()
        return

    next_ids = ids[offset : offset + PAGE_SIZE]
    if not next_ids:
        try:
            await call.message.edit_reply_markup()
        except TelegramBadRequest:
            pass
        await call.message.answer(
            "Больше результатов нет.",
            reply_markup=search_menu_kb().as_markup(),
        )
        await call.answer()
        return

    with SessionLocal() as s:
        rows = (
            s.execute(
                select(Tasting)
                .where(
                    Tasting.user_id == call.from_user.id,
                    Tasting.id.in_(next_ids),
                )
            )
            .scalars()
            .all()
        )

    rows_by_id = {t.id: t for t in rows}
    ordered_rows = [rows_by_id[i] for i in next_ids if i in rows_by_id]

    try:
        await call.message.edit_reply_markup()
    except TelegramBadRequest:
        pass

    if not ordered_rows:
        await call.message.answer(
            "Больше результатов нет.",
            reply_markup=search_menu_kb().as_markup(),
        )
        await call.answer()
        return

    for t in ordered_rows:
        await call.message.answer(
            short_row(t),
            reply_markup=open_btn_kb(t.id).as_markup(),
        )

    next_offset = offset + len(ordered_rows)
    if next_offset < len(ids):
        await call.message.answer(
            "Показать ещё:",
            reply_markup=more_btn_kb(
                "name", f"{token}:{next_offset}"
            ).as_markup(),
        )

    await call.answer()


# --- поиск по категории

async def s_cat(call: CallbackQuery, state: FSMContext):
    await ui(
        call,
        "Выбери категорию или введи её вручную сообщением:",
        reply_markup=category_search_kb().as_markup(),
    )
    await state.set_state(SearchFlow.category)
    await call.answer()


async def s_cat_pick(call: CallbackQuery, state: FSMContext):
    _, val = call.data.split(":", 1)
    uid = call.from_user.id

    await state.clear()

    key = "__other__" if val == "__other__" else val
    tastings = fetch_user_tastings(uid)
    matches = filter_by_category(tastings, key)

    if not matches:
        await call.message.answer(
            "Ничего не нашёл.",
            reply_markup=search_menu_kb().as_markup(),
        )
        await call.answer()
        return

    if key == "__other__":
        heading = "Найдено по пользовательским категориям:"
    else:
        heading = f"Найдено по категории «{val}»:"

    await call.message.answer(heading)
    first_page = matches[:PAGE_SIZE]
    for t in first_page:
        await call.message.answer(
            short_row(t), reply_markup=open_btn_kb(t.id).as_markup()
        )

    if len(matches) > PAGE_SIZE:
        ctx = {
            "type": "cat",
            "uid": uid,
            "key": key,
            "ids": [t.id for t in matches],
        }
        token = new_ctx(ctx)
        await call.message.answer(
            "Показать ещё:",
            reply_markup=more_btn_kb("cat", f"{token}:{PAGE_SIZE}").as_markup(),
        )
    await call.answer()


async def s_cat_text(message: Message, state: FSMContext):
    q = (message.text or "").strip()
    uid = message.from_user.id

    if not q:
        await message.answer("Нужно указать категорию.", reply_markup=search_menu_kb().as_markup())
        await state.clear()
        return

    key = "__other__" if q.casefold() in CUSTOM_CATEGORY_ALIASES else q
    tastings = fetch_user_tastings(uid)
    matches = filter_by_category(tastings, key)

    await state.clear()

    if not matches:
        await message.answer("Ничего не нашёл.", reply_markup=search_menu_kb().as_markup())
        return

    if key == "__other__":
        heading = "Найдено по пользовательским категориям:"
    else:
        heading = f"Найдено по категории «{q}»:"

    await message.answer(heading)
    first_page = matches[:PAGE_SIZE]
    for t in first_page:
        await message.answer(short_row(t), reply_markup=open_btn_kb(t.id).as_markup())

    if len(matches) > PAGE_SIZE:
        ctx = {
            "type": "cat",
            "uid": uid,
            "key": key,
            "ids": [t.id for t in matches],
        }
        token = new_ctx(ctx)
        await message.answer("Показать ещё:", reply_markup=more_btn_kb("cat", f"{token}:{PAGE_SIZE}").as_markup())


async def more_cat(call: CallbackQuery):
    _, _, payload = call.data.split(":", 2)
    try:
        token, sid = payload.split(":", 1)
        offset = int(sid)
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

    ids: List[int] = ctx.get("ids", [])
    if offset >= len(ids):
        try:
            await call.message.edit_reply_markup()
        except TelegramBadRequest:
            pass
        await call.message.answer(
            "Больше результатов нет.",
            reply_markup=search_menu_kb().as_markup(),
        )
        await call.answer()
        return

    next_ids = ids[offset : offset + PAGE_SIZE]
    if not next_ids:
        try:
            await call.message.edit_reply_markup()
        except TelegramBadRequest:
            pass
        await call.message.answer(
            "Больше результатов нет.",
            reply_markup=search_menu_kb().as_markup(),
        )
        await call.answer()
        return

    with SessionLocal() as s:
        rows = (
            s.execute(
                select(Tasting)
                .where(
                    Tasting.user_id == call.from_user.id,
                    Tasting.id.in_(next_ids),
                )
            )
            .scalars()
            .all()
        )

    rows_by_id = {t.id: t for t in rows}
    ordered_rows = [rows_by_id[i] for i in next_ids if i in rows_by_id]

    try:
        await call.message.edit_reply_markup()
    except TelegramBadRequest:
        pass

    if not ordered_rows:
        await call.message.answer(
            "Больше результатов нет.",
            reply_markup=search_menu_kb().as_markup(),
        )
        await call.answer()
        return

    for t in ordered_rows:
        await call.message.answer(short_row(t), reply_markup=open_btn_kb(t.id).as_markup())

    next_offset = offset + len(ordered_rows)
    if next_offset < len(ids):
        await call.message.answer(
            "Показать ещё:",
            reply_markup=more_btn_kb("cat", f"{token}:{next_offset}").as_markup(),
        )
    await call.answer()


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

        photo_files = (
            s.execute(
                select(Photo.file_id)
                .where(Photo.tasting_id == tid)
                .order_by(Photo.id)
            )
            .scalars()
            .all()
        )

    card_text = build_card_text(
        t, infusions_data, photo_count=len(photo_files)
    )
    actions_markup = card_actions_kb(t.id).as_markup()
    chat_id = call.message.chat.id
    bot = call.message.bot

    if photo_files:
        if len(card_text) <= 1024:
            if len(photo_files) == 1:
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=photo_files[0],
                    caption=card_text,
                    reply_markup=actions_markup,
                )
            else:
                media = [
                    InputMediaPhoto(media=photo_files[0], caption=card_text)
                ]
                media += [
                    InputMediaPhoto(media=fid)
                    for fid in photo_files[1:10]
                ]
                await bot.send_media_group(chat_id, media)
                await call.message.answer(
                    "Действия:", reply_markup=actions_markup
                )
        else:
            await call.message.answer(
                card_text, reply_markup=actions_markup
            )
            if len(photo_files) == 1:
                await bot.send_photo(chat_id, photo_files[0])
            else:
                media = [
                    InputMediaPhoto(media=fid) for fid in photo_files[:10]
                ]
                await bot.send_media_group(chat_id, media)
    else:
        await call.message.answer(
            card_text,
            reply_markup=actions_markup,
        )
    await call.answer()


async def prompt_edit_menu(target: Union[Message, CallbackQuery], state: FSMContext):
    text = (
        "Что изменить? Выбери поле. Чтобы очистить значение, пришли «-»"
        " или воспользуйся кнопкой «Отмена»."
    )
    markup = edit_menu_kb().as_markup()
    if isinstance(target, CallbackQuery):
        await target.message.answer(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)
    await state.set_state(EditFlow.choosing)


def update_tasting_fields(tid: int, uid: int, **updates: Any) -> bool:
    if not updates:
        return False
    with SessionLocal() as s:
        t = s.get(Tasting, tid)
        if not t or t.user_id != uid:
            return False
        for key, value in updates.items():
            setattr(t, key, value)
        s.commit()
    return True


EDIT_FIELD_PROMPTS: Dict[str, str] = {
    "name": "Пришли новое название.",
    "year": "Пришли год (4 цифры) или «-» чтобы очистить.",
    "region": "Пришли регион или «-» чтобы очистить.",
    "grams": "Пришли граммовку (число, можно с точкой) или «-».",
    "temp_c": "Пришли температуру (°C) или «-».",
    "tasted_at": "Пришли время дегустации в формате HH:MM или «-».",
    "gear": "Пришли посуду дегустации или «-».",
    "aroma_dry": "Пришли аромат сухого листа или «-».",
    "aroma_warmed": "Пришли аромат прогретого/промытого листа или «-».",
    "effects": "Пришли ощущения (перечисли через запятую) или «-».",
    "scenarios": "Пришли сценарии (через запятую) или «-».",
    "summary": "Пришли заметку или «-» чтобы очистить.",
}


EDIT_SUCCESS_LABELS: Dict[str, str] = {
    "name": "название",
    "year": "год",
    "region": "регион",
    "category": "категорию",
    "grams": "граммовку",
    "temp_c": "температуру",
    "tasted_at": "время дегустации",
    "gear": "посуду",
    "aroma_dry": "аромат сухого листа",
    "aroma_warmed": "аромат прогретого листа",
    "effects": "ощущения",
    "scenarios": "сценарии",
    "rating": "оценку",
    "summary": "заметку",
}


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

    await state.update_data(
        edit_t_id=tid,
        edit_field=None,
        awaiting_custom_category=False,
    )
    await prompt_edit_menu(call, state)
    await call.answer()


async def edit_field_select(call: CallbackQuery, state: FSMContext):
    _, field = call.data.split(":", 1)
    data = await state.get_data()
    tid = data.get("edit_t_id")
    if not tid:
        await call.message.answer("Не знаю, что редактировать. Начни заново.")
        await state.clear()
        await call.answer()
        return

    if field == "cancel":
        await state.clear()
        await call.message.answer("Редактирование отменено.")
        await call.answer()
        return

    if field == "menu":
        await prompt_edit_menu(call, state)
        await call.answer()
        return

    if field == "rating":
        await state.update_data(edit_field="rating", awaiting_custom_category=False)
        await call.message.answer(
            "Выбери новую оценку:", reply_markup=edit_rating_kb().as_markup()
        )
        await call.answer()
        return

    if field == "category":
        await state.update_data(
            edit_field="category",
            awaiting_custom_category=False,
        )
        await state.set_state(EditFlow.waiting_text)
        await call.message.answer(
            "Выбери новую категорию или введи её текстом:",
            reply_markup=edit_category_kb().as_markup(),
        )
        await call.answer()
        return

    prompt = EDIT_FIELD_PROMPTS.get(field)
    if not prompt:
        await call.message.answer("Этот параметр пока нельзя изменить.")
        await call.answer()
        return

    await state.update_data(edit_field=field, awaiting_custom_category=False)
    await state.set_state(EditFlow.waiting_text)
    await call.message.answer(prompt)
    await call.answer()


async def edit_category_pick(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    tid = data.get("edit_t_id")
    if not tid:
        await call.message.answer("Не знаю, что редактировать. Начни заново.")
        await state.clear()
        await call.answer()
        return

    _, val = call.data.split(":", 1)
    if val == "__other__":
        await state.update_data(
            edit_field="category",
            awaiting_custom_category=True,
        )
        await state.set_state(EditFlow.waiting_text)
        await call.message.answer("Введи новую категорию текстом:")
        await call.answer()
        return

    success = update_tasting_fields(tid, call.from_user.id, category=val)
    if success:
        await state.update_data(edit_field=None, awaiting_custom_category=False)
        await call.message.answer(f"Обновил категорию на «{val}».")
        await prompt_edit_menu(call, state)
    else:
        await call.message.answer("Не удалось обновить категорию.")
        await state.clear()
    await call.answer()


async def edit_rating_pick(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    tid = data.get("edit_t_id")
    if not tid:
        await call.message.answer("Не знаю, что редактировать. Начни заново.")
        await state.clear()
        await call.answer()
        return

    try:
        _, val = call.data.split(":", 1)
        rating = int(val)
    except Exception:
        await call.answer()
        return

    success = update_tasting_fields(tid, call.from_user.id, rating=rating)
    if success:
        await state.update_data(edit_field=None, awaiting_custom_category=False)
        await call.message.answer(f"Обновил оценку: {rating}.")
        await prompt_edit_menu(call, state)
    else:
        await call.message.answer("Не удалось обновить оценку.")
        await state.clear()
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
    field = data.get("edit_field")
    if not tid or not field:
        await message.answer("Не знаю, что редактировать.")
        await state.clear()
        return

    raw = (message.text or "").strip()
    cleared = raw == "-" or raw == ""
    updates: Dict[str, Any] = {}

    if field == "name":
        if cleared:
            await message.answer("Название не может быть пустым.")
            return
        updates["name"] = raw
    elif field == "year":
        if cleared:
            updates["year"] = None
        elif raw.isdigit():
            updates["year"] = int(raw)
        else:
            await message.answer("Нужен год цифрами или «-» для очистки.")
            return
    elif field == "region":
        updates["region"] = None if cleared else raw
    elif field == "grams":
        if cleared:
            updates["grams"] = None
        else:
            try:
                updates["grams"] = float(raw.replace(",", "."))
            except Exception:
                await message.answer("Нужно число или «-» для очистки.")
                return
    elif field == "temp_c":
        if cleared:
            updates["temp_c"] = None
        else:
            try:
                updates["temp_c"] = int(float(raw.replace(",", ".")))
            except Exception:
                await message.answer("Нужно число или «-» для очистки.")
                return
    elif field == "tasted_at":
        if cleared:
            updates["tasted_at"] = None
        else:
            try:
                datetime.datetime.strptime(raw, "%H:%M")
            except Exception:
                await message.answer("Формат HH:MM или «-».")
                return
            updates["tasted_at"] = raw
    elif field == "gear":
        updates["gear"] = None if cleared else raw
    elif field == "aroma_dry":
        updates["aroma_dry"] = None if cleared else raw
    elif field == "aroma_warmed":
        updates["aroma_warmed"] = None if cleared else raw
    elif field == "effects":
        if cleared:
            updates["effects_csv"] = None
        else:
            parts = [p.strip() for p in raw.split(",") if p.strip()]
            updates["effects_csv"] = ", ".join(parts) if parts else None
    elif field == "scenarios":
        if cleared:
            updates["scenarios_csv"] = None
        else:
            parts = [p.strip() for p in raw.split(",") if p.strip()]
            updates["scenarios_csv"] = ", ".join(parts) if parts else None
    elif field == "summary":
        updates["summary"] = None if cleared else raw
    elif field == "category":
        if cleared:
            await message.answer("Категория не может быть пустой.")
            return
        updates["category"] = raw
    else:
        await message.answer("Этот параметр пока нельзя изменить.")
        return

    success = update_tasting_fields(tid, message.from_user.id, **updates)
    if not success:
        await message.answer("Не удалось сохранить изменения.")
        await state.clear()
        return

    label = EDIT_SUCCESS_LABELS.get(field, "параметр")
    await message.answer(f"Обновил {label}.")
    await state.update_data(edit_field=None, awaiting_custom_category=False)
    await prompt_edit_menu(message, state)


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
    await state.update_data(
        edit_t_id=tid,
        edit_field=None,
        awaiting_custom_category=False,
    )
    await message.answer(f"Редактирование ID {tid}.")
    await prompt_edit_menu(message, state)


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

MAIN_MENU_TEXT = "Привет! Что делаем — создать новую запись или найти уже созданную?"
BOT_COMMANDS: List[tuple[str, str]] = [
    ("start", "Меню"),
    ("new", "Новая дегустация"),
    ("find", "Поиск"),
    ("last", "Последние 5 записей"),
    ("tz", "Настройка часового пояса"),
    ("cancel", "Сброс текущего действия"),
    ("help", "Перечень команд"),
]
COMMANDS_TEXT = "\n".join(f"/{cmd} — {desc.lower()}" for cmd, desc in BOT_COMMANDS)


async def show_main_menu(target: Union[Message, CallbackQuery]):
    markup = main_kb().as_markup()
    if isinstance(target, CallbackQuery):
        msg = target.message
        if getattr(msg, "photo", None) or getattr(msg, "caption", None):
            try:
                await msg.edit_reply_markup()
            except TelegramBadRequest:
                pass
            await msg.answer(MAIN_MENU_TEXT, reply_markup=markup)
            return
    await ui(target, MAIN_MENU_TEXT, reply_markup=markup)


async def on_start(message: Message, state: FSMContext):
    await state.clear()
    await show_main_menu(message)


async def help_cmd(message: Message):
    await message.answer(COMMANDS_TEXT)


async def cancel_cmd(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Ок, сбросил. Возвращаю в меню.")
    await show_main_menu(message)


async def help_cb(call: CallbackQuery):
    await ui(call, COMMANDS_TEXT, reply_markup=back_only_kb().as_markup())
    await call.answer()


async def back_main(call: CallbackQuery, state: FSMContext):
    await show_main_menu(call)
    await call.answer()


async def tz_cmd(message: Message):
    """
    /tz -> показать текущий сдвиг
    /tz +3    /tz -5.5 -> сохранить новый сдвиг
    """
    parts = (message.text or "").split(maxsplit=1)
    uid = message.from_user.id

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


# ---------------- РЕГИСТРАЦИЯ ХЭНДЛЕРОВ ----------------

def setup_handlers(dp: Dispatcher):
    # команды
    dp.message.register(on_start, CommandStart())
    dp.message.register(cancel_cmd, F.text.casefold() == "сброс")
    dp.message.register(cancel_cmd, Command("cancel"))
    dp.message.register(help_cmd, Command("help"))
    dp.message.register(new_cmd, Command("new"))
    dp.message.register(find_cmd, Command("find"))
    dp.message.register(last_cmd, Command("last"))
    dp.message.register(edit_cmd, Command("edit"))
    dp.message.register(delete_cmd, Command("delete"))
    dp.message.register(tz_cmd, Command("tz"))

    # STATE-хендлеры — раньше любых общих
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
    dp.message.register(aftertaste_custom, InfusionState.aftertaste)

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

    # callbacks
    dp.callback_query.register(new_cb, F.data == "new")
    dp.callback_query.register(find_cb, F.data == "find")
    dp.callback_query.register(help_cb, F.data == "help")
    dp.callback_query.register(back_main, F.data == "back:main")

    dp.callback_query.register(cat_pick, F.data.startswith("cat:"))
    dp.callback_query.register(s_cat_pick, F.data.startswith("scat:"))

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
    dp.callback_query.register(edit_field_select, F.data.startswith("efld:"))
    dp.callback_query.register(edit_category_pick, F.data.startswith("ecat:"))
    dp.callback_query.register(edit_rating_pick, F.data.startswith("erate:"))
    dp.callback_query.register(del_cb, F.data.startswith("del:"))
    dp.callback_query.register(del_ok_cb, F.data.startswith("delok:"))
    dp.callback_query.register(del_no_cb, F.data.startswith("delno:"))


async def set_bot_commands(bot: Bot):
    commands = [BotCommand(command=cmd, description=desc) for cmd, desc in BOT_COMMANDS]
    await bot.set_my_commands(commands)


# ---------------- MAIN ----------------

async def main():
    global cfg
    cfg = get_settings()
    setup_db(cfg.db_url)

    # Опционально: ускорить event loop, если добавишь uvloop в requirements
    try:
        import uvloop  # type: ignore
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    except Exception:
        pass

    bot = Bot(cfg.token)

    # ВАЖНО: дропаем «хвосты» апдейтов и гарантируем, что нет webhook
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass

    dp = Dispatcher()
    setup_handlers(dp)
    await set_bot_commands(bot)

    logging.info("Bot started")
    await dp.start_polling(
        bot,
        allowed_updates=dp.resolve_used_update_types(),
        polling_timeout=30,
        handle_signals=True,
    )


if __name__ == "__main__":
    asyncio.run(main())