import asyncio
import logging
import os
import datetime
import uuid
import textwrap
from dataclasses import dataclass
from typing import Optional, List, Dict, Union

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery, BotCommand,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, FSInputFile
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest

from sqlalchemy import (
    create_engine, Integer, String, DateTime, ForeignKey, select, func
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from aiogram.types import InputMediaPhoto

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
        banner_path=banner if banner and os.path.exists(banner) else None
    )

cfg: Settings  # присвоим в main()

# ---------------- БД ----------------

class Base(DeclarativeBase):
    pass

class Tasting(Base):
    __tablename__ = "tastings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.now, nullable=False)

    name: Mapped[str] = mapped_column(String(200))
    year: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    region: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    category: Mapped[str] = mapped_column(String(60))

    grams: Mapped[Optional[float]] = mapped_column(nullable=True)
    temp_c: Mapped[Optional[int]] = mapped_column(nullable=True)
    tasted_at: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)  # "HH:MM"
    gear: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    aroma_dry: Mapped[Optional[str]] = mapped_column(nullable=True)
    aroma_warmed: Mapped[Optional[str]] = mapped_column(nullable=True)
    aroma_after: Mapped[Optional[str]] = mapped_column(nullable=True)

    effects_csv: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)     # «Ощущения»
    scenarios_csv: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)   # «Сценарии»

    rating: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[Optional[str]] = mapped_column(nullable=True)  # «Заметка»

    infusions: Mapped[List["Infusion"]] = relationship(back_populates="tasting", cascade="all, delete-orphan")
    photos: Mapped[List["Photo"]] = relationship(cascade="all, delete-orphan")


    @property
    def title(self) -> str:
        parts: List[str] = [f"[{self.category}]", self.name]
        extra: List[str] = []
        if self.year: extra.append(str(self.year))
        if self.region: extra.append(self.region)
        if extra: parts.append("(" + ", ".join(extra) + ")")
        return " ".join(parts)

class Infusion(Base):
    __tablename__ = "infusions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tasting_id: Mapped[int] = mapped_column(ForeignKey("tastings.id", ondelete="CASCADE"))
    n: Mapped[int] = mapped_column(Integer)

    seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    liquor_color: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    taste: Mapped[Optional[str]] = mapped_column(nullable=True)
    special_notes: Mapped[Optional[str]] = mapped_column(nullable=True)
    body: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    aftertaste: Mapped[Optional[str]] = mapped_column(nullable=True)

    tasting: Mapped[Tasting] = relationship(back_populates="infusions")

class Photo(Base):
    __tablename__ = "photos"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tasting_id: Mapped[int] = mapped_column(ForeignKey("tastings.id", ondelete="CASCADE"))
    file_id: Mapped[str] = mapped_column(String(255))


SessionLocal = None  # фабрика сессий

def setup_db(db_url: str):
    global SessionLocal
    engine = create_engine(db_url, echo=False, future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

# ---------------- КОНСТАНТЫ UI ----------------

CATEGORIES = ["Зелёный", "Белый", "Красный", "Улун", "Шу Пуэр", "Шен Пуэр", "Хэй Ча", "Другое"]
BODY_PRESETS = ["тонкое", "лёгкое", "среднее", "плотное", "маслянистое"]

# Новый набор «Ощущения»
EFFECTS = ["Тепло", "Охлаждение", "Расслабление", "Фокус", "Бодрость", "Тонус", "Спокойствие", "Сонливость"]

# Новый набор «Сценарии»
SCENARIOS = ["Отдых", "Работа/учеба", "Творчество", "Медитация", "Общение", "Прогулка"]

# Общие дескрипторы для аромата/вкуса
DESCRIPTORS = [
    "сухофрукты", "мёд", "хлебные", "цветы", "орех",
    "древесный", "дымный", "ягоды", "фрукты",
    "травянистый", "овощные", "пряный", "землистый"
]

# Новый набор для послевкусия
AFTERTASTE_SET = [
    "сладкий", "фруктовый", "ягодный", "цветочный", "цитрусовый",
    "кондитерский", "хлебный", "древесный", "пряный", "горький",
    "минеральный", "овощной", "землистый"
]

PAGE_SIZE = 5

# ---------------- КЛАВИАТУРЫ ----------------

def main_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Новая дегустация", callback_data="new")
    kb.button(text="🔎 Найти записи", callback_data="find")
    kb.button(text="ℹ️ О боте", callback_data="about")
    kb.adjust(1, 1, 1)
    return kb

def reply_main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📝 Новая дегустация"), KeyboardButton(text="🔎 Найти записи")],
            [KeyboardButton(text="🕔 Последние 5"), KeyboardButton(text="ℹ️ О боте")],
            [KeyboardButton(text="Отмена")]
        ],
        resize_keyboard=True,
        input_field_placeholder="Выбери действие"
    )

def category_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    for c in CATEGORIES:
        kb.button(text=c, callback_data=f"cat:{c}")
    kb.adjust(2)
    return kb

def skip_kb(tag: str) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="Пропустить", callback_data=f"skip:{tag}")
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

def toggle_list_kb(source: List[str], selected: List[str], prefix: str, done_text="Готово", include_other=False) -> InlineKeyboardBuilder:
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

def search_menu_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="По названию", callback_data="s_name")
    kb.button(text="По категории", callback_data="s_cat")
    kb.button(text="По году", callback_data="s_year")
    kb.button(text="Последние 5", callback_data="s_last")
    kb.button(text="Расширенный поиск", callback_data="s_adv")
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

def any_category_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="Любая", callback_data="advcat:any")
    for c in CATEGORIES:
        kb.button(text=c, callback_data=f"advcat:{c}")
    kb.adjust(3, 3)
    return kb

def sort_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="Сначала высокий рейтинг", callback_data="advs:rate")
    kb.button(text="Сначала новые", callback_data="advs:date")
    kb.adjust(1, 1)
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
    aroma_warmed = State()
    aroma_after = State()

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

class SearchFlow(StatesGroup):
    name = State()
    year = State()
    cat = State()

class EditFlow(StatesGroup):
    waiting_text = State()

class AdvSearch(StatesGroup):
    cat = State()
    year = State()
    text = State()
    min_rating = State()
    sort = State()

class PhotoFlow(StatesGroup):
    photos = State()


# ---------------- ХЭЛПЕРЫ UI ----------------

async def ui(target: Union[CallbackQuery, Message], text: str, reply_markup=None):
    """
    Универсальный вывод:
    - CallbackQuery: пробуем редактировать (caption у медиа или text), иначе шлём новое.
    - Message: просто отправляем новое сообщение.
    """
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
    return f"#{t.id} [{t.category}] {t.name}"

def build_card_text(t: Tasting, infusions: List[dict], photo_count: Optional[int] = None) -> str:
    lines = [f"{t.title}"]
    lines.append(f"⭐ Оценка: {t.rating}")
    if t.grams is not None: lines.append(f"⚖️ Граммовка: {t.grams} г")
    if t.temp_c is not None: lines.append(f"🌡️ Температура: {t.temp_c} °C")
    if t.tasted_at: lines.append(f"⏰ Время дегустации: {t.tasted_at}")
    if t.gear: lines.append(f"🍶 Посуда: {t.gear}")

    if t.aroma_dry or t.aroma_warmed or t.aroma_after:
        lines.append("🌬️ Ароматы:")
        if t.aroma_dry:     lines.append(f"  ▫️ сухой лист: {t.aroma_dry}")
        if t.aroma_warmed:  lines.append(f"  ▫️ прогретый лист: {t.aroma_warmed}")
        if t.aroma_after:   lines.append(f"  ▫️ после прогрева: {t.aroma_after}")

    if t.effects_csv:   lines.append(f"🧘 Ощущения: {t.effects_csv}")
    if t.scenarios_csv: lines.append(f"🎯 Сценарии: {t.scenarios_csv}")
    if t.summary:       lines.append(f"📝 Заметка: {t.summary}")

    if photo_count is not None and photo_count > 0:
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
        cur_seconds=None, cur_color=None, cur_taste=None,
        cur_special=None, cur_body=None, cur_aftertaste=None,
        cur_taste_sel=[], cur_aftertaste_sel=[],
        awaiting_custom_taste=False, awaiting_custom_after=False
    )
    text = "Добавить ещё пролив или завершаем?"
    kb = yesno_more_infusions_kb().as_markup()
    if isinstance(msg_or_call, Message):
        await msg_or_call.answer(text, reply_markup=kb)
    else:
        await ui(msg_or_call, text, reply_markup=kb)

