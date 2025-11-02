import asyncio
import base64
import datetime
import logging
import re
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple, Union

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery, BotCommand,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, FSInputFile,
    InputMediaPhoto, InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
    # fmt: off
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
    inspect,
    select,
)
from sqlalchemy import Index, desc
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)
# fmt: on

from db import build_db_url_from_env, create_sa_engine

# ---------------- ОКРУЖЕНИЕ И ЛОГИ ----------------

load_dotenv()

_LEGACY_ENV = os.getenv("ENV")
APP_ENV = os.getenv("APP_ENV") or _LEGACY_ENV or "production"

_log_level = logging.DEBUG if APP_ENV.lower() != "production" else logging.INFO
logging.basicConfig(level=_log_level)
logger = logging.getLogger(__name__)


def _int_from_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    raw = raw.strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Игнорирую некорректное значение %s=%r", name, raw)
        return default


_legacy_db_url = os.getenv("DB_URL")
DATABASE_URL = os.getenv("DATABASE_URL") or _legacy_db_url or "sqlite:///tastings.db"
PAGE_SIZE = _int_from_env("PAGE_SIZE", 5)
_photo_limit_raw = os.getenv("PHOTO_LIMIT")
if _photo_limit_raw is not None and _photo_limit_raw.strip():
    PHOTO_LIMIT = _int_from_env("PHOTO_LIMIT", 3)
else:
    PHOTO_LIMIT = _int_from_env("MAX_PHOTOS", 3)
MAX_PHOTOS = PHOTO_LIMIT


# ---------------- НАСТРОЙКИ ----------------

def _raw_admins_string() -> str:
    admins_env = os.getenv("ADMINS", "")
    legacy_admin = os.getenv("ADMIN_ID", "").strip()
    if legacy_admin:
        admins_env = f"{admins_env},{legacy_admin}" if admins_env else legacy_admin
    return admins_env


def _parse_admins() -> Tuple[Set[int], List[str]]:
    admins_raw = _raw_admins_string()
    tokens = [token for token in re.split(r"[\s,]+", admins_raw) if token]
    admins = {int(token) for token in tokens if token.strip().isdigit()}
    invalid = [token for token in tokens if token and not token.strip().isdigit()]
    return admins, invalid


ADMINS, _invalid_admin_tokens = _parse_admins()
if _invalid_admin_tokens:
    logger.warning(
        "Игнорирую некорректные значения ADMINS: %s",
        ", ".join(_invalid_admin_tokens),
    )


@dataclass
class Settings:
    token: str
    admin_ids: Set[int]
    db_url: str = DATABASE_URL
    db_path: Optional[str] = None
    banner_path: Optional[str] = None


def get_settings() -> Settings:
    token = os.getenv("BOT_TOKEN")
    db_url = DATABASE_URL
    banner = os.getenv("BANNER_PATH")
    db_path = None
    try:
        url = make_url(db_url)
        if url.drivername.startswith("sqlite") and url.database and url.database != ":memory:":
            db_path = os.path.abspath(url.database)
    except Exception:
        db_path = None
    return Settings(
        token=token,
        admin_ids=set(ADMINS),
        db_url=db_url,
        db_path=db_path,
        banner_path=banner if banner and os.path.exists(banner) else None,
    )


cfg: Optional[Settings] = None  # присвоим в main()


# ---------------- БД ----------------


def is_admin(user_id: Optional[int]) -> bool:
    return bool(user_id is not None and user_id in ADMINS)


async def ensure_admin_message(message: Message) -> bool:
    if message.from_user and is_admin(message.from_user.id):
        return True
    await message.answer("Команда доступна только админам.")
    return False


def resolved_db_path() -> str:
    if cfg and cfg.db_path:
        return cfg.db_path
    if cfg:
        return cfg.db_url
    return DATABASE_URL


def environment_summary() -> str:
    env_display = _LEGACY_ENV or "-"
    admins_display: List[int] = sorted(ADMINS) if ADMINS else []
    return (
        "APP_ENV={app_env} | ENV={env} | DB={db} | PAGE_SIZE={page} | "
        "PHOTO_LIMIT={photo} | ADMINS={admins}"
    ).format(
        app_env=APP_ENV,
        env=env_display,
        db=resolved_db_path(),
        page=PAGE_SIZE,
        photo=PHOTO_LIMIT,
        admins=admins_display,
    )


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
    __table_args__ = (
        UniqueConstraint("user_id", "seq_no", name="uq_tastings_user_seq_no"),
        Index("ix_tastings_user_category", "user_id", "category"),
        Index("ix_tastings_user_year", "user_id", "year"),
        Index("ix_tastings_user_rating", "user_id", "rating"),
        Index("ix_tastings_user_id_desc", "user_id", desc("id")),
    )

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
    seq_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

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
engine: Optional[Engine] = None


def setup_db(db_url: str):
    """
    Создаёт таблицы, если их нет.
    + Твики для SQLite: WAL, NORMAL, кэши — меньше блокировок на дешёвом хостинге.
    """
    global SessionLocal, engine
    old_engine = engine
    env_db_url = build_db_url_from_env(os)
    if db_url and db_url != DATABASE_URL:
        db_url_obj = db_url
    else:
        db_url_obj = env_db_url
    if cfg is not None:
        cfg.db_url = str(db_url_obj)
        try:
            url_obj = make_url(cfg.db_url)
            if (
                url_obj.drivername.startswith("sqlite")
                and url_obj.database
                and url_obj.database != ":memory:"
            ):
                cfg.db_path = os.path.abspath(url_obj.database)
            else:
                cfg.db_path = None
        except Exception:
            cfg.db_path = None
    engine = create_sa_engine(db_url_obj)

    # безопасный лог без пароля
    safe_db_url = str(db_url_obj)
    pwd = os.getenv("POSTGRESQL_PASSWORD", "")
    if pwd:
        safe_db_url = safe_db_url.replace(pwd, "***")
    print(f"[DB] Using: {safe_db_url}")

    # PRAGMA для SQLite
    db_url_str = str(db_url_obj)
    if db_url_str.startswith("sqlite"):
        with engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA journal_mode=WAL;")
            conn.exec_driver_sql("PRAGMA synchronous=NORMAL;")
            conn.exec_driver_sql("PRAGMA temp_store=MEMORY;")
            conn.exec_driver_sql("PRAGMA cache_size=-20000;")  # ~20MB кэша

    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    if old_engine is not None:
        old_engine.dispose()

    inspector = inspect(engine)
    with engine.begin() as conn:
        columns = {col["name"] for col in inspector.get_columns("tastings")}
        if "seq_no" not in columns:
            conn.exec_driver_sql(
                "ALTER TABLE tastings ADD COLUMN seq_no INTEGER NOT NULL DEFAULT 0"
            )

    # Бэкофилл seq_no для старых записей
    with SessionLocal() as s:
        needs = (
            s.execute(
                select(Tasting.user_id).where(Tasting.seq_no == 0).distinct()
            )
            .scalars()
            .all()
        )
        if needs:
            for uid in needs:
                rows = (
                    s.execute(
                        select(Tasting)
                        .where(Tasting.user_id == uid)
                        .order_by(Tasting.id.asc())
                    )
                    .scalars()
                    .all()
                )
                for idx, tasting in enumerate(rows, start=1):
                    tasting.seq_no = idx
            s.commit()

    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_tastings_user_seq_no ON tastings (user_id, seq_no)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_tastings_user_id_desc ON tastings (user_id, id DESC)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_tastings_user_category ON tastings (user_id, category)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_tastings_user_year ON tastings (user_id, year)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_tastings_user_rating ON tastings (user_id, rating)"
        )


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