async def save_and_reply(target_message: Message, state: FSMContext, summary_text: Optional[str]):
    data = await state.get_data()
    t = Tasting(
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
        summary=summary_text or None
    )
    infusions_data = data.get("infusions", [])
    with SessionLocal() as s:
        s.add(t); s.flush()
        for inf in infusions_data:
            s.add(Infusion(
                tasting_id=t.id, n=inf["n"],
                seconds=inf["seconds"], liquor_color=inf["liquor_color"],
                taste=inf["taste"], special_notes=inf["special_notes"],
                body=inf["body"], aftertaste=inf["aftertaste"],
            ))
        s.commit()

    await state.clear()
    text = build_card_text(t, infusions_data)
    await target_message.answer(text, reply_markup=main_kb().as_markup())

# ---------------- КОНТЕКСТ ПОИСКА ----------------

SEARCH_CTX: Dict[str, dict] = {}

def new_ctx(data: dict) -> str:
    token = uuid.uuid4().hex[:8]
    SEARCH_CTX[token] = data
    return token

def get_ctx(token: str) -> Optional[dict]:
    return SEARCH_CTX.get(token)

# ---------------- БАННЕР / START ----------------

async def show_main_menu_as_photo(bot: Bot, chat_id: int):
    caption = "Привет! Создать новую запись или найти уже созданную."
    if cfg.banner_path:
        await bot.send_photo(
            chat_id=chat_id,
            photo=FSInputFile(cfg.banner_path),
            caption=caption,
            reply_markup=main_kb().as_markup()
        )
    else:
        await bot.send_message(
            chat_id=chat_id,
            text=caption,
            reply_markup=main_kb().as_markup()
        )

# ---------------- ОБЩЕЕ ----------------

async def on_start(message: Message):
    await show_main_menu_as_photo(message.bot, message.chat.id)

async def help_cmd(message: Message):
    await message.answer(
        "/start — меню\n/new — новая дегустация\n/find — поиск\n/last — последние 5\n/menu — включить кнопки под вводом\n/hide — скрыть кнопки\n/cancel — сброс\n/edit <id> — редактировать\n/delete <id> — удалить"
    )

async def cancel_cmd(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Ок, сбросил. Возвращаю в меню.", reply_markup=main_kb().as_markup())

async def menu_cmd(message: Message):
    await message.answer("Включил кнопки под полем ввода.", reply_markup=reply_main_kb())

async def hide_cmd(message: Message):
    await message.answer("Скрываю кнопки.", reply_markup=ReplyKeyboardRemove())

async def reply_buttons_router(message: Message, state: FSMContext):
    t = (message.text or "").strip()
    if t.endswith("Новая дегустация") or t == "Новая дегустация" or t == "📝 Новая дегустация":
        await new_cmd(message, state)
    elif t.endswith("Найти записи") or t == "Найти записи" or t == "🔎 Найти записи":
        await find_cmd(message)
    elif "Последние 5" in t:
        await last_cmd(message)
    elif "О боте" in t:
        await message.answer(
            "Здесь можно создать новую запись или найти уже созданную.",
            reply_markup=main_kb().as_markup()
        )
    elif t == "Отмена":
        await cancel_cmd(message, state)

async def about_cb(call: CallbackQuery):
    await show_main_menu_as_photo(call.message.bot, call.message.chat.id)
    await call.answer()

async def back_main(call: CallbackQuery):
    await show_main_menu_as_photo(call.message.bot, call.message.chat.id)
    await call.answer()

# ---------------- НОВАЯ ЗАПИСЬ ----------------

async def new_cmd(message: Message, state: FSMContext):
    await start_new(state)
    await message.answer("🍵 Название чая?")

async def new_cb(call: CallbackQuery, state: FSMContext):
    await start_new(state)
    await ui(call, "🍵 Название чая?")
    await call.answer()

async def start_new(state: FSMContext):
    await state.clear()
    await state.update_data(
        infusions=[], effects=[], scenarios=[], infusion_n=1,
        aroma_dry_sel=[], aroma_warmed_sel=[], aroma_after_sel=[],
        cur_taste_sel=[], cur_aftertaste_sel=[]
    )
    await state.set_state(NewTasting.name)

async def name_in(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer("📅 Год сбора? Можно пропустить.", reply_markup=skip_kb("year").as_markup())
    await state.set_state(NewTasting.year)

async def year_skip(call: CallbackQuery, state: FSMContext):
    await state.update_data(year=None)
    await ui(call, "🗺️ Регион? Можно пропустить.", reply_markup=skip_kb("region").as_markup())
    await state.set_state(NewTasting.region)
    await call.answer()

async def year_in(message: Message, state: FSMContext):
    txt = message.text.strip()
    year = int(txt) if txt.isdigit() else None
    await state.update_data(year=year)
    await message.answer("🗺️ Регион? Можно пропустить.", reply_markup=skip_kb("region").as_markup())
    await state.set_state(NewTasting.region)

async def region_skip(call: CallbackQuery, state: FSMContext):
    await state.update_data(region=None)
    await ui(call, "🏷️ Категория?", reply_markup=category_kb().as_markup())
    await state.set_state(NewTasting.category)
    await call.answer()

async def region_in(message: Message, state: FSMContext):
    region = message.text.strip()
    await state.update_data(region=region if region else None)
    await message.answer("🏷️ Категория?", reply_markup=category_kb().as_markup())
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
    await state.update_data(category=message.text.strip(), awaiting_custom_cat=False)
    await ask_optional_grams_msg(message, state)

async def ask_optional_grams_edit(call: CallbackQuery, state: FSMContext):
    await ui(call, "⚖️ Граммовка? Можно пропустить.", reply_markup=skip_kb("grams").as_markup())
    await state.set_state(NewTasting.grams)

async def ask_optional_grams_msg(message: Message, state: FSMContext):
    await message.answer("⚖️ Граммовка? Можно пропустить.", reply_markup=skip_kb("grams").as_markup())
    await state.set_state(NewTasting.grams)

async def grams_skip(call: CallbackQuery, state: FSMContext):
    await state.update_data(grams=None)
    await ui(call, "🌡️ Температура, °C? Можно пропустить.", reply_markup=skip_kb("temp").as_markup())
    await state.set_state(NewTasting.temp_c)
    await call.answer()

async def grams_in(message: Message, state: FSMContext):
    txt = message.text.replace(",", ".").strip()
    try:
        grams = float(txt)
    except Exception:
        grams = None
    await state.update_data(grams=grams)
    await message.answer("🌡️ Температура, °C? Можно пропустить.", reply_markup=skip_kb("temp").as_markup())
    await state.set_state(NewTasting.temp_c)

async def temp_skip(call: CallbackQuery, state: FSMContext):
    await state.update_data(temp_c=None)
    now_hm = datetime.datetime.now().strftime("%H:%M")
    await ui(call, f"⏰ Время дегустации? Сейчас {now_hm}. Введи HH:MM, нажми «Текущее время» или пропусти.",
             reply_markup=time_kb().as_markup())
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
    now_hm = datetime.datetime.now().strftime("%H:%M")
    await message.answer(
        f"⏰ Время дегустации? Сейчас {now_hm}. Введи HH:MM, нажми «Текущее время» или пропусти.",
        reply_markup=time_kb().as_markup()
    )
    await state.set_state(NewTasting.tasted_at)

async def time_now(call: CallbackQuery, state: FSMContext):
    now_hm = datetime.datetime.now().strftime("%H:%M")
    await state.update_data(tasted_at=now_hm)
    await ui(call, "🍶 Посудa дегустации? Можно пропустить.", reply_markup=skip_kb("gear").as_markup())
    await state.set_state(NewTasting.gear)
    await call.answer()

async def tasted_at_skip(call: CallbackQuery, state: FSMContext):
    await state.update_data(tasted_at=None)
    await ui(call, "🍶 Посудa дегустации? Можно пропустить.", reply_markup=skip_kb("gear").as_markup())
    await state.set_state(NewTasting.gear)
    await call.answer()

async def tasted_at_in(message: Message, state: FSMContext):
    text = message.text.strip()
    ta = text[:5] if ":" in text else None
    await state.update_data(tasted_at=ta)
    await message.answer("🍶 Посудa дегустации? Можно пропустить.", reply_markup=skip_kb("gear").as_markup())
    await state.set_state(NewTasting.gear)

async def gear_skip(call: CallbackQuery, state: FSMContext):
    await state.update_data(gear=None)
    await ask_aroma_dry_call(call, state)
    await call.answer()

async def gear_in(message: Message, state: FSMContext):
    await state.update_data(gear=message.text.strip())
    await ask_aroma_dry_msg(message, state)

# --- Ароматы: мультивыбор + «Другое»

async def ask_aroma_dry_msg(message: Message, state: FSMContext):
    await state.update_data(aroma_dry_sel=[])
    kb = toggle_list_kb(DESCRIPTORS, [], "ad", include_other=True)
    await message.answer("🌬️ Аромат сухого листа: выбери дескрипторы и нажми «Готово», или «Другое».", reply_markup=kb.as_markup())
    await state.set_state(NewTasting.aroma_dry)

async def ask_aroma_dry_call(call: CallbackQuery, state: FSMContext):
    await state.update_data(aroma_dry_sel=[])
    kb = toggle_list_kb(DESCRIPTORS, [], "ad", include_other=True)
    await ui(call, "🌬️ Аромат сухого листа: выбери дескрипторы и нажми «Готово», или «Другое».", reply_markup=kb.as_markup())
    await state.set_state(NewTasting.aroma_dry)

async def aroma_dry_toggle(call: CallbackQuery, state: FSMContext):
    _, tail = call.data.split(":", 1)
    data = await state.get_data()
    selected = data.get("aroma_dry_sel", [])
    if tail == "done":
        text = ", ".join(selected) if selected else None
        await state.update_data(aroma_dry=text)
        kb = toggle_list_kb(DESCRIPTORS, [], "aw", include_other=True)
        await ui(call, "🌬️ Аромат прогретого листа: выбери и нажми «Готово».", reply_markup=kb.as_markup())
        await state.set_state(NewTasting.aroma_warmed)
        await call.answer(); return
    if tail == "other":
        await state.update_data(awaiting_custom_ad=True)
        await ui(call, "Введи аромат сухого листа текстом:")
        await call.answer(); return
    idx = int(tail); item = DESCRIPTORS[idx]
    if item in selected: selected.remove(item)
    else: selected.append(item)
    await state.update_data(aroma_dry_sel=selected)
    kb = toggle_list_kb(DESCRIPTORS, selected, "ad", include_other=True)
    try:
        await call.message.edit_reply_markup(reply_markup=kb.as_markup())
    except TelegramBadRequest:
        pass
    await call.answer()

async def aroma_dry_custom(message: Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("awaiting_custom_ad"): return
    selected = data.get("aroma_dry_sel", [])
    if message.text.strip(): selected.append(message.text.strip())
    await state.update_data(aroma_dry=", ".join(selected) if selected else None, awaiting_custom_ad=False)
    kb = toggle_list_kb(DESCRIPTORS, [], "aw", include_other=True)
    await message.answer("🌬️ Аромат прогретого листа: выбери и нажми «Готово».", reply_markup=kb.as_markup())
    await state.set_state(NewTasting.aroma_warmed)

async def aroma_warmed_toggle(call: CallbackQuery, state: FSMContext):
    _, tail = call.data.split(":", 1)
    data = await state.get_data()
    selected = data.get("aroma_warmed_sel", [])
    if tail == "done":
        text = ", ".join(selected) if selected else None
        await state.update_data(aroma_warmed=text)
        kb = toggle_list_kb(DESCRIPTORS, [], "aa", include_other=True)
        await ui(call, "🌬️ Аромат после прогрева: выбери и нажми «Готово».", reply_markup=kb.as_markup())
        await state.set_state(NewTasting.aroma_after)
        await call.answer(); return
    if tail == "other":
        await state.update_data(awaiting_custom_aw=True)
        await ui(call, "Введи аромат прогретого листа текстом:")
        await call.answer(); return
    idx = int(tail); item = DESCRIPTORS[idx]
    if item in selected: selected.remove(item)
    else: selected.append(item)
    await state.update_data(aroma_warmed_sel=selected)
    kb = toggle_list_kb(DESCRIPTORS, selected, "aw", include_other=True)
    try:
        await call.message.edit_reply_markup(reply_markup=kb.as_markup())
    except TelegramBadRequest:
        pass
    await call.answer()

async def aroma_warmed_custom(message: Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("awaiting_custom_aw"): return
    selected = data.get("aroma_warmed_sel", [])
    if message.text.strip(): selected.append(message.text.strip())
    await state.update_data(aroma_warmed=", ".join(selected) if selected else None, awaiting_custom_aw=False)
    kb = toggle_list_kb(DESCRIPTORS, [], "aa", include_other=True)
    await message.answer("🌬️ Аромат после прогрева: выбери и нажми «Готово».", reply_markup=kb.as_markup())
    await state.set_state(NewTasting.aroma_after)

async def aroma_after_toggle(call: CallbackQuery, state: FSMContext):
    _, tail = call.data.split(":", 1)
    data = await state.get_data()
    selected = data.get("aroma_after_sel", [])
    if tail == "done":
        text = ", ".join(selected) if selected else None
        await state.update_data(aroma_after=text)
        await start_infusion_block_call(call, state)
        await call.answer(); return
    if tail == "other":
        await state.update_data(awaiting_custom_aa=True)
        await ui(call, "Введи аромат после прогрева текстом:")
        await call.answer(); return
    idx = int(tail)
    item = DESCRIPTORS[idx]
    if item in selected: selected.remove(item)
    else: selected.append(item)
    await state.update_data(aroma_after_sel=selected)
    kb = toggle_list_kb(DESCRIPTORS, selected, "aa", include_other=True)
    try:
        await call.message.edit_reply_markup(reply_markup=kb.as_markup())
    except TelegramBadRequest:
        pass
    await call.answer()

async def aroma_after_custom(message: Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("awaiting_custom_aa"): return
    selected = data.get("aroma_after_sel", [])
    if message.text.strip(): selected.append(message.text.strip())
    await state.update_data(aroma_after=", ".join(selected) if selected else None, awaiting_custom_aa=False)
    await start_infusion_block_msg(message, state)

# --- Проливы

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
    await message.answer("Цвет настоя пролива? Можно пропустить.", reply_markup=skip_kb("color").as_markup())
    await state.set_state(InfusionState.color)

async def color_skip(call: CallbackQuery, state: FSMContext):
    await state.update_data(cur_color=None)
    await state.update_data(cur_taste_sel=[])
    kb = toggle_list_kb(DESCRIPTORS, [], "taste", include_other=True)
    await ui(call, "Вкус настоя: выбери дескрипторы и нажми «Готово», или «Другое».", reply_markup=kb.as_markup())
    await state.set_state(InfusionState.taste)
    await call.answer()

async def inf_color(message: Message, state: FSMContext):
    await state.update_data(cur_color=message.text.strip())
    await state.update_data(cur_taste_sel=[])
    kb = toggle_list_kb(DESCRIPTORS, [], "taste", include_other=True)
    await message.answer("Вкус настоя: выбери дескрипторы и нажми «Готово», или «Другое».", reply_markup=kb.as_markup())
    await state.set_state(InfusionState.taste)

async def taste_toggle(call: CallbackQuery, state: FSMContext):
    _, tail = call.data.split(":", 1)
    data = await state.get_data()
    selected = data.get("cur_taste_sel", [])
    if tail == "done":
        text = ", ".join(selected) if selected else None
        await state.update_data(cur_taste=text, awaiting_custom_taste=False)
        await ui(call, "✨ Особенные ноты пролива? (можно пропустить)", reply_markup=skip_kb("special").as_markup())
        await state.set_state(InfusionState.special)
        await call.answer(); return
    if tail == "other":
        await state.update_data(awaiting_custom_taste=True)
        await ui(call, "Введи вкус текстом:")
        await call.answer(); return
    idx = int(tail); item = DESCRIPTORS[idx]
    if item in selected: selected.remove(item)
    else: selected.append(item)
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
        await message.answer("✨ Особенные ноты пролива? (можно пропустить)", reply_markup=skip_kb("special").as_markup())
        await state.set_state(InfusionState.special)
        return
    text = message.text.strip()
    await state.update_data(cur_taste=text or None, awaiting_custom_taste=False)
    await message.answer("✨ Особенные ноты пролива? (можно пропустить)", reply_markup=skip_kb("special").as_markup())
    await state.set_state(InfusionState.special)

async def inf_taste(message: Message, state: FSMContext):
    await state.update_data(cur_taste=message.text.strip() or None, awaiting_custom_taste=False)
    await message.answer("✨ Особенные ноты пролива? (можно пропустить)", reply_markup=skip_kb("special").as_markup())
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
        await call.answer(); return
    await state.update_data(cur_body=val)
    await state.update_data(cur_aftertaste_sel=[])
    kb = toggle_list_kb(AFTERTASTE_SET, [], "aft", include_other=True)
    await ui(call, "Характер послевкусия: выбери пункты и нажми «Готово», или «Другое».", reply_markup=kb.as_markup())
    await state.set_state(InfusionState.aftertaste)
    await call.answer()

async def inf_body_custom(message: Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("awaiting_custom_body"): return
    await state.update_data(cur_body=message.text.strip(), awaiting_custom_body=False)
    kb = toggle_list_kb(AFTERTASTE_SET, [], "aft", include_other=True)
    await message.answer("Характер послевкусия: выбери пункты и нажми «Готово», или «Другое».", reply_markup=kb.as_markup())
    await state.set_state(InfusionState.aftertaste)

async def aftertaste_toggle(call: CallbackQuery, state: FSMContext):
    _, tail = call.data.split(":", 1)
    data = await state.get_data()
    selected = data.get("cur_aftertaste_sel", [])
    if tail == "done":
        text = ", ".join(selected) if selected else None
        await state.update_data(cur_aftertaste=text, awaiting_custom_after=False)
        await append_current_infusion_and_prompt(call, state)
        await call.answer(); return
    if tail == "other":
        await state.update_data(awaiting_custom_after=True)
        await ui(call, "Введи характер послевкусия текстом:")
        await call.answer(); return
    idx = int(tail); item = AFTERTASTE_SET[idx]
    if item in selected: selected.remove(item)
    else: selected.append(item)
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
    await state.update_data(cur_aftertaste=message.text.strip() or None, awaiting_custom_after=False)
    await append_current_infusion_and_prompt(message, state)

async def more_infusions(call: CallbackQuery, state: FSMContext):
    await start_infusion_block_call(call, state)

async def finish_infusions(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = data.get("effects", [])
    kb = toggle_list_kb(EFFECTS, selected, prefix="eff", include_other=True)
    await ui(call, "Ощущения (мультивыбор). Жми пункты, затем «Готово», либо «Другое».", reply_markup=kb.as_markup())
    await state.set_state(EffectsScenarios.effects)
    await call.answer()

# --- Ощущения / Сценарии

async def eff_toggle_or_done(call: CallbackQuery, state: FSMContext):
    _, tail = call.data.split(":", 1)
    data = await state.get_data()
    selected = data.get("effects", [])
    if tail == "done":
        kb = toggle_list_kb(SCENARIOS, data.get("scenarios", []), prefix="scn", include_other=True)
        await ui(call, "Сценарии (мультивыбор). Жми пункты, затем «Готово», либо «Другое».", reply_markup=kb.as_markup())
        await state.set_state(EffectsScenarios.scenarios)
        await call.answer(); return
    if tail == "other":
        await state.update_data(awaiting_custom_eff=True)
        await ui(call, "Введи ощущение текстом:")
        await call.answer(); return
    idx = int(tail); item = EFFECTS[idx]
    if item in selected: selected.remove(item)
    else: selected.append(item)
    await state.update_data(effects=selected)
    kb = toggle_list_kb(EFFECTS, selected, prefix="eff", include_other=True)
    try:
        await call.message.edit_reply_markup(reply_markup=kb.as_markup())
    except TelegramBadRequest:
        pass
    await call.answer()

async def eff_custom(message: Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("awaiting_custom_eff"): return
    selected = data.get("effects", [])
    txt = message.text.strip()
    if txt: selected.append(txt)
    await state.update_data(effects=selected, awaiting_custom_eff=False)
    kb = toggle_list_kb(EFFECTS, selected, prefix="eff", include_other=True)
    await message.answer("Добавил. Можешь выбрать ещё и нажать «Готово».", reply_markup=kb.as_markup())
    await state.set_state(EffectsScenarios.effects)

async def scn_toggle_or_done(call: CallbackQuery, state: FSMContext):
    _, tail = call.data.split(":", 1)
    data = await state.get_data()
    selected = data.get("scenarios", [])
    if tail == "done":
        await ui(call, "Оценка сорта 0..10?", reply_markup=rating_kb().as_markup())
        await state.set_state(RatingSummary.rating)
        await call.answer(); return
    if tail == "other":
        await state.update_data(awaiting_custom_scn=True)
        await ui(call, "Введи сценарий текстом:")
        await call.answer(); return
    idx = int(tail); item = SCENARIOS[idx]
    if item in selected: selected.remove(item)
    else: selected.append(item)
    await state.update_data(scenarios=selected)
    kb = toggle_list_kb(SCENARIOS, selected, prefix="scn", include_other=True)
    try:
        await call.message.edit_reply_markup(reply_markup=kb.as_markup())
    except TelegramBadRequest:
        pass
    await call.answer()

async def scn_custom(message: Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("awaiting_custom_scn"): return
    selected = data.get("scenarios", [])
    txt = message.text.strip()
    if txt: selected.append(txt)
    await state.update_data(scenarios=selected, awaiting_custom_scn=False)
    kb = toggle_list_kb(SCENARIOS, selected, prefix="scn", include_other=True)
    await message.answer("Добавил. Можешь выбрать ещё и нажать «Готово».", reply_markup=kb.as_markup())
    await state.set_state(EffectsScenarios.scenarios)

# --- оценка и заметка

async def rate_pick(call: CallbackQuery, state: FSMContext):
    _, val = call.data.split(":", 1)
    await state.update_data(rating=int(val))
    await ui(call, "📝 Заметка по дегустации? (можно пропустить)", reply_markup=skip_kb("summary").as_markup())
    await state.set_state(RatingSummary.summary)
    await call.answer()

async def rating_in(message: Message, state: FSMContext):
    txt = message.text.strip()
    rating = int(txt) if txt.isdigit() else 0
    rating = max(0, min(10, rating))
    await state.update_data(rating=rating)
    await message.answer("📝 Заметка по дегустации? (можно пропустить)", reply_markup=skip_kb("summary").as_markup())
    await state.set_state(RatingSummary.summary)

async def summary_in(message: Message, state: FSMContext):
    # ЛОГ: увидим, что попали в правильный хэндлер
    logging.info("summary_in -> prompt_photos")
    await state.update_data(summary=message.text.strip())
    await prompt_photos(message, state)

async def summary_skip(call: CallbackQuery, state: FSMContext):
    logging.info("summary_skip -> prompt_photos")
    await state.update_data(summary=None)
    await prompt_photos(call, state)
    await call.answer()

async def prompt_photos(target: Union[Message, CallbackQuery], state: FSMContext):
    await state.update_data(new_photos=[])
    txt = "📷 Добавить фото? Пришли до 3 фото одним или несколькими сообщениями. Когда готов — нажми «Готово» или «Пропустить»."
    kb = photos_kb().as_markup()
    if isinstance(target, CallbackQuery):
        await ui(target, txt, reply_markup=kb)
    else:
        await target.answer(txt, reply_markup=kb)
    await state.set_state(PhotoFlow.photos)

async def finalize_save(target_message: Message, state: FSMContext):
    data = await state.get_data()
    t = Tasting(
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
        s.add(t); s.flush()
        for inf in infusions_data:
            s.add(Infusion(
                tasting_id=t.id, n=inf["n"],
                seconds=inf["seconds"], liquor_color=inf["liquor_color"],
                taste=inf["taste"], special_notes=inf["special_notes"],
                body=inf["body"], aftertaste=inf["aftertaste"],
            ))
        for fid in new_photos:
            s.add(Photo(tasting_id=t.id, file_id=fid))
        s.commit()
        s.refresh(t)
        _ = t.photos  # подгрузим список в объект

    await state.clear()

    text = build_card_text(t, infusions_data, photo_count=len(new_photos))

    # Если есть фото — пробуем вложить карточку в подпись
    if new_photos:
        if len(text) <= 1024 and len(new_photos) == 1:
            # 1 фото + подпись = карточка с кнопками в одном сообщении
            await target_message.answer_photo(
                new_photos[0],
                caption=text,
                reply_markup=card_actions_kb(t.id).as_markup()
            )
        elif len(text) <= 1024 and len(new_photos) > 1:
            # Альбом: caption только у первого элемента; у альбомов нет reply_markup,
            # поэтому кнопки отправим отдельным сообщением после альбома.
            media = [InputMediaPhoto(media=new_photos[0], caption=text)]
            media += [InputMediaPhoto(media=fid) for fid in new_photos[1:10]]
            await target_message.bot.send_media_group(target_message.chat.id, media)
            await target_message.answer("Действия:", reply_markup=card_actions_kb(t.id).as_markup())
        else:
            # Текст слишком длинный для подписи — отправим текст отдельно, затем фото
            await target_message.answer(text, reply_markup=card_actions_kb(t.id).as_markup())
            if len(new_photos) == 1:
                await target_message.answer_photo(new_photos[0])
            else:
                media = [InputMediaPhoto(media=fid) for fid in new_photos[:10]]
                await target_message.bot.send_media_group(target_message.chat.id, media)
    else:
        # Фото нет — обычная текстовая карточка
        await target_message.answer(text, reply_markup=card_actions_kb(t.id).as_markup())


async def photo_add(message: Message, state: FSMContext):
    data = await state.get_data()
    photos: List[str] = data.get("new_photos", []) or []
    if not message.photo:
        await message.answer("Пришли фото (или жми «Готово» / «Пропустить»).")
        return
    if len(photos) >= 3:
        await message.answer("Лимит 3 фото. Жми «Готово» или «Пропустить».")
        return
    fid = message.photo[-1].file_id
    photos.append(fid)
    await state.update_data(new_photos=photos)
    await message.answer(f"Фото сохранено ({len(photos)}/3). Можешь прислать ещё или нажми «Готово».")

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
    except:
        await call.answer(); return

    with SessionLocal() as s:
        t = s.get(Tasting, tid)
        if not t:
            await ui(call, "Запись не найдена."); await call.answer(); return
        pics = [p.file_id for p in (t.photos or [])]

    if not pics:
        await ui(call, "Фото нет."); await call.answer(); return

    if len(pics) == 1:
        await call.message.answer_photo(pics[0])
    else:
        media = [InputMediaPhoto(media=fid) for fid in pics[:10]]
        await call.message.bot.send_media_group(call.message.chat.id, media)
    await call.answer()


# ---------------- ПОИСК + ПАГИНАЦИЯ ----------------

def has_more_last(min_id: int) -> bool:
    with SessionLocal() as s:
        x = s.execute(select(Tasting.id).where(Tasting.id < min_id).order_by(Tasting.id.desc()).limit(1)).scalars().first()
        return x is not None

async def find_cb(call: CallbackQuery):
    await ui(call, "Выбери способ поиска:", reply_markup=search_menu_kb().as_markup())
    await call.answer()

async def find_cmd(message: Message):
    await message.answer("Выбери способ поиска:", reply_markup=search_menu_kb().as_markup())

async def s_last(call: CallbackQuery):
    with SessionLocal() as s:
        rows = s.execute(select(Tasting).order_by(Tasting.id.desc()).limit(PAGE_SIZE)).scalars().all()

    if not rows:
        await ui(call, "Пока пусто.", reply_markup=search_menu_kb().as_markup())
        await call.answer(); return

    await ui(call, "Последние записи:")
    for t in rows:
        await call.message.answer(short_row(t), reply_markup=open_btn_kb(t.id).as_markup())

    min_id = rows[-1].id
    if has_more_last(min_id):
        await call.message.answer("Показать ещё:", reply_markup=more_btn_kb("last", str(min_id)).as_markup())
    await call.message.answer("Ещё варианты:", reply_markup=search_menu_kb().as_markup())
    await call.answer()

async def last_cmd(message: Message):
    with SessionLocal() as s:
        rows = s.execute(select(Tasting).order_by(Tasting.id.desc()).limit(PAGE_SIZE)).scalars().all()
    if not rows:
        await message.answer("Пока пусто.", reply_markup=search_menu_kb().as_markup()); return

    await message.answer("Последние записи:")
    for t in rows:
        await message.answer(short_row(t), reply_markup=open_btn_kb(t.id).as_markup())
    min_id = rows[-1].id
    if has_more_last(min_id):
        await message.answer("Показать ещё:", reply_markup=more_btn_kb("last", str(min_id)).as_markup())
    await message.answer("Ещё варианты:", reply_markup=search_menu_kb().as_markup())

async def more_last(call: CallbackQuery):
    _, _, payload = call.data.split(":", 2)
    try:
        cursor = int(payload)
    except:
        await call.answer(); return
    with SessionLocal() as s:
        rows = s.execute(
            select(Tasting).where(Tasting.id < cursor).order_by(Tasting.id.desc()).limit(PAGE_SIZE)
        ).scalars().all()

    try:
        await call.message.edit_reply_markup()
    except TelegramBadRequest:
        pass

    if not rows:
        await call.message.answer("Больше записей нет.", reply_markup=search_menu_kb().as_markup())
        await call.answer(); return

    for t in rows:
        await call.message.answer(short_row(t), reply_markup=open_btn_kb(t.id).as_markup())
    min_id = rows[-1].id
    if has_more_last(min_id):
        await call.message.answer("Показать ещё:", reply_markup=more_btn_kb("last", str(min_id)).as_markup())
    await call.answer()

# --- По названию

async def s_name(call: CallbackQuery, state: FSMContext):
    await ui(call, "Введи часть названия чая:")
    await state.set_state(SearchFlow.name)
    await call.answer()

async def s_name_run(message: Message, state: FSMContext):
    q = message.text.strip()
    token = new_ctx({"type": "name", "q": q})
    with SessionLocal() as s:
        rows = s.execute(
            select(Tasting)
            .where(func.lower(Tasting.name).like(f"%{q.lower()}%"))
            .order_by(Tasting.id.desc())
            .limit(PAGE_SIZE)
        ).scalars().all()
    await state.clear()

    if not rows:
        await message.answer("Ничего не нашёл.", reply_markup=search_menu_kb().as_markup()); return

    await message.answer("Найдено:")
    for t in rows:
        await message.answer(short_row(t), reply_markup=open_btn_kb(t.id).as_markup())
    min_id = rows[-1].id
    with SessionLocal() as s:
        more = s.execute(
            select(Tasting.id)
            .where(func.lower(Tasting.name).like(f"%{q.lower()}%"), Tasting.id < min_id)
            .order_by(Tasting.id.desc()).limit(1)
        ).scalars().first() is not None
    if more:
        await message.answer("Показать ещё:", reply_markup=more_btn_kb("name", f"{token}:{min_id}").as_markup())
    await message.answer("Ещё варианты:", reply_markup=search_menu_kb().as_markup())

async def more_name(call: CallbackQuery):
    _, _, payload = call.data.split(":", 2)  # name: token:min_id
    try:
        token, sid = payload.split(":", 1)
        cursor = int(sid)
    except:
        await call.answer(); return
    ctx = get_ctx(token)
    if not ctx or ctx.get("type") != "name":
        try:
            await call.message.edit_reply_markup()
        except TelegramBadRequest:
            pass
        await call.message.answer("Контекст поиска устарел. Запусти поиск заново.", reply_markup=search_menu_kb().as_markup())
        await call.answer(); return
    q = ctx["q"]
    with SessionLocal() as s:
        rows = s.execute(
            select(Tasting)
            .where(func.lower(Tasting.name).like(f"%{q.lower()}%"), Tasting.id < cursor)
            .order_by(Tasting.id.desc()).limit(PAGE_SIZE)
        ).scalars().all()

    try:
        await call.message.edit_reply_markup()
    except TelegramBadRequest:
        pass

    if not rows:
        await call.message.answer("Больше результатов нет.", reply_markup=search_menu_kb().as_markup())
        await call.answer(); return

    for t in rows:
        await call.message.answer(short_row(t), reply_markup=open_btn_kb(t.id).as_markup())
    min_id = rows[-1].id
    with SessionLocal() as s:
        more = s.execute(
            select(Tasting.id)
            .where(func.lower(Tasting.name).like(f"%{q.lower()}%"), Tasting.id < min_id)
            .order_by(Tasting.id.desc()).limit(1)
        ).scalars().first() is not None
    if more:
        await call.message.answer("Показать ещё:", reply_markup=more_btn_kb("name", f"{token}:{min_id}").as_markup())
    await call.answer()

# --- По категории

async def s_cat(call: CallbackQuery, state: FSMContext):
    await ui(call, "Выбери категорию:", reply_markup=category_kb().as_markup())
    await state.set_state(SearchFlow.cat)
    await call.answer()

async def s_cat_run(call: CallbackQuery, state: FSMContext):
    if not call.data.startswith("cat:"):
        await call.answer(); return
    _, cat = call.data.split(":", 1)
    token = new_ctx({"type": "cat", "cat": cat})
    with SessionLocal() as s:
        rows = s.execute(
            select(Tasting).where(Tasting.category == cat).order_by(Tasting.id.desc()).limit(PAGE_SIZE)
        ).scalars().all()
    await state.clear()

    if not rows:
        await ui(call, "Пусто по этой категории.", reply_markup=search_menu_kb().as_markup())
        await call.answer(); return

    await ui(call, f"Категория: {cat}")
    for t in rows:
        await call.message.answer(short_row(t), reply_markup=open_btn_kb(t.id).as_markup())
    min_id = rows[-1].id
    with SessionLocal() as s:
        more = s.execute(
            select(Tasting.id).where(Tasting.category == cat, Tasting.id < min_id)
            .order_by(Tasting.id.desc()).limit(1)
        ).scalars().first() is not None
    if more:
        await call.message.answer("Показать ещё:", reply_markup=more_btn_kb("cat", f"{token}:{min_id}").as_markup())
    await call.message.answer("Ещё варианты:", reply_markup=search_menu_kb().as_markup())
    await call.answer()

async def more_cat(call: CallbackQuery):
    _, _, payload = call.data.split(":", 2)  # cat: token:min_id
    try:
        token, sid = payload.split(":", 1)
        cursor = int(sid)
    except:
        await call.answer(); return
    ctx = get_ctx(token)
    if not ctx or ctx.get("type") != "cat":
        try:
            await call.message.edit_reply_markup()
        except TelegramBadRequest:
            pass
        await call.message.answer("Контекст поиска устарел. Запусти поиск заново.", reply_markup=search_menu_kb().as_markup())
        await call.answer(); return
    cat = ctx["cat"]
    with SessionLocal() as s:
        rows = s.execute(
            select(Tasting).where(Tasting.category == cat, Tasting.id < cursor)
            .order_by(Tasting.id.desc()).limit(PAGE_SIZE)
        ).scalars().all()

    try:
        await call.message.edit_reply_markup()
    except TelegramBadRequest:
        pass

    if not rows:
        await call.message.answer("Больше результатов нет.", reply_markup=search_menu_kb().as_markup())
        await call.answer(); return

    for t in rows:
        await call.message.answer(short_row(t), reply_markup=open_btn_kb(t.id).as_markup())
    min_id = rows[-1].id
    with SessionLocal() as s:
        more = s.execute(
            select(Tasting.id).where(Tasting.category == cat, Tasting.id < min_id)
            .order_by(Tasting.id.desc()).limit(1)
        ).scalars().first() is not None
    if more:
        await call.message.answer("Показать ещё:", reply_markup=more_btn_kb("cat", f"{token}:{min_id}").as_markup())
    await call.answer()

# --- По году

async def s_year(call: CallbackQuery, state: FSMContext):
    await ui(call, "Введи год (например 2024):")
    await state.set_state(SearchFlow.year)
    await call.answer()

async def s_year_run(message: Message, state: FSMContext):
    year = int(message.text.strip()) if message.text.strip().isdigit() else None
    token = new_ctx({"type": "year", "year": year})
    with SessionLocal() as s:
        rows = s.execute(
            select(Tasting).where(Tasting.year == year).order_by(Tasting.id.desc()).limit(PAGE_SIZE)
        ).scalars().all()
    await state.clear()

    if not rows:
        await message.answer("Пусто по этому году.", reply_markup=search_menu_kb().as_markup()); return

    await message.answer(f"Год: {year}")
    for t in rows:
        await message.answer(short_row(t), reply_markup=open_btn_kb(t.id).as_markup())
    min_id = rows[-1].id
    with SessionLocal() as s:
        more = s.execute(
            select(Tasting.id).where(Tasting.year == year, Tasting.id < min_id)
            .order_by(Tasting.id.desc()).limit(1)
        ).scalars().first() is not None
    if more:
        await message.answer("Показать ещё:", reply_markup=more_btn_kb("year", f"{token}:{min_id}").as_markup())
    await message.answer("Ещё варианты:", reply_markup=search_menu_kb().as_markup())

async def more_year(call: CallbackQuery):
    _, _, payload = call.data.split(":", 2)  # year: token:min_id
    try:
        token, sid = payload.split(":", 1)
        cursor = int(sid)
    except:
        await call.answer(); return
    ctx = get_ctx(token)
    if not ctx or ctx.get("type") != "year":
        try:
            await call.message.edit_reply_markup()
        except TelegramBadRequest:
            pass
        await call.message.answer("Контекст поиска устарел. Запусти поиск заново.", reply_markup=search_menu_kb().as_markup())
        await call.answer(); return
    year = ctx["year"]
    with SessionLocal() as s:
        rows = s.execute(
            select(Tasting).where(Tasting.year == year, Tasting.id < cursor)
            .order_by(Tasting.id.desc()).limit(PAGE_SIZE)
        ).scalars().all()

    try:
        await call.message.edit_reply_markup()
    except TelegramBadRequest:
        pass

    if not rows:
        await call.message.answer("Больше результатов нет.", reply_markup=search_menu_kb().as_markup())
        await call.answer(); return

    for t in rows:
        await call.message.answer(short_row(t), reply_markup=open_btn_kb(t.id).as_markup())
    min_id = rows[-1].id
    with SessionLocal() as s:
        more = s.execute(
            select(Tasting.id).where(Tasting.year == year, Tasting.id < min_id)
            .order_by(Tasting.id.desc()).limit(1)
        ).scalars().first() is not None
    if more:
        await call.message.answer("Показать ещё:", reply_markup=more_btn_kb("year", f"{token}:{min_id}").as_markup())
    await call.answer()

# --- Расширенный поиск

def build_where_from_adv(d: dict):
    conds = []
    if d.get("adv_cat") is not None:
        conds.append(Tasting.category == d["adv_cat"])
    if d.get("adv_year") is not None:
        conds.append(Tasting.year == d["adv_year"])
    if d.get("adv_text"):
        conds.append(func.lower(Tasting.name).like(f"%{d['adv_text'].lower()}%"))
    if d.get("adv_minr") is not None:
        conds.append(Tasting.rating >= d["adv_minr"])
    return conds

async def s_adv(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await ui(call, "Категория?", reply_markup=any_category_kb().as_markup())
    await state.set_state(AdvSearch.cat)
    await call.answer()

async def adv_cat_pick(call: CallbackQuery, state: FSMContext):
    _, val = call.data.split(":", 1)
    cat = None if val == "any" else val
    await state.update_data(adv_cat=cat)
    await ui(call, "Год (или пропусти).", reply_markup=skip_kb("advy").as_markup())
    await state.set_state(AdvSearch.year)
    await call.answer()

async def adv_year_in(message: Message, state: FSMContext):
    y = int(message.text.strip()) if message.text.strip().isdigit() else None
    await state.update_data(adv_year=y)
    await message.answer("Часть названия (или пропусти).", reply_markup=skip_kb("advt").as_markup())
    await state.set_state(AdvSearch.text)

async def adv_year_skip(call: CallbackQuery, state: FSMContext):
    await state.update_data(adv_year=None)
    await ui(call, "Часть названия (или пропусти).", reply_markup=skip_kb("advt").as_markup())
    await state.set_state(AdvSearch.text)
    await call.answer()

async def adv_text_in(message: Message, state: FSMContext):
    await state.update_data(adv_text=message.text.strip())
    await message.answer("Минимальный рейтинг 0..10 (или пропусти).", reply_markup=skip_kb("advr").as_markup())
    await state.set_state(AdvSearch.min_rating)

async def adv_text_skip(call: CallbackQuery, state: FSMContext):
    await state.update_data(adv_text=None)
    await ui(call, "Минимальный рейтинг 0..10 (или пропусти).", reply_markup=skip_kb("advr").as_markup())
    await state.set_state(AdvSearch.min_rating)
    await call.answer()

async def adv_minr_in(message: Message, state: FSMContext):
    v = message.text.strip()
    try: r = max(0, min(10, int(v)))
    except: r = None
    await state.update_data(adv_minr=r)
    await message.answer("Сортировка:", reply_markup=sort_kb().as_markup())
    await state.set_state(AdvSearch.sort)

async def adv_minr_skip(call: CallbackQuery, state: FSMContext):
    await state.update_data(adv_minr=None)
    await ui(call, "Сортировка:", reply_markup=sort_kb().as_markup())
    await state.set_state(AdvSearch.sort)
    await call.answer()

async def adv_sort_pick(call: CallbackQuery, state: FSMContext):
    _, sortkey = call.data.split(":", 1)  # rate|date
    data = await state.get_data()
    data["adv_sort"] = sortkey
    token = new_ctx({"type": "adv", **data})
    await state.clear()

    order = Tasting.rating.desc() if sortkey == "rate" else Tasting.id.desc()
    conds = build_where_from_adv(data)

    with SessionLocal() as s:
        q = select(Tasting)
        if conds: q = q.where(*conds)
        rows = s.execute(q.order_by(order).limit(PAGE_SIZE)).scalars().all()

    if not rows:
        await ui(call, "Ничего не нашёл.", reply_markup=search_menu_kb().as_markup())
        await call.answer(); return

    await ui(call, "Результаты:")
    for t in rows:
        await call.message.answer(short_row(t), reply_markup=open_btn_kb(t.id).as_markup())

    min_id = rows[-1].id
    with SessionLocal() as s:
        q = select(Tasting.id).where(Tasting.id < min_id)
        if conds: q = q.where(*conds)
        more = s.execute(q.order_by(Tasting.id.desc()).limit(1)).scalars().first() is not None
    if more:
        await call.message.answer("Показать ещё:", reply_markup=more_btn_kb("adv", f"{token}:{min_id}").as_markup())
    await call.message.answer("Ещё варианты:", reply_markup=search_menu_kb().as_markup())
    await call.answer()

async def more_adv(call: CallbackQuery):
    _, _, payload = call.data.split(":", 2)  # adv: token:min_id
    try:
        token, sid = payload.split(":", 1)
        cursor = int(sid)
    except:
        await call.answer(); return
    ctx = get_ctx(token)
    if not ctx or ctx.get("type") != "adv":
        try:
            await call.message.edit_reply_markup()
        except TelegramBadRequest:
            pass
        await call.message.answer("Контекст поиска устарел. Запусти поиск заново.", reply_markup=search_menu_kb().as_markup())
        await call.answer(); return

    conds = build_where_from_adv(ctx)
    order = Tasting.rating.desc() if ctx.get("adv_sort") == "rate" else Tasting.id.desc()

    with SessionLocal() as s:
        q = select(Tasting).where(Tasting.id < cursor)
        if conds: q = q.where(*conds)
        rows = s.execute(q.order_by(order).limit(PAGE_SIZE)).scalars().all()

    try:
        await call.message.edit_reply_markup()
    except TelegramBadRequest:
        pass

    if not rows:
        await call.message.answer("Больше результатов нет.", reply_markup=search_menu_kb().as_markup())
        await call.answer(); return

    for t in rows:
        await call.message.answer(short_row(t), reply_markup=open_btn_kb(t.id).as_markup())

    min_id = rows[-1].id
    with SessionLocal() as s:
        q = select(Tasting.id).where(Tasting.id < min_id)
        if conds: q = q.where(*conds)
        more = s.execute(q.order_by(Tasting.id.desc()).limit(1)).scalars().first() is not None
    if more:
        await call.message.answer("Показать ещё:", reply_markup=more_btn_kb("adv", f"{token}:{min_id}").as_markup())
    await call.answer()

# ---------------- ОТКРЫТЬ / РЕДАКТ / УДАЛИТЬ ----------------

async def open_card(call: CallbackQuery):
    try:
        _, sid = call.data.split(":", 1)
        tid = int(sid)
    except Exception:
        await call.answer(); return

    with SessionLocal() as s:
        t: Optional[Tasting] = s.get(Tasting, tid)
        if not t:
            await ui(call, "Запись не найдена.", reply_markup=search_menu_kb().as_markup())
            await call.answer(); return
        infusions_data = [{
            "n": inf.n, "seconds": inf.seconds, "liquor_color": inf.liquor_color,
            "taste": inf.taste, "special_notes": inf.special_notes,
            "body": inf.body, "aftertaste": inf.aftertaste
        } for inf in t.infusions]
        pics_count = len(t.photos or [])
        pics = [p.file_id for p in (t.photos or [])]
        pics_count = len(pics)


        # Снимем клавиатуру у сообщения-списка (чтобы не путало)
    try:
        await call.message.edit_reply_markup()
    except TelegramBadRequest:
        pass

    text = build_card_text(t, infusions_data, photo_count=pics_count)

    if pics_count > 0:
        if len(text) <= 1024 and pics_count == 1:
            await call.message.answer_photo(
                pics[0],
                caption=text,
                reply_markup=card_actions_kb(t.id).as_markup()
            )
        elif len(text) <= 1024 and pics_count > 1:
            media = [InputMediaPhoto(media=pics[0], caption=text)]
            media += [InputMediaPhoto(media=fid) for fid in pics[1:10]]
            await call.message.bot.send_media_group(call.message.chat.id, media)
            await call.message.answer("Действия:", reply_markup=card_actions_kb(t.id).as_markup())
        else:
            await call.message.answer(text, reply_markup=card_actions_kb(t.id).as_markup())
            if pics_count == 1:
                await call.message.answer_photo(pics[0])
            else:
                media = [InputMediaPhoto(media=fid) for fid in pics[:10]]
                await call.message.bot.send_media_group(call.message.chat.id, media)
    else:
        await call.message.answer(text, reply_markup=card_actions_kb(t.id).as_markup())

    await call.answer()

def edit_fields_kb(t_id: int) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="Название", callback_data=f"editf:name:{t_id}")
    kb.button(text="Год", callback_data=f"editf:year:{t_id}")
    kb.button(text="Регион", callback_data=f"editf:region:{t_id}")
    kb.button(text="Категория", callback_data=f"editf:category:{t_id}")
    kb.button(text="Граммы", callback_data=f"editf:grams:{t_id}")
    kb.button(text="Температура", callback_data=f"editf:temp:{t_id}")
    kb.button(text="Время дегустации", callback_data=f"editf:tasted_at:{t_id}")
    kb.button(text="Посуда", callback_data=f"editf:gear:{t_id}")
    kb.button(text="Аромат (сух.)", callback_data=f"editf:aroma_dry:{t_id}")
    kb.button(text="Аромат (прогр.)", callback_data=f"editf:aroma_warmed:{t_id}")
    kb.button(text="Аромат (после)", callback_data=f"editf:aroma_after:{t_id}")
    kb.button(text="Ощущения", callback_data=f"editf:effects:{t_id}")
    kb.button(text="Сценарии", callback_data=f"editf:scenarios:{t_id}")
    kb.button(text="Рейтинг", callback_data=f"editf:rating:{t_id}")
    kb.button(text="Заметка", callback_data=f"editf:summary:{t_id}")
    kb.button(text="⬅️ Назад", callback_data=f"open:{t_id}")
    kb.adjust(2)
    return kb

async def edit_open(call: CallbackQuery):
    try:
        _, sid = call.data.split(":", 1)
        tid = int(sid)
    except:
        await call.answer(); return
    await ui(call, f"Что изменить в записи #{tid}?", reply_markup=edit_fields_kb(tid).as_markup())
    await call.answer()

async def edit_field_pick(call: CallbackQuery, state: FSMContext):
    try:
        _, field, sid = call.data.split(":", 2)
        tid = int(sid)
    except:
        await call.answer(); return

    await state.update_data(edit_tid=tid, edit_field=field)

    prompts = {
        "name": "Введите новое название:",
        "year": "Введите год (или оставьте пусто):",
        "region": "Введите регион (или оставьте пусто):",
        "category": "Введите категорию (или оставьте пусто):",
        "grams": "Введите граммовку (число, можно пусто):",
        "temp": "Введите температуру °C (число, можно пусто):",
        "tasted_at": "Введите время HH:MM (или пусто):",
        "gear": "Введите посуду (или пусто):",
        "aroma_dry": "Введите аромат сухого листа (текст/CSV):",
        "aroma_warmed": "Введите аромат прогретого листа (текст/CSV):",
        "aroma_after": "Введите аромат после прогрева (текст/CSV):",
        "effects": "Введите ощущения через запятую (или пусто):",
        "scenarios": "Введите сценарии через запятую (или пусто):",
        "rating": "Введите рейтинг 0..10:",
        "summary": "Введите заметку (или пусто):",
    }
    kb = InlineKeyboardBuilder(); kb.button(text="Отмена", callback_data=f"open:{tid}")
    await ui(call, prompts.get(field, "Введите значение:"), reply_markup=kb.as_markup())
    await state.set_state(EditFlow.waiting_text)
    await call.answer()

async def edit_apply(message: Message, state: FSMContext):
    data = await state.get_data()
    tid = data.get("edit_tid"); field = data.get("edit_field")
    if not tid or not field:
        await state.clear()
        await message.answer("Редактирование прервано.", reply_markup=search_menu_kb().as_markup())
        return

    val = (message.text or "").strip()
    to_update = {}
    if field in {"name","region","category","gear","aroma_dry","aroma_warmed","aroma_after","summary","tasted_at"}:
        to_update[field if field!="temp" else "temp_c"] = val or None
    elif field == "year":
        to_update["year"] = int(val) if val.isdigit() else None
    elif field == "grams":
        try: to_update["grams"] = float(val.replace(",", ".")) if val else None
        except: to_update["grams"] = None
    elif field == "temp":
        try: to_update["temp_c"] = int(float(val)) if val else None
        except: to_update["temp_c"] = None
    elif field == "effects":
        to_update["effects_csv"] = ",".join([x.strip() for x in val.split(",") if x.strip()]) or None
    elif field == "scenarios":
        to_update["scenarios_csv"] = ",".join([x.strip() for x in val.split(",") if x.strip()]) or None
    elif field == "rating":
        try:
            r = max(0, min(10, int(val)))
        except:
            r = 0
        to_update["rating"] = r

    with SessionLocal() as s:
        t: Optional[Tasting] = s.get(Tasting, int(tid))
        if not t:
            await state.clear()
            await message.answer("Запись не найдена.", reply_markup=search_menu_kb().as_markup()); return
        for k,v in to_update.items():
            setattr(t, k, v)
        s.commit()
        infusions_data = [{
            "n": inf.n, "seconds": inf.seconds, "liquor_color": inf.liquor_color,
            "taste": inf.taste, "special_notes": inf.special_notes,
            "body": inf.body, "aftertaste": inf.aftertaste
        } for inf in t.infusions]

    await state.clear()
    await message.answer("Обновлено:\n\n" + build_card_text(t, infusions_data), reply_markup=card_actions_kb(t.id).as_markup())

async def del_ask(call: CallbackQuery):
    try:
        _, sid = call.data.split(":", 1)
        tid = int(sid)
    except:
        await call.answer(); return
    await ui(call, "Удалить запись безвозвратно?", reply_markup=confirm_del_kb(tid).as_markup())
    await call.answer()

async def del_ok(call: CallbackQuery):
    try:
        _, sid = call.data.split(":", 1)
        tid = int(sid)
    except:
        await call.answer(); return
    with SessionLocal() as s:
        t = s.get(Tasting, tid)
        if not t:
            await ui(call, "Запись не найдена.", reply_markup=search_menu_kb().as_markup())
            await call.answer(); return
        s.delete(t); s.commit()
    await ui(call, "Удалено.", reply_markup=search_menu_kb().as_markup())
    await call.answer()

async def del_no(call: CallbackQuery):
    await ui(call, "Операция отменена.", reply_markup=search_menu_kb().as_markup())
    await call.answer()

async def delete_cmd(message: Message):
    parts = (message.text or "").strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /delete <id>")
        return
    tid = int(parts[1])
    await message.answer("Удалить запись безвозвратно?", reply_markup=confirm_del_kb(tid).as_markup())

async def edit_cmd(message: Message):
    parts = (message.text or "").strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /edit <id>")
        return
    tid = int(parts[1])
    await message.answer(f"Что изменить в записи #{tid}?", reply_markup=edit_fields_kb(tid).as_markup())

# ---------------- MAIN ----------------

async def main():
    global cfg
    cfg = get_settings()
    if not cfg.token:
        raise RuntimeError("Не найден BOT_TOKEN в .env")

    setup_db(cfg.db_url)

    bot = Bot(cfg.token)
    dp = Dispatcher()

    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="new", description="Новая дегустация"),
        BotCommand(command="find", description="Поиск записей"),
        BotCommand(command="last", description="Последние 5"),
        BotCommand(command="menu", description="Включить кнопки под вводом"),
        BotCommand(command="hide", description="Скрыть кнопки"),
        BotCommand(command="cancel", description="Сброс"),
        BotCommand(command="edit", description="Редактировать запись"),
        BotCommand(command="delete", description="Удалить запись"),
        BotCommand(command="help", description="Справка"),
    ])

    # Команды
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

    # Reply-кнопки
    dp.message.register(
        reply_buttons_router,
        F.text.in_({"📝 Новая дегустация", "🔎 Найти записи", "🕔 Последние 5", "ℹ️ О боте", "Новая дегустация", "Найти записи", "Последние 5", "О боте", "Отмена"})
    )

    # Главное меню
    dp.callback_query.register(about_cb, F.data == "about")
    dp.callback_query.register(new_cb, F.data == "new")
    dp.callback_query.register(find_cb, F.data == "find")
    dp.callback_query.register(back_main, F.data == "back:main")

    # Поток метаданных (создание)
    dp.message.register(name_in, NewTasting.name)

    dp.callback_query.register(year_skip, F.data == "skip:year")
    dp.message.register(year_in, NewTasting.year)

    dp.callback_query.register(region_skip, F.data == "skip:region")
    dp.message.register(region_in, NewTasting.region)

    dp.callback_query.register(cat_pick, F.data.startswith("cat:"), NewTasting.category)  # ограничили по состоянию
    dp.message.register(cat_custom_in, NewTasting.category)

    dp.callback_query.register(grams_skip, F.data == "skip:grams")
    dp.message.register(grams_in, NewTasting.grams)

    dp.callback_query.register(temp_skip, F.data == "skip:temp")
    dp.message.register(temp_in, NewTasting.temp_c)

    dp.callback_query.register(time_now, F.data == "time:now")
    dp.callback_query.register(tasted_at_skip, F.data == "skip:tasted_at")
    dp.message.register(tasted_at_in, NewTasting.tasted_at)

    dp.callback_query.register(gear_skip, F.data == "skip:gear")
    dp.message.register(gear_in, NewTasting.gear)

    # Ароматы
    dp.callback_query.register(aroma_dry_toggle, F.data.startswith("ad:"), NewTasting.aroma_dry)
    dp.message.register(aroma_dry_custom, NewTasting.aroma_dry)

    dp.callback_query.register(aroma_warmed_toggle, F.data.startswith("aw:"), NewTasting.aroma_warmed)
    dp.message.register(aroma_warmed_custom, NewTasting.aroma_warmed)

    dp.callback_query.register(aroma_after_toggle, F.data.startswith("aa:"), NewTasting.aroma_after)
    dp.message.register(aroma_after_custom, NewTasting.aroma_after)

    # Проливы
    dp.message.register(inf_seconds, InfusionState.seconds)

    dp.callback_query.register(color_skip, F.data == "skip:color")
    dp.message.register(inf_color, InfusionState.color)

    dp.callback_query.register(taste_toggle, F.data.startswith("taste:"))
    dp.message.register(taste_custom, InfusionState.taste)
    dp.message.register(inf_taste, InfusionState.taste)

    dp.callback_query.register(special_skip, F.data == "skip:special")
    dp.message.register(inf_special, InfusionState.special)

    dp.callback_query.register(inf_body_pick, F.data.startswith("body:"))
    dp.message.register(inf_body_custom, InfusionState.body)

    dp.callback_query.register(aftertaste_toggle, F.data.startswith("aft:"))
    dp.message.register(aftertaste_custom, InfusionState.aftertaste)

    dp.callback_query.register(more_infusions, F.data == "more_inf")
    dp.callback_query.register(finish_infusions, F.data == "finish_inf")

   # Ощущения / Сценарии / Оценка / Заметка
    dp.callback_query.register(eff_toggle_or_done, F.data.startswith("eff:"), EffectsScenarios.effects)
    dp.message.register(       eff_custom,                             EffectsScenarios.effects)

    dp.callback_query.register(scn_toggle_or_done, F.data.startswith("scn:"), EffectsScenarios.scenarios)
    dp.message.register(       scn_custom,                             EffectsScenarios.scenarios)

    dp.callback_query.register(rate_pick, F.data.startswith("rate:"))
    dp.message.register(       rating_in,  RatingSummary.rating)

    # ВАЖНО: для шага "Заметка" должны быть ровно эти две регистрации (без дублей)
    dp.message.register(       summary_in,   RatingSummary.summary)
    dp.callback_query.register(summary_skip, F.data == "skip:summary")

    # Фото (должны идти после summary)
    dp.message.register(       photo_add,   PhotoFlow.photos, F.photo)
    dp.callback_query.register(photos_done,  F.data == "photos:done", PhotoFlow.photos)
    dp.callback_query.register(photos_skip,  F.data == "skip:photos", PhotoFlow.photos)

    # Фото (важно: регистрировать именно с фильтром F.photo)
    dp.message.register(photo_add, PhotoFlow.photos, F.photo)
    dp.callback_query.register(photos_done, F.data == "photos:done", PhotoFlow.photos)
    dp.callback_query.register(photos_skip, F.data == "skip:photos", PhotoFlow.photos)

    # Показ фото из карточки:
    dp.callback_query.register(show_pics, F.data.startswith("showpics:"))

    # Поиск + пагинация
    dp.callback_query.register(s_last, F.data == "s_last")
    dp.callback_query.register(more_last, F.data.startswith("more:last:"))

    dp.callback_query.register(s_name, F.data == "s_name")
    dp.message.register(s_name_run, SearchFlow.name)

    dp.callback_query.register(s_cat, F.data == "s_cat")
    dp.callback_query.register(s_cat_run, F.data.startswith("cat:"), SearchFlow.cat)

    dp.callback_query.register(s_year, F.data == "s_year")
    dp.message.register(s_year_run, SearchFlow.year)

    dp.callback_query.register(more_name, F.data.startswith("more:name:"))
    dp.callback_query.register(more_cat, F.data.startswith("more:cat:"))
    dp.callback_query.register(more_year, F.data.startswith("more:year:"))

    # Расширенный поиск
    dp.callback_query.register(s_adv, F.data == "s_adv")
    dp.callback_query.register(adv_cat_pick, F.data.startswith("advcat:"))
    dp.message.register(adv_year_in, AdvSearch.year)
    dp.callback_query.register(adv_year_skip, F.data == "skip:advy")
    dp.message.register(adv_text_in, AdvSearch.text)
    dp.callback_query.register(adv_text_skip, F.data == "skip:advt")
    dp.message.register(adv_minr_in, AdvSearch.min_rating)
    dp.callback_query.register(adv_minr_skip, F.data == "skip:advr")
    dp.callback_query.register(adv_sort_pick, F.data.startswith("advs:"))
    dp.callback_query.register(more_adv, F.data.startswith("more:adv:"))

    # Открыть/Редактирование/Удаление
    dp.callback_query.register(open_card, F.data.startswith("open:"))
    dp.callback_query.register(edit_open, F.data.startswith("edit:"))
    dp.callback_query.register(edit_field_pick, F.data.startswith("editf:"))
    dp.message.register(edit_apply, EditFlow.waiting_text)
    dp.callback_query.register(del_ask, F.data.startswith("del:"))
    dp.callback_query.register(del_ok, F.data.startswith("delok:"))
    dp.callback_query.register(del_no, F.data.startswith("delno:"))

    logging.info("Bot started (long-polling)")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