def resolve_tasting(uid: int, identifier: str) -> Optional[Tasting]:
    token = (identifier or "").strip()
    if not token:
        return None
    with SessionLocal() as s:
        if token.startswith("#"):
            seq_part = token[1:]
            if not seq_part.isdigit():
                return None
            seq_no = int(seq_part)
            return (
                s.execute(
                    select(Tasting).where(
                        Tasting.user_id == uid, Tasting.seq_no == seq_no
                    )
                )
                .scalars()
                .first()
            )
        if not token.isdigit():
            return None
        tasting = s.get(Tasting, int(token))
        if tasting and tasting.user_id == uid:
            return tasting
        return None


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

CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096
ALBUM_TIMEOUT = 2.0
ALBUM_BUFFER: Dict[Tuple[int, str], dict] = {}
MORE_THROTTLE: Dict[int, float] = {}
MORE_THROTTLE_INTERVAL = 1.0


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


def edit_fields_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    buttons = [
        ("Название", "name"),
        ("Год", "year"),
        ("Регион", "region"),
        ("Категория", "category"),
        ("Граммовка", "grams"),
        ("Температура", "temp_c"),
        ("Время", "tasted_at"),
        ("Посуда", "gear"),
        ("Аромат (сухой)", "aroma_dry"),
        ("Аромат (прогретый)", "aroma_warmed"),
        ("Ощущения", "effects"),
        ("Сценарии", "scenarios"),
        ("Оценка", "rating"),
        ("Заметка", "summary"),
        ("Отмена", "cancel"),
    ]
    for text, field in buttons:
        kb.button(text=text, callback_data=f"efld:{field}")
    kb.adjust(2, 2, 2, 2, 2, 2, 2, 1)
    return kb


def edit_category_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    for c in CATEGORIES:
        kb.button(text=c, callback_data=f"ecat:{c}")
    kb.button(text="Другое (ввести)", callback_data="ecat:__other__")
    kb.button(text="⬅️ Назад", callback_data="ecat:__back__")
    kb.adjust(2, 2, 2, 2, 2)
    return kb


def edit_rating_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    for value in range(0, 11):
        kb.button(text=str(value), callback_data=f"erat:{value}")
    kb.adjust(6, 5)
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
    return f"#{t.seq_no} [{t.category}] {t.name}"


def build_card_text(
    t: Tasting,
    infusions: List[dict],
    photo_count: Optional[int] = None,
) -> str:
    lines = [f"#{t.seq_no} {t.title}"]
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


def split_text_for_telegram(text: str, limit: int = MESSAGE_LIMIT) -> List[str]:
    if len(text) <= limit:
        return [text]

    parts: List[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
            current = ""
        if len(paragraph) <= limit:
            current = paragraph
            continue
        for i in range(0, len(paragraph), limit):
            parts.append(paragraph[i : i + limit])
    if current:
        parts.append(current)
    if not parts:
        return [text[:limit]]
    # ensure each chunk is within limit by splitting on newlines if needed
    final_parts: List[str] = []
    for chunk in parts:
        if len(chunk) <= limit:
            final_parts.append(chunk)
            continue
        buf = ""
        for line in chunk.split("\n"):
            line = line.strip()
            if not line:
                addition = ""
            else:
                addition = (buf + "\n" + line) if buf else line
            if addition and len(addition) > limit:
                if buf:
                    final_parts.append(buf)
                for i in range(0, len(line), limit):
                    final_parts.append(line[i : i + limit])
                buf = ""
            else:
                buf = addition
        if buf:
            final_parts.append(buf)
    return final_parts or [text[:limit]]


FIELD_LABELS = {
    "name": "Название",
    "year": "Год",
    "region": "Регион",
    "category": "Категория",
    "grams": "Граммовка",
    "temp_c": "Температура",
    "tasted_at": "Время",
    "gear": "Посуда",
    "aroma_dry": "Аромат (сухой)",
    "aroma_warmed": "Аромат (прогретый)",
    "effects": "Ощущения",
    "scenarios": "Сценарии",
    "rating": "Оценка",
    "summary": "Заметка",
}


EDIT_TEXT_FIELDS = {
    "name": {
        "prompt": "Пришли новое название.",
        "allow_clear": False,
        "column": "name",
    },
    "year": {
        "prompt": "Пришли год (4 цифры) или «-» чтобы очистить.",
        "allow_clear": True,
        "column": "year",
    },
    "region": {
        "prompt": "Пришли регион или «-» чтобы очистить.",
        "allow_clear": True,
        "column": "region",
    },
    "grams": {
        "prompt": "Пришли граммовку (число) или «-».",
        "allow_clear": True,
        "column": "grams",
    },
    "temp_c": {
        "prompt": "Пришли температуру (°C) или «-».",
        "allow_clear": True,
        "column": "temp_c",
    },
    "tasted_at": {
        "prompt": "Пришли время в формате HH:MM или «-».",
        "allow_clear": True,
        "column": "tasted_at",
    },
    "gear": {
        "prompt": "Пришли посуду или «-».",
        "allow_clear": True,
        "column": "gear",
    },
    "aroma_dry": {
        "prompt": "Пришли аромат сухого листа или «-».",
        "allow_clear": True,
        "column": "aroma_dry",
    },
    "aroma_warmed": {
        "prompt": "Пришли аромат прогретого/промытого листа или «-».",
        "allow_clear": True,
        "column": "aroma_warmed",
    },
    "effects": {
        "prompt": "Пришли ощущения через запятую или «-».",
        "allow_clear": True,
        "column": "effects_csv",
    },
    "scenarios": {
        "prompt": "Пришли сценарии через запятую или «-».",
        "allow_clear": True,
        "column": "scenarios_csv",
    },
    "summary": {
        "prompt": "Пришли заметку или «-».",
        "allow_clear": True,
        "column": "summary",
    },
}


def edit_menu_text(seq_no: int) -> str:
    return f"Редактирование #{seq_no}. Выбери поле."


def normalize_csv_text(raw: str) -> str:
    parts = [piece.strip() for piece in raw.split(",")]
    filtered = [p for p in parts if p]
    return ", ".join(filtered)


async def send_card_with_media(
    target_message: Message,
    tasting_id: int,
    text_card: str,
    photos: List[str],
    reply_markup=None,
) -> None:
    bot = target_message.bot
    chat_id = target_message.chat.id
    photos = photos[:MAX_PHOTOS]
    markup_sent = False

    async def send_text_chunks(text: str) -> None:
        nonlocal markup_sent
        if not text:
            return
        chunks = split_text_for_telegram(text, MESSAGE_LIMIT)
        for idx, chunk in enumerate(chunks):
            await bot.send_message(
                chat_id,
                chunk,
                reply_markup=(reply_markup if not markup_sent and reply_markup and idx == 0 else None),
            )
            if reply_markup and not markup_sent and idx == 0:
                markup_sent = True

    async def ensure_actions_message() -> None:
        nonlocal markup_sent
        if reply_markup and not markup_sent:
            await bot.send_message(
                chat_id,
                "Действия:",
                reply_markup=reply_markup,
            )
            markup_sent = True

    try:
        if photos:
            use_caption = len(text_card) <= CAPTION_LIMIT and bool(text_card)
            media: List[InputMediaPhoto] = []
            for idx, fid in enumerate(photos):
                if idx == 0 and use_caption:
                    media.append(InputMediaPhoto(media=fid, caption=text_card))
                else:
                    media.append(InputMediaPhoto(media=fid))
            await bot.send_media_group(chat_id, media)
            if use_caption:
                await ensure_actions_message()
            else:
                await send_text_chunks(text_card)
                await ensure_actions_message()
        else:
            await send_text_chunks(text_card)
            await ensure_actions_message()
    except Exception:
        logging.exception("Failed to send media group for tasting %s", tasting_id)
        await send_text_chunks(text_card)
        await ensure_actions_message()
        for fid in photos:
            try:
                await bot.send_photo(chat_id, fid)
            except Exception:
                logging.exception(
                    "Fallback photo send failed for tasting %s", tasting_id
                )


async def _process_album_entry(entry: dict) -> None:
    state: Optional[FSMContext] = entry.get("state")
    message: Optional[Message] = entry.get("message")
    file_ids: List[str] = entry.get("file_ids", [])
    if not state or not message or not file_ids:
        return
    try:
        data = await state.get_data()
    except Exception:
        return
    photos: List[str] = data.get("new_photos", []) or []
    capacity = MAX_PHOTOS - len(photos)
    accepted: List[str] = file_ids[: capacity if capacity > 0 else 0]
    extra = len(file_ids) - len(accepted)
    if accepted:
        photos.extend(accepted)
        await state.update_data(new_photos=photos)
    if capacity <= 0:
        await message.answer(
            f"Можно добавить максимум {MAX_PHOTOS} фото, лишние я не сохранил."
        )
        await message.answer(
            f"Добавлено {len(photos)}/{MAX_PHOTOS}. Отправьте ещё или нажмите «Дальше»."
        )
        return
    if not accepted:
        await message.answer(
            f"Можно добавить максимум {MAX_PHOTOS} фото, лишние я не сохранил."
        )
        await message.answer(
            f"Добавлено {len(photos)}/{MAX_PHOTOS}. Отправьте ещё или нажмите «Дальше»."
        )
        return
    if extra > 0:
        await message.answer(
            f"Из-за лимита {MAX_PHOTOS} фото сохранил только часть альбома."
        )
    await message.answer(
        f"Добавлено {len(photos)}/{MAX_PHOTOS}. Отправьте ещё или нажмите «Дальше»."
    )


async def _album_timeout_handler(key: Tuple[int, str]) -> None:
    try:
        await asyncio.sleep(ALBUM_TIMEOUT)
    except asyncio.CancelledError:
        return
    entry = ALBUM_BUFFER.pop(key, None)
    if not entry:
        return
    await _process_album_entry(entry)


async def flush_user_albums(
    uid: Optional[int], state: FSMContext, process: bool = True
) -> None:
    if uid is None:
        return
    keys = [key for key in list(ALBUM_BUFFER.keys()) if key[0] == uid]
    for key in keys:
        entry = ALBUM_BUFFER.pop(key, None)
        if not entry:
            continue
        task: Optional[asyncio.Task] = entry.get("task")
        if task and not task.done():
            task.cancel()
        if not process:
            continue
        entry["state"] = state
        await _process_album_entry(entry)
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
    await flush_user_albums(data.get("user_id"), state)
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
    new_photos: List[str] = (data.get("new_photos", []) or [])[:MAX_PHOTOS]

    with SessionLocal() as s:
        max_seq = (
            s.execute(
                select(func.max(Tasting.seq_no)).where(Tasting.user_id == t.user_id)
            ).scalar()
            or 0
        )
        t.seq_no = max_seq + 1
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
    await send_card_with_media(
        target_message,
        t.id,
        text_card,
        new_photos,
        reply_markup=card_actions_kb(t.id).as_markup(),
    )


# ---------------- ФОТО ПОСЛЕ ЗАМЕТКИ ----------------

async def prompt_photos(target: Union[Message, CallbackQuery], state: FSMContext):
    await flush_user_albums(
        getattr(target.from_user, "id", None) if hasattr(target, "from_user") else None,
        state,
        process=False,
    )
    await state.update_data(new_photos=[])
    txt = (
        f"📷 Добавьте фото (до {MAX_PHOTOS}). Добавлено 0/{MAX_PHOTOS}. "
        "Отправьте ещё или нажмите «Дальше»."
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
    if len(photos) >= MAX_PHOTOS:
        await message.answer(
            f"Можно добавить максимум {MAX_PHOTOS} фото. Нажми «Дальше» или «Пропустить»."
        )
        return

    uid = data.get("user_id") or message.from_user.id
    media_group_id = message.media_group_id
    fid = message.photo[-1].file_id

    if media_group_id:
        key = (uid, media_group_id)
        entry = ALBUM_BUFFER.get(key)
        if not entry:
            entry = {"file_ids": [], "message": message, "state": state, "task": None}
            ALBUM_BUFFER[key] = entry
        entry["file_ids"].append(fid)
        entry["message"] = message
        entry["state"] = state
        task: Optional[asyncio.Task] = entry.get("task")
        if task and not task.done():
            task.cancel()
        entry["task"] = asyncio.create_task(_album_timeout_handler(key))
    else:
        photos.append(fid)
        await state.update_data(new_photos=photos)
        await message.answer(
            f"Добавлено {len(photos)}/{MAX_PHOTOS}. Отправьте ещё или нажмите «Дальше»."
        )


async def photos_done(call: CallbackQuery, state: FSMContext):
    await finalize_save(call.message, state)
    await call.answer()


async def photos_skip(call: CallbackQuery, state: FSMContext):
    await flush_user_albums(call.from_user.id, state, process=False)
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

    pics = pics[:MAX_PHOTOS]
    if len(pics) == 1:
        await call.message.answer_photo(pics[0])
    else:
        media = [InputMediaPhoto(media=fid) for fid in pics]
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
    """
    Обработка пользовательского текста после выбора 'Другое' в Характере послевкусия.
    Принимаем строку только если ранее было нажато 'Другое' (awaiting_custom_after=True).
    После сохранения сразу двигаем сценарий дальше.
    """
    data = await state.get_data()

    # Текст принимаем только после 'Другое'
    if not data.get("awaiting_custom_after"):
        await ui(
            message,
            "Выбери вариант из списка или нажми «Другое», чтобы ввести свой вариант."
        )
        return

    txt = (message.text or "").strip()
    if not txt:
        await ui(message, "Пусто. Введи характер послевкусия текстом или нажми «Сброс».")
        return

    # Сохраняем введённый текст и сбрасываем флаг ожидания кастомного ввода
    await state.update_data(cur_aftertaste=txt, awaiting_custom_after=False)

    # Переходим к следующему шагу (добавляем текущую инфузию и задаём следующий вопрос)
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


def encode_more_payload(uid: int, min_id: int, extra: str = "") -> str:
    encoded_extra = (
        base64.urlsafe_b64encode(extra.encode("utf-8")).decode("ascii").rstrip("=")
        if extra
        else ""
    )
    return f"{uid}|{min_id}|{encoded_extra}"


def decode_more_payload(payload: str) -> Tuple[int, int, str]:
    parts = payload.split("|", 2)
    if len(parts) < 2:
        raise ValueError
    uid = int(parts[0])
    min_id = int(parts[1])
    extra_enc = parts[2] if len(parts) > 2 else ""
    if extra_enc:
        padding = "=" * (-len(extra_enc) % 4)
        extra = base64.urlsafe_b64decode(extra_enc + padding).decode("utf-8")
    else:
        extra = ""
    return uid, min_id, extra


def apply_search_filters(stmt, kind: str, extra: str):
    extra_clean = (extra or "").strip()
    if kind == "last":
        return stmt
    if kind == "name":
        if not extra_clean:
            return None
        return stmt.where(Tasting.name.ilike(f"%{extra_clean}%"))
    if kind == "cat":
        if not extra_clean:
            return None
        return stmt.where(Tasting.category.ilike(extra_clean))
    if kind == "year":
        if not extra_clean.isdigit():
            return None
        return stmt.where(Tasting.year == int(extra_clean))
    if kind == "rating":
        try:
            thr = int(extra_clean)
        except Exception:
            return None
        return stmt.where(Tasting.rating >= thr)
    return None


def fetch_tastings_page(
    uid: int, kind: str, extra: str, min_id: Optional[int] = None
) -> Tuple[List[Tasting], bool]:
    with SessionLocal() as s:
        stmt = select(Tasting).where(Tasting.user_id == uid)
        stmt = apply_search_filters(stmt, kind, extra)
        if stmt is None:
            return [], False
        if min_id is not None:
            stmt = stmt.where(Tasting.id < min_id)
        stmt = stmt.order_by(Tasting.id.desc()).limit(PAGE_SIZE)
        rows = s.execute(stmt).scalars().all()
        if not rows:
            return [], False

        next_stmt = select(Tasting.id).where(Tasting.user_id == uid)
        next_stmt = apply_search_filters(next_stmt, kind, extra)
        if next_stmt is None:
            return rows, False
        next_stmt = next_stmt.where(Tasting.id < rows[-1].id)
        next_stmt = next_stmt.order_by(Tasting.id.desc()).limit(1)
        more = s.execute(next_stmt).scalars().first() is not None
        return rows, more


def more_allowed(uid: int) -> bool:
    now = time.monotonic()
    last = MORE_THROTTLE.get(uid, 0.0)
    if now - last < MORE_THROTTLE_INTERVAL:
        return False
    MORE_THROTTLE[uid] = now
    return True


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
    rows, has_more = fetch_tastings_page(uid, "last", "")

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

    if has_more:
        payload = encode_more_payload(uid, rows[-1].id)
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
    rows, has_more = fetch_tastings_page(uid, "last", "")

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

    if has_more:
        payload = encode_more_payload(uid, rows[-1].id)
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
        uid_payload, cursor, extra = decode_more_payload(payload)
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

    if not more_allowed(call.from_user.id):
        await call.answer("Слишком часто. Подожди секунду.")
        return

    rows, has_more = fetch_tastings_page(call.from_user.id, "last", extra, min_id=cursor)

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

    if has_more:
        payload2 = encode_more_payload(call.from_user.id, rows[-1].id, extra)
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
    rows, has_more = fetch_tastings_page(uid, "name", q)

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

    if has_more:
        await message.answer(
            "Показать ещё:",
            reply_markup=more_btn_kb(
                "name", encode_more_payload(uid, rows[-1].id, q)
            ).as_markup(),
        )

    await message.answer(
        "Ещё варианты:", reply_markup=search_menu_kb().as_markup()
    )


async def more_name(call: CallbackQuery):
    _, _, payload = call.data.split(":", 2)
    try:
        uid_payload, cursor, extra = decode_more_payload(payload)
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

    if not more_allowed(call.from_user.id):
        await call.answer("Слишком часто. Подожди секунду.")
        return

    rows, has_more = fetch_tastings_page(
        call.from_user.id, "name", extra, min_id=cursor
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

    if has_more:
        await call.message.answer(
            "Показать ещё:",
            reply_markup=more_btn_kb(
                "name",
                encode_more_payload(call.from_user.id, rows[-1].id, extra),
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
        await call.answer()
        return

    rows, has_more = fetch_tastings_page(uid, "cat", val)

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

    if has_more:
        await call.message.answer(
            "Показать ещё:",
            reply_markup=more_btn_kb(
                "cat", encode_more_payload(uid, rows[-1].id, val)
            ).as_markup(),
        )
    await call.answer()


async def s_cat_text(message: Message, state: FSMContext):
    q = (message.text or "").strip()
    uid = message.from_user.id

    rows, has_more = fetch_tastings_page(uid, "cat", q)

    if not rows:
        await message.answer("Ничего не нашёл.", reply_markup=search_menu_kb().as_markup())
        return

    await message.answer(f"Найдено по категории «{q}»:")
    for t in rows:
        await message.answer(short_row(t), reply_markup=open_btn_kb(t.id).as_markup())

    if has_more:
        await message.answer(
            "Показать ещё:",
            reply_markup=more_btn_kb(
                "cat", encode_more_payload(uid, rows[-1].id, q)
            ).as_markup(),
        )


async def more_cat(call: CallbackQuery):
    _, _, payload = call.data.split(":", 2)
    try:
        uid_payload, cursor, extra = decode_more_payload(payload)
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

    if not more_allowed(call.from_user.id):
        await call.answer("Слишком часто. Подожди секунду.")
        return

    rows, has_more = fetch_tastings_page(
        call.from_user.id, "cat", extra, min_id=cursor
    )

    try:
        await call.message.edit_reply_markup()
    except TelegramBadRequest:
        pass

    if not rows:
        await call.message.answer(
            "Больше результатов нет.", reply_markup=search_menu_kb().as_markup()
        )
        await call.answer()
        return

    for t in rows:
        await call.message.answer(short_row(t), reply_markup=open_btn_kb(t.id).as_markup())

    if has_more:
        await call.message.answer(
            "Показать ещё:",
            reply_markup=more_btn_kb(
                "cat", encode_more_payload(call.from_user.id, rows[-1].id, extra)
            ).as_markup(),
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
    rows, has_more = fetch_tastings_page(uid, "year", str(year))
    await state.clear()

    if not rows:
        await message.answer("Ничего не нашёл.", reply_markup=search_menu_kb().as_markup())
        return

    await message.answer(f"Найдено за {year}:")
    for t in rows:
        await message.answer(short_row(t), reply_markup=open_btn_kb(t.id).as_markup())

    if has_more:
        await message.answer(
            "Показать ещё:",
            reply_markup=more_btn_kb(
                "year", encode_more_payload(uid, rows[-1].id, str(year))
            ).as_markup(),
        )


async def more_year(call: CallbackQuery):
    _, _, payload = call.data.split(":", 2)
    try:
        uid_payload, cursor, extra = decode_more_payload(payload)
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

    if not more_allowed(call.from_user.id):
        await call.answer("Слишком часто. Подожди секунду.")
        return

    rows, has_more = fetch_tastings_page(
        call.from_user.id, "year", extra, min_id=cursor
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

    if has_more:
        await call.message.answer(
            "Показать ещё:",
            reply_markup=more_btn_kb(
                "year",
                encode_more_payload(call.from_user.id, rows[-1].id, extra),
            ).as_markup(),
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
    rows, has_more = fetch_tastings_page(uid, "rating", str(thr))

    if not rows:
        await call.message.answer("Ничего не нашёл.", reply_markup=search_menu_kb().as_markup())
        await call.answer()
        return

    await call.message.answer(f"Найдено с оценкой ≥ {thr}:")
    for t in rows:
        await call.message.answer(short_row(t), reply_markup=open_btn_kb(t.id).as_markup())

    if has_more:
        await call.message.answer(
            "Показать ещё:",
            reply_markup=more_btn_kb(
                "rating", encode_more_payload(uid, rows[-1].id, str(thr))
            ).as_markup(),
        )
    await call.answer()


async def more_rating(call: CallbackQuery):
    _, _, payload = call.data.split(":", 2)
    try:
        uid_payload, cursor, extra = decode_more_payload(payload)
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

    if not more_allowed(call.from_user.id):
        await call.answer("Слишком часто. Подожди секунду.")
        return

    rows, has_more = fetch_tastings_page(
        call.from_user.id, "rating", extra, min_id=cursor
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

    if has_more:
        await call.message.answer(
            "Показать ещё:",
            reply_markup=more_btn_kb(
                "rating", encode_more_payload(call.from_user.id, rows[-1].id, extra)
            ).as_markup(),
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
        photo_ids = (
            s.execute(
                select(Photo.file_id)
                .where(Photo.tasting_id == tid)
                .order_by(Photo.id.asc())
                .limit(MAX_PHOTOS)
            )
            .scalars()
            .all()
        )

    card_text = build_card_text(
        t, infusions_data, photo_count=photo_count or 0
    )
    await send_card_with_media(
        call.message,
        t.id,
        card_text,
        photo_ids,
        reply_markup=card_actions_kb(t.id).as_markup(),
    )
    await call.answer()


def edit_context_home_markup() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("⬅️ В меню", callback_data="nav:home"))
    return kb


async def notify_edit_context_lost(event: Union[CallbackQuery, Message], state: FSMContext):
    data = await state.get_data()
    if data.get("edit_ctx_warned"):
        return
    await ui(
        event,
        "Контекст редактирования потерян.",
        reply_markup=edit_context_home_markup(),
    )
    await state.update_data(edit_ctx_warned=True)


async def ensure_edit_context(event: Union[CallbackQuery, Message], state: FSMContext):
    """
    Проверяет валидность контекста редактирования.
    Возвращает dict с { 'tid': int, 'field': Optional[str], 'seq_no': Optional[int] } если валиден.
    Если контекст потерян — показывает сообщение с кнопкой '⬅️ В меню' (однократно) и возвращает None.
    """
    data = await state.get_data()
    current_state = await state.get_state()
    editing_states = {EditFlow.choosing.state, EditFlow.waiting_text.state}

    tid = data.get("edit_t_id")
    field = data.get("edit_field")
    seq_no = data.get("edit_seq_no")

    if not tid or seq_no is None:
        if current_state in editing_states:
            logger.warning(
                "Edit context missing (state=%s, tid=%s, seq=%s)",
                current_state,
                tid,
                seq_no,
            )
            await notify_edit_context_lost(event, state)
            return None
        if data.get("edit_ctx_warned"):
            await state.update_data(edit_ctx_warned=False)
        return {"tid": tid, "field": field, "seq_no": seq_no}

    if isinstance(event, CallbackQuery):
        uid = event.from_user.id
    elif isinstance(event, Message):
        uid = event.from_user.id
    else:
        uid = getattr(getattr(event, "from_user", None), "id", None)
        if uid is None and hasattr(event, "message"):
            uid = getattr(event.message.from_user, "id", None)

    if uid is None:
        logger.warning("Unable to determine user for edit context check (tid=%s)", tid)
        await notify_edit_context_lost(event, state)
        return None

    try:
        with SessionLocal() as s:
            t = s.get(Tasting, tid)
            if not t or t.user_id != uid:
                logger.warning("Edit context invalid owner (tid=%s, uid=%s)", tid, uid)
                await notify_edit_context_lost(event, state)
                return None
    except Exception:
        logger.exception("Failed to verify edit context (tid=%s)", tid)
        await notify_edit_context_lost(event, state)
        return None

    if data.get("edit_ctx_warned"):
        await state.update_data(edit_ctx_warned=False)

    return {"tid": tid, "field": field, "seq_no": seq_no}


def prepare_text_edit(field: str, raw: str) -> Tuple[Optional[Union[str, int, float]], Optional[str], Optional[str]]:
    cfg = EDIT_TEXT_FIELDS[field]
    text = (raw or "").strip()
    if not text:
        return None, cfg["prompt"], None

    if text == "-":
        if cfg["allow_clear"]:
            return None, None, cfg["column"]
        return None, cfg["prompt"], None

    if field == "name":
        if text == "-":
            return None, cfg["prompt"], None
        return text, None, cfg["column"]
    if field == "year":
        if len(text) == 4 and text.isdigit():
            return int(text), None, cfg["column"]
        return None, "Год должен состоять из 4 цифр. " + cfg["prompt"], None
    if field == "grams":
        try:
            value = float(text.replace(",", "."))
        except ValueError:
            return None, "Не удалось распознать число. " + cfg["prompt"], None
        return value, None, cfg["column"]
    if field == "temp_c":
        try:
            value = int(text)
        except ValueError:
            return None, "Используй целое число. " + cfg["prompt"], None
        return value, None, cfg["column"]
    if field == "tasted_at":
        try:
            datetime.datetime.strptime(text, "%H:%M")
        except ValueError:
            return None, "Время должно быть в формате HH:MM. " + cfg["prompt"], None
        return text, None, cfg["column"]
    if field in {"effects", "scenarios"}:
        normalized = normalize_csv_text(text)
        if not normalized:
            return None, cfg["prompt"], None
        return normalized, None, cfg["column"]
    # остальные текстовые поля — просто сохраняем строку
    return text, None, cfg["column"]


def update_tasting_fields(tid: int, uid: int, **updates) -> bool:
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


async def send_edit_menu(target: Union[CallbackQuery, Message], seq_no: int):
    markup = edit_fields_kb().as_markup()
    text = edit_menu_text(seq_no)
    if isinstance(target, CallbackQuery):
        await target.message.answer(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


async def edit_cb(call: CallbackQuery, state: FSMContext):
    ctx = await ensure_edit_context(call, state)
    if ctx is None:
        await call.answer()
        return

    try:
        _, sid = call.data.split(":", 1)
        tid = int(sid)
    except Exception:
        await call.answer()
        return

    try:
        with SessionLocal() as s:
            t = s.get(Tasting, tid)
            if not t or t.user_id != call.from_user.id:
                await call.message.answer("Нет доступа к этой записи.")
                await call.answer()
                return
            seq_no = t.seq_no

        await state.clear()
        await state.set_state(EditFlow.choosing)
        await state.update_data(
            edit_t_id=tid,
            edit_seq_no=seq_no,
            edit_field=None,
            awaiting_category_text=False,
            edit_ctx_warned=False,
        )
        await send_edit_menu(call, seq_no)
        await call.answer()
    except Exception:
        logger.exception("edit flow failed")
        await notify_edit_context_lost(call, state)
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
        f"Удалить #{t.seq_no}?",
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
    await call.message.answer(f"Удалил #{t.seq_no}.")
    await call.answer()


async def del_no_cb(call: CallbackQuery):
    await call.message.answer("Ок, не удаляю.")
    await call.answer()


async def edit_field_select(call: CallbackQuery, state: FSMContext):
    ctx = await ensure_edit_context(call, state)
    if ctx is None:
        await call.answer()
        return

    try:
        _, field = call.data.split(":", 1)
    except ValueError:
        await call.answer()
        return

    tid = ctx.get("tid")
    seq_no = ctx.get("seq_no")
    if not tid or seq_no is None:
        await notify_edit_context_lost(call, state)
        await call.answer()
        return

    try:
        if field == "cancel":
            await call.message.answer("Редактирование отменено.")
            await state.clear()
            await show_main_menu(call.message.bot, call.from_user.id)
            await call.answer()
            return

        if field == "category":
            await state.update_data(
                edit_field="category",
                awaiting_category_text=False,
                edit_ctx_warned=False,
            )
            await call.message.answer(
                "Выбери категорию:", reply_markup=edit_category_kb().as_markup()
            )
            await call.answer()
            return

        if field == "rating":
            await state.update_data(edit_field="rating", edit_ctx_warned=False)
            await call.message.answer(
                "Выбери оценку:", reply_markup=edit_rating_kb().as_markup()
            )
            await call.answer()
            return

        if field not in EDIT_TEXT_FIELDS:
            await call.answer()
            return

        cfg = EDIT_TEXT_FIELDS[field]
        await state.update_data(
            edit_field=field,
            awaiting_category_text=False,
            edit_ctx_warned=False,
        )
        await state.set_state(EditFlow.waiting_text)
        await call.message.answer(cfg["prompt"])
        await call.answer()
    except Exception:
        logger.exception("edit flow failed")
        await notify_edit_context_lost(call, state)
        await call.answer()


async def edit_category_pick(call: CallbackQuery, state: FSMContext):
    ctx = await ensure_edit_context(call, state)
    if ctx is None:
        await call.answer()
        return

    try:
        _, raw = call.data.split(":", 1)
    except ValueError:
        await call.answer()
        return

    tid = ctx.get("tid")
    seq_no = ctx.get("seq_no")
    if not tid or seq_no is None:
        await notify_edit_context_lost(call, state)
        await call.answer()
        return

    try:
        if raw == "__back__":
            await state.set_state(EditFlow.choosing)
            await state.update_data(
                edit_field=None,
                awaiting_category_text=False,
                edit_ctx_warned=False,
            )
            await send_edit_menu(call, seq_no)
            await call.answer()
            return

        if raw == "__other__":
            await state.update_data(
                edit_field="category",
                awaiting_category_text=True,
                edit_ctx_warned=False,
            )
            await state.set_state(EditFlow.waiting_text)
            await call.message.answer("Пришли категорию текстом.")
            await call.answer()
            return

        if raw not in CATEGORIES:
            await call.answer()
            return

        if len(raw) > 60:
            await call.message.answer("Категория слишком длинная.")
            await call.answer()
            return

        ok = update_tasting_fields(tid, call.from_user.id, category=raw)
        if not ok:
            logger.warning("Failed to update category for tasting %s", tid)
            await notify_edit_context_lost(call, state)
            await call.answer()
            return

        await state.set_state(EditFlow.choosing)
        await state.update_data(
            edit_field=None,
            awaiting_category_text=False,
            edit_ctx_warned=False,
        )
        await call.message.answer(f"Обновил {FIELD_LABELS['category']}.")
        await send_edit_menu(call, seq_no)
        await call.answer()
    except Exception:
        logger.exception("edit flow failed")
        await notify_edit_context_lost(call, state)
        await call.answer()


async def edit_rating_pick(call: CallbackQuery, state: FSMContext):
    ctx = await ensure_edit_context(call, state)
    if ctx is None:
        await call.answer()
        return

    try:
        _, raw = call.data.split(":", 1)
        rating = int(raw)
    except Exception:
        await call.answer()
        return

    if rating < 0 or rating > 10:
        await call.answer()
        return

    tid = ctx.get("tid")
    seq_no = ctx.get("seq_no")
    if not tid or seq_no is None:
        await notify_edit_context_lost(call, state)
        await call.answer()
        return

    try:
        ok = update_tasting_fields(tid, call.from_user.id, rating=rating)
        if not ok:
            logger.warning("Failed to update rating for tasting %s", tid)
            await notify_edit_context_lost(call, state)
            await call.answer()
            return

        await state.set_state(EditFlow.choosing)
        await state.update_data(
            edit_field=None,
            awaiting_category_text=False,
            edit_ctx_warned=False,
        )
        await call.message.answer(f"Обновил {FIELD_LABELS['rating']}.")
        await send_edit_menu(call, seq_no)
        await call.answer()
    except Exception:
        logger.exception("edit flow failed")
        await notify_edit_context_lost(call, state)
        await call.answer()


async def edit_flow_msg(message: Message, state: FSMContext):
    ctx = await ensure_edit_context(message, state)
    if ctx is None:
        return

    data = await state.get_data()
    tid = ctx.get("tid")
    seq_no = ctx.get("seq_no")
    field = data.get("edit_field")
    awaiting_category = data.get("awaiting_category_text")

    if not tid or seq_no is None or not field:
        await notify_edit_context_lost(message, state)
        return

    try:
        if field == "category" and awaiting_category:
            txt = (message.text or "").strip()
            if not txt or txt == "-":
                await message.answer(
                    "Категория не может быть пустой. Пришли категорию текстом."
                )
                return
            if len(txt) > 60:
                await message.answer(
                    "Категория слишком длинная. Пришли категорию текстом покороче."
                )
                return
            ok = update_tasting_fields(tid, message.from_user.id, category=txt)
            if not ok:
                logger.warning("Failed to update category text for tasting %s", tid)
                await notify_edit_context_lost(message, state)
                return
            await state.set_state(EditFlow.choosing)
            await state.update_data(
                edit_field=None,
                awaiting_category_text=False,
                edit_ctx_warned=False,
            )
            await message.answer(f"Обновил {FIELD_LABELS['category']}.")
            await send_edit_menu(message, seq_no)
            return

        if field not in EDIT_TEXT_FIELDS:
            await notify_edit_context_lost(message, state)
            return

        value, error, column = prepare_text_edit(field, message.text or "")
        if error:
            await message.answer(error)
            return

        updates = {column: value}
        ok = update_tasting_fields(tid, message.from_user.id, **updates)
        if not ok:
            logger.warning("Failed to update field %s for tasting %s", field, tid)
            await notify_edit_context_lost(message, state)
            return

        await state.set_state(EditFlow.choosing)
        await state.update_data(
            edit_field=None,
            awaiting_category_text=False,
            edit_ctx_warned=False,
        )
        await message.answer(f"Обновил {FIELD_LABELS[field]}.")
        await send_edit_menu(message, seq_no)
    except Exception:
        logger.exception("edit flow failed")
        await notify_edit_context_lost(message, state)


async def edit_cmd(message: Message, state: FSMContext):
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /edit <id или #номер>")
        return
    target = resolve_tasting(message.from_user.id, parts[1])
    if not target:
        await message.answer("Запись не найдена.")
        return
    await state.clear()
    await state.set_state(EditFlow.choosing)
    await state.update_data(
        edit_t_id=target.id,
        edit_seq_no=target.seq_no,
        edit_field=None,
        awaiting_category_text=False,
        edit_ctx_warned=False,
    )
    await send_edit_menu(message, target.seq_no)


async def delete_cmd(message: Message):
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /delete <id или #номер>")
        return
    target = resolve_tasting(message.from_user.id, parts[1])
    if not target:
        await message.answer("Запись не найдена.")
        return
    await message.answer(
        f"Удалить #{target.seq_no}?",
        reply_markup=confirm_del_kb(target.id).as_markup(),
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
    text = (
        "/start — меню\n"
        "/new — новая дегустация\n"
        "/find — поиск (по названию, категории, году, рейтингу, последние 5)\n"
        "/last — последние 5\n"
        "/tz — часовой пояс\n"
        "/menu — включить кнопки под вводом (сквозное меню)\n"
        "/hide — скрыть кнопки\n"
        "/reset — сброс и возврат в меню\n"
        "/cancel — сброс текущего действия\n"
        "/edit <id или #N> — редактировать запись\n"
        "/delete <id или #N> — удалить запись"
    )
    if message.from_user and is_admin(message.from_user.id):
        text += (
            "\n/backup — бэкап БД (admin)\n"
            "/restore — восстановление БД (admin, ответом на документ)\n"
            "/dbinfo — информация об окружении и БД (admin)"
        )
    await message.answer(text)


async def cancel_cmd(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Ок, сбросил. Возвращаю в меню.",
        reply_markup=main_kb().as_markup(),
    )


async def reset_cmd(message: Message, state: FSMContext):
    await cancel_cmd(message, state)


async def menu_cmd(message: Message):
    await message.answer(
        "Включил кнопки под полем ввода.",
        reply_markup=reply_main_kb(),
    )


async def hide_cmd(message: Message):
    await message.answer("Скрываю кнопки.", reply_markup=ReplyKeyboardRemove())


async def backup_cmd(message: Message, state: FSMContext):
    logger.info("Entered /backup")
    if not await ensure_admin_message(message):
        return
    if not cfg or not cfg.db_path:
        await message.answer("Для бэкапа нужна SQLite-база данных.")
        return
    if not os.path.exists(cfg.db_path):
        await message.answer("Файл базы данных не найден.")
        return

    file_name = os.path.basename(cfg.db_path)
    await message.answer_document(
        FSInputFile(cfg.db_path, filename=file_name),
        caption=f"Бэкап {file_name}",
    )


async def restore_cmd(message: Message, state: FSMContext):
    logger.info("Entered /restore")
    if not await ensure_admin_message(message):
        return
    if not cfg or not cfg.db_path:
        await message.answer("Для восстановления нужна SQLite-база данных.")
        return

    reply = message.reply_to_message
    document = reply.document if reply else None
    if not document:
        await message.answer(
            "Пришлите /restore ответом на документ с файлом базы данных."
        )
        return

    os.makedirs(os.path.dirname(cfg.db_path) or ".", exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix="restore_", suffix=".db")
    os.close(fd)
    try:
        await message.bot.download(document, destination=tmp_path)
    except Exception as exc:
        logger.exception("Restore download failed: %s", exc)
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        await message.answer("Не удалось скачать файл для восстановления.")
        return

    backup_path = cfg.db_path + ".bak"
    try:
        if os.path.exists(cfg.db_path):
            shutil.copy2(cfg.db_path, backup_path)
        shutil.move(tmp_path, cfg.db_path)
        try:
            setup_db(cfg.db_url)
        except Exception as exc:
            logger.exception("Restore reinitialization failed: %s", exc)
            if os.path.exists(backup_path):
                shutil.move(backup_path, cfg.db_path)
            else:
                os.remove(cfg.db_path)
            try:
                setup_db(cfg.db_url)
            except Exception:
                logger.exception("Failed to restore previous database after reinit error")
            await message.answer("Не удалось применить файл восстановления.")
            return
    except Exception as exc:
        logger.exception("Restore failed: %s", exc)
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        await message.answer("Не удалось применить файл восстановления.")
        return
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    await message.answer("✅ Восстановлено.")


async def dbinfo_cmd(message: Message, state: FSMContext):
    logger.info("Entered /dbinfo")
    if not await ensure_admin_message(message):
        return
    await message.answer(environment_summary())


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
    text = (
        "/start — меню\n"
        "/new — новая дегустация\n"
        "/find — поиск (по названию, категории, году, рейтингу, последние 5)\n"
        "/last — последние 5\n"
        "/tz — часовой пояс\n"
        "/menu — включить кнопки под вводом (сквозное меню)\n"
        "/hide — скрыть кнопки\n"
        "/reset — сброс и возврат в меню\n"
        "/cancel — сброс текущего действия\n"
        "/edit <id или #N> — редактировать запись\n"
        "/delete <id или #N> — удалить запись"
    )
    if call.from_user and is_admin(call.from_user.id):
        text += (
            "\n/backup — бэкап БД (admin)\n"
            "/restore — восстановление БД (admin, ответом на документ)\n"
            "/dbinfo — информация об окружении и БД (admin)"
        )
    await call.message.answer(
        text,
        reply_markup=search_menu_kb().as_markup(),
    )
    await call.answer()


async def back_main(call: CallbackQuery):
    await show_main_menu(call.message.bot, call.message.chat.id)
    await call.answer()


async def nav_home(call: CallbackQuery, state: FSMContext):
    await state.update_data(edit_t_id=None, edit_field=None, edit_ctx_warned=False)
    await state.clear()
    await show_main_menu(call.message.bot, call.from_user.id)
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
    dp.message.register(help_cmd, Command("help"))
    dp.message.register(backup_cmd, Command("backup"))
    dp.message.register(restore_cmd, Command("restore"))
    dp.message.register(dbinfo_cmd, Command("dbinfo"))
    dp.message.register(cancel_cmd, Command("cancel"))
    dp.message.register(reset_cmd, Command("reset"))
    dp.message.register(menu_cmd, Command("menu"))
    dp.message.register(hide_cmd, Command("hide"))
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

    # редактирование записи
    dp.message.register(edit_flow_msg, EditFlow.waiting_text)

    # reply-кнопки в самом конце!
    dp.message.register(reply_buttons_router)

    # callbacks
    dp.callback_query.register(new_cb, F.data == "new")
    dp.callback_query.register(find_cb, F.data == "find")
    dp.callback_query.register(help_cb, F.data == "help")
    dp.callback_query.register(back_main, F.data == "back:main")
    dp.callback_query.register(nav_home, F.data == "nav:home")

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

    # редактирование tasting
    dp.callback_query.register(edit_field_select, F.data.startswith("efld:"))
    dp.callback_query.register(edit_category_pick, F.data.startswith("ecat:"))
    dp.callback_query.register(edit_rating_pick, F.data.startswith("erat:"))
    dp.callback_query.register(edit_cb, F.data.startswith("edit:"))

    # карточка
    dp.callback_query.register(open_card, F.data.startswith("open:"))
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
        BotCommand(command="reset", description="Сброс и меню"),
        BotCommand(command="help", description="Помощь"),
    ]
    if cfg and cfg.admin_ids:
        commands.extend(
            [
                BotCommand(command="backup", description="Бэкап базы (admin)"),
                BotCommand(command="restore", description="Восстановление БД (admin)"),
                BotCommand(command="dbinfo", description="Инфо о БД (admin)"),
            ]
        )
    await bot.set_my_commands(commands)


# ---------------- MAIN ----------------

async def main():
    global cfg
    cfg = get_settings()
    logger.info(environment_summary())
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