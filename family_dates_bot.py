"""
Telegram-бот для контроля семейных дат, автоматических напоминаний и умного подбора подарков.
Стек: Python 3.11+, aiogram 3.x, SQLAlchemy 2.0 (asyncpg / PostgreSQL), APScheduler, pandas (экспорт датасета).

Основные возможности:
1. Пошаговый диалог (FSM):
   - Ввод названия события (например, «Юбилей мамы Али»)
   - Ввод даты (например, «25.11.1971» или «25.11.2026»)
   - Быстрый выбор гендерной группы и интересов получателя инлайн-кнопками
   - Сохранение в PostgreSQL и вывод карточки вида: «Юбилей мамы Али — 55 лет»
2. Инлайн-управление под карточкой события:
   - Включение/выключение уведомлений
   - Прикрепление ссылки на подарок и/или фотографии подарка
   - Умный подбор подарков (гибридная система правил, тегов и коллаборативного скоринга)
3. Планировщик уведомлений (APScheduler):
   - Уведомляет за 30 дней (месяц), 21 день (3 недели), 14 дней (2 недели), 7 дней (неделя) и 1 день до даты в 10:00 утра
4. Накопление обучающего датасета (gift_ml_dataset) по всем пользователям:
   - Делит получателей на гендерные и возрастные группы, учитывает выборы/отказы и пользовательские подарки
   - Пересчитывает веса подарков в реальном времени и позволяет выгружать CSV-датасет для обучения ML-модели (/export_dataset)
"""

import asyncio
from contextvars import ContextVar
import csv
import io
import logging
import json
from pathlib import Path
from html import escape
import os
import re
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo
from urllib.parse import quote

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.methods import SendMessage, SendPhoto
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    ForceReply,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import (
    ARRAY,
     BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    delete,
    text,
    select,
    update,
)
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# =====================================================================
# 1. КОНФИГУРАЦИЯ И НАСТРОЙКИ
# =====================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("family_dates_bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/family_dates_db",
)
DEFAULT_TIMEZONE = os.getenv("DEFAULT_TIMEZONE", "Asia/Almaty")

# Дни до события, за которые нужно присылать уведомления в 10:00 утра:
# 30 дней (месяц), 21 день (3 недели), 14 дней (2 недели), 7 дней (1 неделя), 1 день
REMINDER_OFFSETS_DAYS = (30, 21, 14, 7, 1)


# =====================================================================
# 2. МОДЕЛИ БАЗЫ ДАННЫХ (SQLAlchemy 2.0 Async + PostgreSQL)
# =====================================================================


class Base(DeclarativeBase):
    pass


class InterfaceMessage(Base):
    __tablename__ = "bot_interface_messages"
    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)


preserve_messages = ContextVar("preserve_messages", default=False)


class TrackInterfaceMessages(BaseRequestMiddleware):
    def __init__(self, session_maker):
        self.session_maker = session_maker

    async def __call__(self, make_request, bot, method):
        result = await make_request(bot, method)
        if (isinstance(method, (SendMessage, SendPhoto))
                and isinstance(result, Message) and not preserve_messages.get()):
            try:
                async with self.session_maker() as session:
                    await session.merge(InterfaceMessage(
                        chat_id=result.chat.id, message_id=result.message_id
                    ))
                    await session.commit()
            except Exception:
                logger.exception("Не удалось зарегистрировать сообщение для /clean")
        return result


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(128))
    first_name: Mapped[Optional[str]] = mapped_column(String(128))
    timezone: Mapped[str] = mapped_column(String(64), default=DEFAULT_TIMEZONE)
    notify_hour: Mapped[int] = mapped_column(SmallInteger, default=10)
    notify_minute: Mapped[int] = mapped_column(SmallInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    has_birth_year: Mapped[bool] = mapped_column(Boolean, default=True)
    event_type: Mapped[str] = mapped_column(String(50), default="birthday")
    recipient_gender: Mapped[str] = mapped_column(String(20), default="unknown")
    recipient_relation: Mapped[str] = mapped_column(String(64), default="relative")
    recipient_tags: Mapped[List[str]] = mapped_column(ARRAY(Text), default=list)
    gift_budget_kzt: Mapped[Optional[int]] = mapped_column(Integer)
    budget_configured: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    notifications_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    gift_title: Mapped[Optional[str]] = mapped_column(String(255))
    gift_url: Mapped[Optional[str]] = mapped_column(Text)
    gift_photo_file_id: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class NotificationLog(Base):
    __tablename__ = "notification_logs"
    __table_args__ = (
        UniqueConstraint(
            "event_id",
            "target_occurrence_date",
            "days_before",
            name="uq_event_occurrence_offset",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    target_occurrence_date: Mapped[date] = mapped_column(Date, nullable=False)
    days_before: Mapped[int] = mapped_column(Integer, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class GiftCatalog(Base):
    __tablename__ = "gift_catalog"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    target_gender: Mapped[str] = mapped_column(String(20), nullable=False)  # female, male, couple, any
    target_relations: Mapped[List[str]] = mapped_column(ARRAY(Text), default=list)
    tags: Mapped[List[str]] = mapped_column(ARRAY(Text), default=list)
    min_age: Mapped[int] = mapped_column(SmallInteger, default=0)
    max_age: Mapped[int] = mapped_column(SmallInteger, default=120)
    shop_url: Mapped[Optional[str]] = mapped_column(Text)
    estimated_min_kzt: Mapped[Optional[int]] = mapped_column(Integer)
    image_url: Mapped[Optional[str]] = mapped_column(Text)
    image_file_id: Mapped[Optional[str]] = mapped_column(Text)
    verified_product: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    price_checked_on: Mapped[Optional[date]] = mapped_column(Date)
    base_score: Mapped[float] = mapped_column(Float, default=1.0)
    times_selected: Mapped[int] = mapped_column(Integer, default=0)
    times_rejected: Mapped[int] = mapped_column(Integer, default=0)
    created_by_user_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    is_approved: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class GiftMLDataset(Base):
    """
    Таблица накопления обучающей выборки по действиям всех пользователей.
    Используется как для онлайн-ранжирования подарков по гендерным группам и тегам,
    так и для выгрузки датасета в ML-модель (CatBoost / LightGBM / scikit-learn).
    """

    __tablename__ = "gift_ml_dataset"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("events.id", ondelete="SET NULL")
    )
    gift_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("gift_catalog.id", ondelete="SET NULL")
    )
    recipient_gender: Mapped[str] = mapped_column(String(20), nullable=False)
    recipient_age: Mapped[Optional[int]] = mapped_column(SmallInteger)
    recipient_relation: Mapped[Optional[str]] = mapped_column(String(64))
    event_type: Mapped[Optional[str]] = mapped_column(String(50))
    recipient_tags: Mapped[List[str]] = mapped_column(ARRAY(Text), default=list)
    gift_category: Mapped[Optional[str]] = mapped_column(String(100))
    gift_title: Mapped[str] = mapped_column(String(255), nullable=False)
    gift_url: Mapped[Optional[str]] = mapped_column(Text)
    gift_photo_ref: Mapped[Optional[str]] = mapped_column(Text)
    interaction_type: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # custom_added, recommended_selected, recommended_rejected
    rejection_reason: Mapped[Optional[str]] = mapped_column(String(32))
    budget_kzt: Mapped[Optional[int]] = mapped_column(Integer)
    label: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # 1 = positive, 0 = negative
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class GiftImpression(Base):
    __tablename__ = "gift_impressions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("events.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    gift_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("gift_catalog.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    score: Mapped[float] = mapped_column(Float)
    budget_kzt: Mapped[Optional[int]] = mapped_column(Integer)
    recipient_tags: Mapped[List[str]] = mapped_column(ARRAY(Text), default=list)
    algorithm: Mapped[str] = mapped_column(String(32), default="rules-v2")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# =====================================================================
# 3. БИЗНЕС-ЛОГИКА ДАТ, ФОРМАТИРОВАНИЯ И УМНЫХ ПРАВИЛ
# =====================================================================

FEMALE_KEYWORDS = (
    "мам", "бабушк", "жен", "супруг", "сестр", "доч", "подруг", "тет", "тёт",
    "племянниц", "свекров", "тещ", "тёщ", "девушк", "невест", "крёстн",
)
MALE_KEYWORDS = (
    "пап", "отец", "отц", "дедушк", "муж", "брат", "сын", "друг", "дяд",
    "племянник", "тест", "свекр", "свёкр", "парн", "жених",
)


def infer_profile_from_title(title: str) -> Dict[str, str]:
    """
    Автоматически определяет предполагаемый пол получателя, тип события и родство
    по тексту названия (например, «Юбилей мамы Али» -> female, jubilee, mom).
    Пользователь может подтвердить или изменить это в 1 клик.
    """
    lower = title.lower()

    # Тип события
    if "юбилей" in lower:
        event_type = "jubilee"
    elif "годовщин" in lower or "свадьб" in lower:
        event_type = "anniversary"
    else:
        event_type = "birthday"

    # Пол и категория родства
    gender = "unknown"
    relation = "relative"

    if any(k in lower for k in ("мам", "тещ", "тёщ", "свекров", "бабушк")):
        gender, relation = "female", "mom"
    elif any(k in lower for k in ("пап", "отец", "отц", "дедушк", "тест", "свекр")):
        gender, relation = "male", "dad"
    elif any(k in lower for k in ("жен", "супруг", "девушк", "невест")):
        gender, relation = "female", "partner"
    elif any(k in lower for k in ("муж", "парн", "жених")):
        gender, relation = "male", "partner"
    elif "подруг" in lower:
        gender, relation = "female", "friend"
    elif "друг" in lower:
        gender, relation = "male", "friend"
    elif "годовщин" in lower or "свадьб" in lower:
        gender, relation = "couple", "partner"
    elif any(k in lower for k in FEMALE_KEYWORDS):
        gender = "female"
    elif any(k in lower for k in MALE_KEYWORDS):
        gender = "male"

    return {
        "gender": gender,
        "event_type": event_type,
        "relation": relation,
    }


def parse_user_date(raw_text: str, today: Optional[date] = None) -> Tuple[date, bool, Optional[int]]:
    """
    Парсит дату, введённую пользователем.
    Поддерживает форматы:
    - '25.11.1971' (дата рождения / начала события -> вычисляет сколько лет исполнится)
    - '25.11.2026' (конкретная будущая дата)
    - '25.11' (день и месяц без года)
    - '25.11.1971 55' или указание возраста в скобках
    Возвращает: (event_date, has_birth_year, explicit_age)
    """
    if today is None:
        today = date.today()

    cleaned = raw_text.strip()
    # Поддержка ДД.ММ.ГГГГ или ДД/ММ/ГГГГ или ДД-ММ-ГГГГ
    match_full = re.match(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{4})$", cleaned)
    if match_full:
        day, month, year = map(int, match_full.groups())
        parsed = date(year, month, day)
        # Если указан год в прошлом (год рождения или год свадьбы), сохраняем его для подсчёта возраста/лет
        has_birth_year = year < today.year
        return parsed, has_birth_year, None

    match_short = re.match(r"^(\d{1,2})[./-](\d{1,2})$", cleaned)
    if match_short:
        day, month = map(int, match_short.groups())
        parsed = date(today.year, month, day)
        return parsed, False, None

    raise ValueError(
        "Неверный формат даты. Введите дату в формате ДД.ММ.ГГГГ (например, 15.05.1971) или ДД.ММ (например, 15.05)."
    )


def get_next_occurrence(event_date: date, today: Optional[date] = None) -> date:
    """
    Вычисляет ближайшую будущую (или сегодняшнюю) дату наступления ежегодного события.
    Корректно обрабатывает 29 февраля в невисокосные годы (переносит на 28 февраля).
    """
    if today is None:
        today = date.today()

    for year_candidate in (today.year, today.year + 1):
        try:
            candidate = date(year_candidate, event_date.month, event_date.day)
        except ValueError:
            # Для 29 февраля в невисокосный год
            candidate = date(year_candidate, 2, 28)

        if candidate >= today:
            return candidate

    return date(today.year + 1, event_date.month, event_date.day)


def pluralize_years(years: int) -> str:
    """Склонение слова 'год / года / лет'."""
    rem100 = years % 100
    rem10 = years % 10
    if 11 <= rem100 <= 19:
        return "лет"
    if rem10 == 1:
        return "год"
    if 2 <= rem10 <= 4:
        return "года"
    return "лет"


def format_event_headline(
    title: str,
    event_date: date,
    has_birth_year: bool,
    today: Optional[date] = None,
) -> Tuple[str, Optional[int]]:
    """
    Формирует заголовок события точно по требованию пользователя:
    Например: «Юбилей мамы Али — 55 лет»
    Если возраст уже написан в самом названии — не дублирует его.
    Возвращает (отформатированная_строка, возраст_или_годовщина).
    """
    if today is None:
        today = date.today()

    next_occ = get_next_occurrence(event_date, today)
    clean_title = title.strip()

    # Проверяем, не вписал ли пользователь возраст прямо в название ("Юбилей мамы Али - 55 лет")
    age_in_title = re.search(r"(\d{1,3})\s*(лет|год|года)", clean_title, re.IGNORECASE)
    if age_in_title:
        extracted_age = int(age_in_title.group(1))
        return clean_title, extracted_age

    if has_birth_year and event_date.year < next_occ.year:
        turning_years = next_occ.year - event_date.year
        years_word = pluralize_years(turning_years)
        return f"{clean_title} — {turning_years} {years_word}", turning_years

    return clean_title, None


# =====================================================================
# 4. УМНАЯ СИСТЕМА ПРАВИЛ, ТЕГОВ И КОЛЛАБОРАТИВНОГО СКОРИНГА ПОДАРКОВ
# =====================================================================

DEFAULT_GIFT_RULES = [
    # Женская группа: цветы + дополнительные подарки
    {
        "title": "Авторский букет пионовидных роз + набор клубники в шоколаде",
        "category": "Цветы и сладости",
        "target_gender": "female",
        "target_relations": ["mom", "partner", "friend", "relative", "colleague"],
        "tags": ["цветы", "сладости", "романтика"],
        "min_age": 14,
        "max_age": 95,
        "shop_url": "https://flowwow.com/",
        "base_score": 2.6,
    },
    {
        "title": "Ювелирное украшение (серьги/подвеска) + букет свежих цветов",
        "category": "Украшения и цветы",
        "target_gender": "female",
        "target_relations": ["mom", "partner", "relative"],
        "tags": ["цветы", "украшения", "юбилей"],
        "min_age": 18,
        "max_age": 90,
        "shop_url": "https://sokolov.ru/",
        "base_score": 2.5,
    },
    {
        "title": "Сертификат в SPA-салон на массаж и уход + букет гортензий",
        "category": "Красота, SPA и цветы",
        "target_gender": "female",
        "target_relations": ["mom", "partner", "friend", "relative"],
        "tags": ["цветы", "спа", "красота", "отдых"],
        "min_age": 20,
        "max_age": 85,
        "shop_url": "https://chocolife.me/",
        "base_score": 2.4,
    },
    {
        "title": "Кашемировый палантин, аромадиффузор для дома и композиция из цветов",
        "category": "Уют и цветы",
        "target_gender": "female",
        "target_relations": ["mom", "relative", "friend", "colleague"],
        "tags": ["цветы", "уют", "дом"],
        "min_age": 25,
        "max_age": 95,
        "shop_url": "https://kaspi.kz/shop/",
        "base_score": 2.2,
    },
    # Мужская группа: охотничий магазин, туризм, техника, гриль
    {
        "title": "Подарочный сертификат в охотничий магазин «Корган» / «ОхотАктив»",
        "category": "Охота и рыбалка",
        "target_gender": "male",
        "target_relations": ["dad", "partner", "friend", "relative", "colleague"],
        "tags": ["охота", "рыбалка", "туризм"],
        "min_age": 18,
        "max_age": 90,
        "shop_url": "https://korgan.kz/",
        "base_score": 2.7,
    },
    {
        "title": "Мультитул Leatherman, охотничий нож и термокружка Stanley (из охотничьего магазина)",
        "category": "Охота и снаряжение",
        "target_gender": "male",
        "target_relations": ["dad", "partner", "friend", "relative"],
        "tags": ["охота", "туризм", "инструменты"],
        "min_age": 18,
        "max_age": 85,
        "shop_url": "https://korgan.kz/",
        "base_score": 2.5,
    },
    {
        "title": "Полевой бинокль высокой кратности / тепловизионный монокуляр для охоты",
        "category": "Охота и оптика",
        "target_gender": "male",
        "target_relations": ["dad", "partner", "friend", "relative"],
        "tags": ["охота", "гаджеты", "туризм"],
        "min_age": 22,
        "max_age": 85,
        "shop_url": "https://korgan.kz/",
        "base_score": 2.4,
    },
    {
        "title": "Набор для шашлыка и гриля в кожаном кейсе + афганский казан",
        "category": "Охота и барбекю",
        "target_gender": "male",
        "target_relations": ["dad", "partner", "friend", "relative"],
        "tags": ["охота", "гриль", "отдых"],
        "min_age": 20,
        "max_age": 85,
        "shop_url": "https://kaspi.kz/shop/",
        "base_score": 2.3,
    },
    {
        "title": "Пуско-зарядное устройство (бустер) и профессиональный набор автоинструментов",
        "category": "Авто и техника",
        "target_gender": "male",
        "target_relations": ["dad", "partner", "friend", "relative", "colleague"],
        "tags": ["авто", "инструменты", "гаджеты"],
        "min_age": 18,
        "max_age": 80,
        "shop_url": "https://kaspi.kz/shop/",
        "base_score": 2.1,
    },
    # Семейные / Годовщины
    {
        "title": "Парный сертификат на ужин в панорамном ресторане + букет цветов",
        "category": "Годовщина и впечатления",
        "target_gender": "couple",
        "target_relations": ["partner", "mom", "dad", "relative"],
        "tags": ["годовщина", "цветы", "романтика", "семья"],
        "min_age": 18,
        "max_age": 90,
        "shop_url": "https://sxodim.com/",
        "base_score": 2.5,
    },
]


def calculate_gift_score(
    gift: GiftCatalog,
    recipient_gender: str,
    recipient_relation: str,
    recipient_tags: List[str],
    recipient_age: Optional[int],
    event_type: str,
    community_tag_weights: Dict[str, float],
) -> float:
    """
    Формула гибридного ранжирования подарка:
    Score = BaseRuleScore
            + GenderMatchBonus
            + RelationMatchBonus
            + TagOverlapScore
            + AgeMatchBonus
            + CollaborativePopularityBoost (на основе выборов всех пользователей в этой гендерной группе)
    """
    score = gift.base_score

    # Gender is optional context, never a hard restriction on interests.
    if gift.target_gender in (recipient_gender, "any"):
        score += 0.5

    # 2. Совпадение по категории родства (мама, папа, друг, партнёр)
    if recipient_relation in (gift.target_relations or []):
        score += 1.5

    # 3. Пересечение тегов интересов получателя и тегов подарка
    user_tags_set = {t.lower() for t in (recipient_tags or [])}
    gift_tags_set = {t.lower() for t in (gift.tags or [])}
    overlap = user_tags_set.intersection(gift_tags_set)
    score += len(overlap) * 3.0

    # 4. Учёт юбилея
    if event_type == "jubilee" and "юбилей" in gift_tags_set:
        score += 1.5

    # 5. Возрастной интервал
    if recipient_age is not None:
        if gift.min_age <= recipient_age <= gift.max_age:
            score += 0.8
        else:
            score -= 1.5

    # 6. Коллаборативный сигнал от выборов других пользователей (самообучение на датасете)
    # Сглаженная оценка конверсии (Laplace smoothing)
    collaborative_boost = min(1.0, gift.times_selected * 0.1)
    score += max(-1.5, min(4.0, collaborative_boost))

    # 7. Веса тегов, накопленные в gift_ml_dataset для данной гендерной группы
    for tag in gift_tags_set:
        score += community_tag_weights.get(tag, 0.0)

    return round(score, 3)


async def get_smart_recommendations(
    session: AsyncSession,
    event: Event,
    turning_age: Optional[int],
    limit: int = 3,
) -> List[Tuple[GiftCatalog, float]]:
    """
    Возвращает топ-N подарков для события с учётом:
    1) правил по гендерной группе и родству,
    2) тегов получателя,
    3) накопленной статистики выборов всех пользователей в таблице gift_ml_dataset.
    """
    # Learn from tags of the rejected/selected GIFT, not all interests of its recipient.
    stmt_dataset = select(GiftCatalog.tags, GiftMLDataset.label).join(
        GiftMLDataset, GiftMLDataset.gift_id == GiftCatalog.id
    ).where(GiftMLDataset.event_id == event.id,
            GiftMLDataset.interaction_type.in_(["recommended_selected", "recommended_rejected"]),
            (GiftMLDataset.label == 1) | (GiftMLDataset.rejection_reason == "not_interest"))
    dataset_rows = (await session.execute(stmt_dataset)).all()
    community_tag_weights = {}
    for tags_list, label in dataset_rows:
        for tag in tags_list or []:
            key = tag.lower()
            community_tag_weights[key] = max(-3.0, min(3.0,
                community_tag_weights.get(key, 0.0) + (0.4 if label == 1 else -0.6)))

    # Загружаем кандидатов из каталога
    stmt_catalog = select(GiftCatalog).where(
        GiftCatalog.is_approved.is_(True),
        GiftCatalog.created_by_user_id.is_(None),
        GiftCatalog.verified_product.is_(True),
        GiftCatalog.image_url.is_not(None),
        GiftCatalog.shop_url.is_not(None),
        # A rejection belongs to this recipient/event, not every person of a gender.
        ~GiftCatalog.id.in_(select(GiftMLDataset.gift_id).where(
            GiftMLDataset.event_id == event.id,
            GiftMLDataset.interaction_type == "recommended_rejected",
            GiftMLDataset.gift_id.is_not(None),
        )),

    )
    gifts = (await session.execute(stmt_catalog)).scalars().all()

    scored: List[Tuple[GiftCatalog, float]] = []
    for g in gifts:
        if turning_age is not None and event.event_type == "birthday" and not (g.min_age <= turning_age <= g.max_age):
            continue
        if event.gift_budget_kzt and (g.estimated_min_kzt is None or g.estimated_min_kzt > event.gift_budget_kzt):
            continue
        s = calculate_gift_score(
            gift=g,
            recipient_gender=event.recipient_gender,
            recipient_relation=event.recipient_relation,
            recipient_tags=event.recipient_tags or [],
            recipient_age=turning_age,
            event_type=event.event_type,
            community_tag_weights=community_tag_weights,
        )
        if s > 0:
            scored.append((g, s))

    scored.sort(key=lambda item: item[1], reverse=True)
    # Prefer different categories before adding similar ideas.
    diverse, remaining, categories = [], [], set()
    for item in scored:
        if item[0].category in categories:
            remaining.append(item)
        else:
            diverse.append(item)
            categories.add(item[0].category)
    return (diverse + remaining)[:limit]


# =====================================================================
# 5. МАШИНА СОСТОЯНИЙ (FSM) ДЛЯ ДИАЛОГОВ С ПОЛЬЗОВАТЕЛЕМ
# =====================================================================


class AddEventStates(StatesGroup):
    waiting_for_title = State()
    waiting_for_date = State()
    waiting_for_gender_and_tags = State()


class AttachGiftStates(StatesGroup):
    waiting_for_gift_data = State()


# =====================================================================
# 6. ИНЛАЙН-КЛАВИАТУРЫ (InlineKeyboardMarkup)
# =====================================================================


def build_event_card_keyboard(event: Event) -> InlineKeyboardMarkup:
    """
    Клавиатура под карточкой события:
    - Включить / Выключить уведомления (за месяц, 3 нед., 2 нед., 1 нед. и 1 день в 10:00)
    - Прикрепить ссылку на подарок или фото
    - Подобрать подарок (рекомендации ИИ/правил по гендерной группе)
    """
    if event.notifications_enabled:
        notify_btn = InlineKeyboardButton(
            text="🔕 Уведомления включены (10:00) — Выключить",
            callback_data=f"toggle_notify:{event.id}",
        )
    else:
        notify_btn = InlineKeyboardButton(
            text="🔔 Включить уведомления",
            callback_data=f"toggle_notify:{event.id}",
        )

    gift_btn_text = (
        "✏️ Изменить подарок (ссылка / фото)"
        if (event.gift_url or event.gift_photo_file_id or event.gift_title)
        else "🎁 Добавить ссылку на подарок или фото"
    )

    buttons = [
        [notify_btn],
        [
            InlineKeyboardButton(
                text=gift_btn_text,
                callback_data=f"attach_gift:{event.id}",
            )
        ],
        [
            InlineKeyboardButton(
                text="💡 Подобрать подарок",
                callback_data=f"recommend_gift:{event.id}",
            )
        ],
    ]
    buttons.append([InlineKeyboardButton(text="⚙️ Интересы", callback_data=f"gift_preferences:{event.id}"), InlineKeyboardButton(text="💰 Бюджет", callback_data=f"gift_budget:{event.id}")])
    buttons.append([InlineKeyboardButton(text="🗑 Удалить событие", callback_data=f"delete_event:{event.id}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Мои события", callback_data="list_my_events")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_profile_selection_keyboard(
    event_id: int, current_gender: str, selected_tags: List[str]
) -> InlineKeyboardMarkup:
    """
    Инлайн-клавиатура выбора гендерной группы и тегов интересов получателя,
    чтобы система рекомендаций делила получателей на группы и обучалась по признакам.
    """
    gender_row = [
        InlineKeyboardButton(
            text=f"{'✅ ' if current_gender == 'female' else ''}👩 Женщине / Девочке",
            callback_data=f"set_gender:{event_id}:female",
        ),
        InlineKeyboardButton(
            text=f"{'✅ ' if current_gender == 'male' else ''}👨 Мужчине / Мальчику",
            callback_data=f"set_gender:{event_id}:male",
        ),
    ]
    couple_row = [
        InlineKeyboardButton(
            text=f"{'✅ ' if current_gender == 'couple' else ''}👩‍❤️‍👨 Семейной паре (Годовщина)",
            callback_data=f"set_gender:{event_id}:couple",
        )
    ]

    available_tags = [("Книги", "книги"), ("Игры", "игры"), ("Спорт", "спорт"),
        ("Творчество", "творчество"), ("Музыка", "музыка"), ("Кулинария", "кулинария"),
        ("Цветы", "цветы"), ("Украшения", "украшения"), ("SPA", "спа"),
        ("Уют", "уют"), ("Гаджеты", "гаджеты"), ("Туризм", "туризм"),
        ("Охота", "охота"), ("Рыбалка", "рыбалка"), ("Авто", "авто"),
        ("Романтика", "романтика"), ("Сладости", "сладости")]

    tag_rows = []
    current_row = []
    for label, tag_code in available_tags:
        is_active = tag_code in (selected_tags or [])
        btn_text = f"{'☑️' if is_active else '⬜️'} {label}"
        current_row.append(
            InlineKeyboardButton(
                text=btn_text,
                callback_data=f"toggle_tag:{event_id}:{tag_code}",
            )
        )
        if len(current_row) == 2:
            tag_rows.append(current_row)
            current_row = []
    if current_row:
        tag_rows.append(current_row)

    finish_row = [
        InlineKeyboardButton(
            text="✔️ Подобрать подарки",
            callback_data=f"recommend_gift:{event_id}",
        )
    ]

    return InlineKeyboardMarkup(
        inline_keyboard=[gender_row, couple_row, *tag_rows, finish_row]
    )


def marketplace_search_links(title):
    query = quote(title, safe="")
    return [("Kaspi", f"https://kaspi.kz/shop/search/?text={query}"),
            ("Wildberries", f"https://www.wildberries.ru/catalog/0/search.aspx?search={query}"),
            ("Ozon", f"https://www.ozon.ru/search/?text={query}")]


def build_recommendations_keyboard(
    event_id: int, recommendations: List[Tuple[GiftCatalog, float]]
) -> InlineKeyboardMarkup:
    """Кнопки выбора или отклонения предложенных подарков (обратная связь для обучения модели)."""
    rows = []
    for idx, (gift, _score) in enumerate(recommendations, start=1):
        row = [
            InlineKeyboardButton(
                text="👍 Выбрать подарок",
                callback_data=f"gift_fb:{event_id}:{gift.id}:1",
            ),
            InlineKeyboardButton(
                text="👎 Другой подарок",
                callback_data=f"gift_fb:{event_id}:{gift.id}:0",
            ),
        ]
        rows.append(row)
        if gift.shop_url:
            rows.append([InlineKeyboardButton(text="🛍 Открыть предложение магазина", url=gift.shop_url)])
        if not gift.verified_product:
            rows.extend([[InlineKeyboardButton(text=f"🔎 Найти на {name}", url=url)]
                         for name, url in marketplace_search_links(gift.title)])

    rows.append(
        [
            InlineKeyboardButton(
                text="🔙 Назад к карточке события",
                callback_data=f"show_event:{event_id}",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


# =====================================================================
# 7. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ОТОБРАЖЕНИЯ КАРТОЧКИ СОБЫТИЯ
# =====================================================================


def render_event_card_text(event: Event, today: Optional[date] = None) -> str:
    """Формирует текст карточки события в чате."""
    if today is None:
        today = date.today()

    headline, _turning_age = format_event_headline(
        event.title, event.event_date, event.has_birth_year, today
    )
    next_occ = get_next_occurrence(event.event_date, today)
    days_left = (next_occ - today).days

    gender_map = {
        "female": "👩 Женская группа",
        "male": "👨 Мужская группа",
        "couple": "👩‍❤️‍👨 Семейная пара",
        "unknown": "👤 Не указана",
    }
    tags_str = ", ".join(f"#{t}" for t in (event.recipient_tags or [])) or "базовые по группе"
    notify_status = (
        "✅ Включены (за 1 мес., 3 нед., 2 нед., 1 нед. и 1 день в 10:00)"
        if event.notifications_enabled
        else "⏸ Выключены"
    )

    lines = [
        f"🎉 <b>{headline}</b>",
        "",
        f"📅 <b>Дата:</b> {next_occ.strftime('%d.%m.%Y')} (через {days_left} дн.)",
        f"🎯 <b>Группа получателя:</b> {gender_map.get(event.recipient_gender, 'Не указана')}",
        f"🏷 <b>Теги для подбора подарка:</b> {tags_str}",
        f"🔔 <b>Уведомления:</b> {notify_status}",
        f"💰 <b>Бюджет:</b> {str(event.gift_budget_kzt) + ' ₸' if event.gift_budget_kzt else 'без ограничения'}",
    ]

    if event.gift_title or event.gift_url or event.gift_photo_file_id:
        lines.append("")
        lines.append("🎁 <b>Прикреплённый подарок:</b>")
        if event.gift_title:
            lines.append(f"• Описание: {event.gift_title}")
        if event.gift_url:
            lines.append(f"• Ссылка: {event.gift_url}")
        if event.gift_photo_file_id:
            lines.append("• Фото подарка: прикреплено 📷")

    return "\n".join(lines)


# =====================================================================
# 8. ОБРАБОТЧИКИ КОМАНД И СООБЩЕНИЙ БОТА (Router)
# =====================================================================

router = Router()


async def get_or_create_user(session: AsyncSession, tg_user) -> User:
    stmt = select(User).where(User.telegram_id == tg_user.id)
    user = (await session.execute(stmt)).scalar_one_or_none()
    if not user:
        user = User(
            telegram_id=tg_user.id,
            username=tg_user.username,
            first_name=tg_user.first_name,
            timezone=DEFAULT_TIMEZONE,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return user


async def track_dialog_message(state: FSMContext, message: Message):
    data = await state.get_data()
    ids = list(data.get("dialog_message_ids", []))
    if message.message_id not in ids:
        ids.append(message.message_id)
    await state.update_data(dialog_message_ids=ids)


async def dialog_prompt(message: Message, state: FSMContext, text: str):
    prompt = await message.answer(text, reply_markup=ForceReply())
    await track_dialog_message(state, prompt)
    await state.update_data(dialog_prompt_id=prompt.message_id)


async def is_dialog_reply(message: Message, state: FSMContext) -> bool:
    if message.text and message.text.startswith("/"):
        return False
    if message.chat.type in ("group", "supergroup"):
        data = await state.get_data()
        return bool(message.reply_to_message and
                    message.reply_to_message.message_id == data.get("dialog_prompt_id"))
    return True


async def clean_dialog(message: Message, state: FSMContext):
    data = await state.get_data()
    # Delete only explicitly recorded messages from this user's active dialogue.
    # Cards, menus, reminders and unrelated chat messages are never recorded.
    for message_id in data.get("dialog_message_ids", []):
        try:
            await message.bot.delete_message(chat_id=message.chat.id, message_id=message_id)
        except TelegramAPIError:
            logger.info("Не удалось удалить служебное сообщение %s в чате %s", message_id, message.chat.id)
    await state.clear()


async def get_chat_event(session: AsyncSession, event_id: int, chat_id: int):
    return (await session.execute(
        select(Event).where(Event.id == event_id, Event.chat_id == chat_id)
    )).scalar_one_or_none()


async def show_events_menu(message: Message, tg_user, session_maker: async_sessionmaker):
    async with session_maker() as session:
        user = await get_or_create_user(session, tg_user)
        events = (await session.execute(
            select(Event).where(Event.chat_id == message.chat.id)
        )).scalars().all()
        today = datetime.now(ZoneInfo(user.timezone)).date()

    rows = []
    for event in sorted(events, key=lambda e: get_next_occurrence(e.event_date, today)):
        headline, _ = format_event_headline(
            event.title, event.event_date, event.has_birth_year, today
        )
        next_date = get_next_occurrence(event.event_date, today)
        rows.append([InlineKeyboardButton(
            text=f"📅 {headline} · {next_date:%d.%m}",
            callback_data=f"show_event:{event.id}",
        )])
    rows.append([InlineKeyboardButton(
        text="➕ Добавить событие", callback_data="start_add_event"
    )])
    name = (message.chat.title if message.chat.type in ("group", "supergroup")
            else (f"@{tg_user.username}" if tg_user.username else tg_user.first_name))
    description = (
        "События этого чата — нажми, чтобы открыть карточку."
        if events else "Пока нет событий. Нажми ➕, чтобы добавить первую карточку."
    )
    await message.answer(
        f"👤 <b>{escape(name or 'Мои события')}</b>\n\n{description}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.message(F.migrate_to_chat_id)
async def migrate_group(message: Message, session_maker: async_sessionmaker):
    async with session_maker() as session:
        await session.execute(update(Event).where(Event.chat_id == message.chat.id)
                              .values(chat_id=message.migrate_to_chat_id))
        await session.commit()


@router.message(F.new_chat_members)
async def welcome_group(message: Message):
    if any(member.id == message.bot.id for member in message.new_chat_members):
        await message.answer(
            "📅 Здесь будет отдельный список общих событий.\n"
            "Участники группы могут добавлять, изменять и удалять карточки.\n"
            "Нажмите /new для создания события или /events для списка.\n"
            "Включите уведомления в карточке — они будут приходить в эту группу "
            "в 10:00 (Asia/Almaty)."
        )


@router.message(Command("clean"))
async def cmd_clean(message: Message, state: FSMContext, session_maker: async_sessionmaker):
    await clean_dialog(message, state)
    async with session_maker() as session:
        tracked = (await session.execute(select(InterfaceMessage).where(
            InterfaceMessage.chat_id == message.chat.id
        ))).scalars().all()
        for item in tracked:
            removed = False
            try:
                await message.bot.delete_message(chat_id=item.chat_id, message_id=item.message_id)
                removed = True
            except TelegramBadRequest as exc:
                removed = "message to delete not found" in exc.message.lower()
            except TelegramAPIError:
                pass
            if removed:
                await session.delete(item)
        await session.commit()
    try:
        await message.delete()
    except TelegramAPIError:
        pass
    await show_events_menu(message, message.from_user, session_maker)


@router.message(CommandStart())
@router.message(Command("events", "menu"))
async def cmd_start(message: Message, state: FSMContext, session_maker: async_sessionmaker):
    await state.clear()
    await show_events_menu(message, message.from_user, session_maker)


@router.callback_query(F.data == "start_add_event")
async def cb_start_add_event(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(AddEventStates.waiting_for_title)
    await dialog_prompt(callback.message, state,
        "📝 <b>Внеси название события</b>\n"
        "Например: <i>Юбилей мамы Али</i>, <i>День рождения папы</i> или <i>Годовщина свадьбы</i>:",
    )
    await callback.answer()


@router.message(Command("add", "new"))
async def cmd_add_event(message: Message, state: FSMContext):
    await state.clear()
    await track_dialog_message(state, message)
    await state.set_state(AddEventStates.waiting_for_title)
    await dialog_prompt(message, state,
        "📝 <b>Внеси название события</b>\n"
        "Например: <i>Юбилей мамы Али</i>, <i>День рождения папы</i> или <i>Годовщина свадьбы</i>:",
    )


@router.message(AddEventStates.waiting_for_title, F.text)
async def process_event_title(message: Message, state: FSMContext):
    if not await is_dialog_reply(message, state):
        return
    await track_dialog_message(state, message)
    title = message.text.strip()
    if len(title) < 2:
        await dialog_prompt(message, state,"Пожалуйста, введи более подробное название события:")
        return

    await state.update_data(title=title)
    await state.set_state(AddEventStates.waiting_for_date)
    await dialog_prompt(message, state,
        f"Отлично: <b>{escape(title)}</b>!\n\n"
        "📅 <b>Далее введи дату</b> в формате <code>ДД.ММ.ГГГГ</code> (если укажешь год рождения или начала события, "
        "я сам посчитаю, сколько лет исполняется — например, <code>25.11.1971</code>) "
        "или просто <code>ДД.ММ</code>:",
    )


@router.message(AddEventStates.waiting_for_date, F.text)
async def process_event_date(
    message: Message, state: FSMContext, session_maker: async_sessionmaker
):
    if not await is_dialog_reply(message, state):
        return
    await track_dialog_message(state, message)
    try:
        parsed_date, has_birth_year, _ = parse_user_date(message.text)
    except ValueError as err:
        await dialog_prompt(message, state,f"⚠️ {err}")
        return

    data = await state.get_data()
    title = data["title"]
    inferred = infer_profile_from_title(title)

    # Interests are selected explicitly; gender does not imply a hobby.
    default_tags = []

    async with session_maker() as session:
        user = await get_or_create_user(session, message.from_user)
        new_event = Event(
            user_id=user.id,
            chat_id=message.chat.id,
            title=title,
            event_date=parsed_date,
            has_birth_year=has_birth_year,
            event_type=inferred["event_type"],
            recipient_gender=inferred["gender"],
            recipient_relation=inferred["relation"],
            recipient_tags=default_tags,
            notifications_enabled=False,
        )
        session.add(new_event)
        await session.commit()
        await session.refresh(new_event)

        # Если пол однозначно определился из названия (например, "Юбилей мамы Али"),
        # сразу показываем готовую карточку события с кнопкой "Включить уведомления"
        # и дополнительно даём возможность уточнить теги
        card_text = render_event_card_text(new_event)
        kb = build_event_card_keyboard(new_event)

    await message.answer(card_text, reply_markup=kb)
    await clean_dialog(message, state)
    await show_events_menu(message, message.from_user, session_maker)

    if new_event.recipient_gender == "unknown":
        await message.answer(
            "Выбери интересы получателя — они помогут точнее подобрать подарок:",
            reply_markup=build_profile_selection_keyboard(
                new_event.id, new_event.recipient_gender, new_event.recipient_tags
            ),
        )


# =====================================================================
# 9. УПРАВЛЕНИЕ УВЕДОМЛЕНИЯМИ И ПРОФИЛЕМ ПОЛУЧАТЕЛЯ (Callback-хендлеры)
# =====================================================================


@router.callback_query(F.data.startswith("toggle_notify:"))
async def cb_toggle_notifications(
    callback: CallbackQuery, session_maker: async_sessionmaker
):
    event_id = int(callback.data.split(":")[1])
    async with session_maker() as session:
        event = await get_chat_event(session, event_id, callback.message.chat.id)
        if not event:
            await callback.answer("Событие не найдено.", show_alert=True)
            return

        event.notifications_enabled = not event.notifications_enabled
        await session.commit()
        await session.refresh(event)

        card_text = render_event_card_text(event)
        kb = build_event_card_keyboard(event)

    await edit_gift_message(callback, card_text, kb)
    if event.notifications_enabled:
        await callback.answer(
            "🔔 Уведомления включены! Напомню за месяц, 3 недели, 2 недели, неделю и за 1 день в 10:00 утра.",
            show_alert=True,
        )
    else:
        await callback.answer("🔕 Уведомления для этого события выключены.")


@router.callback_query(F.data.startswith("set_gender:"))
async def cb_set_gender(callback: CallbackQuery, session_maker: async_sessionmaker):
    _, event_id_str, gender = callback.data.split(":")
    event_id = int(event_id_str)

    async with session_maker() as session:
        event = await get_chat_event(session, event_id, callback.message.chat.id)
        if not event:
            await callback.answer("Событие не найдено.", show_alert=True)
            return

        event.recipient_gender = gender
        await session.commit()
        await session.refresh(event)

        kb = build_profile_selection_keyboard(
            event.id, event.recipient_gender, event.recipient_tags
        )

    await callback.message.edit_reply_markup(reply_markup=kb)
    await callback.answer("Гендерная группа обновлена!")


@router.callback_query(F.data.startswith("toggle_tag:"))
async def cb_toggle_tag(callback: CallbackQuery, session_maker: async_sessionmaker):
    _, event_id_str, tag_code = callback.data.split(":")
    event_id = int(event_id_str)

    async with session_maker() as session:
        event = await get_chat_event(session, event_id, callback.message.chat.id)
        if not event:
            await callback.answer("Событие не найдено.", show_alert=True)
            return

        tags = list(event.recipient_tags or [])
        if tag_code in tags:
            tags.remove(tag_code)
        else:
            tags.append(tag_code)
        event.recipient_tags = tags

        await session.commit()
        await session.refresh(event)

        kb = build_profile_selection_keyboard(
            event.id, event.recipient_gender, event.recipient_tags
        )

    await callback.message.edit_reply_markup(reply_markup=kb)
    await callback.answer(f"Тег #{tag_code} переключён")


@router.callback_query(F.data.startswith("delete_event:"))
async def cb_delete_event_prompt(callback: CallbackQuery, session_maker: async_sessionmaker):
    event_id = int(callback.data.split(":")[1])
    async with session_maker() as session:
        event = (await session.execute(
            select(Event).where(
                Event.id == event_id, Event.chat_id == callback.message.chat.id
            )
        )).scalar_one_or_none()
        if event is None:
            await callback.answer("Событие уже удалено или недоступно.", show_alert=True)
            return
        title = escape(event.title)
    await callback.answer()
    await callback.message.answer(
        f"🗑 Удалить событие <b>{title}</b>?\n\n"
        "Его карточка и будущие напоминания будут удалены. Отменить удаление нельзя.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"confirm_delete_event:{event_id}")],
            [InlineKeyboardButton(text="Отмена", callback_data=f"show_event:{event_id}")],
        ]),
    )


@router.callback_query(F.data.startswith("confirm_delete_event:"))
async def cb_confirm_delete_event(
    callback: CallbackQuery, state: FSMContext, session_maker: async_sessionmaker
):
    event_id = int(callback.data.split(":")[1])
    async with session_maker() as session:
        deleted_id = (await session.execute(
            delete(Event).where(
                Event.id == event_id,
                Event.chat_id == callback.message.chat.id,
            ).returning(Event.id)
        )).scalar_one_or_none()
        await session.commit()
    await callback.answer("Событие удалено." if deleted_id else "Событие уже удалено или недоступно.")
    await state.clear()
    await edit_gift_message(callback, "✅ Событие удалено." if deleted_id else "Событие уже удалено или недоступно.", None)
    await show_events_menu(callback.message, callback.from_user, session_maker)


@router.callback_query(F.data.startswith("show_event:"))
async def cb_show_event(callback: CallbackQuery, session_maker: async_sessionmaker):
    event_id = int(callback.data.split(":")[1])
    async with session_maker() as session:
        event = (await session.execute(
            select(Event).where(
                Event.id == event_id, Event.chat_id == callback.message.chat.id
            )
        )).scalar_one_or_none()
        if not event:
            await callback.answer("Событие не найдено.", show_alert=True)
            return

        card_text = render_event_card_text(event)
        kb = build_event_card_keyboard(event)

    if event.gift_photo_file_id:
        try:
            await callback.message.answer_photo(
                photo=event.gift_photo_file_id,
                caption=f"🎁 {escape((event.gift_title or 'Выбранный подарок')[:255])}",
            )
        except TelegramAPIError:
            logger.info("Фото выбранного подарка недоступно, event_id=%s", event.id)
    await edit_gift_message(callback, card_text, kb, force_new=bool(event.gift_photo_file_id))
    await callback.answer()


# =====================================================================
# 10. ПРИКРЕПЛЕНИЕ ССЫЛКИ НА ПОДАРОК ИЛИ ФОТОГРАФИИ + ОБУЧЕНИЕ ДАТАСЕТА
# =====================================================================


@router.callback_query(F.data.startswith("attach_gift:"))
async def cb_attach_gift_prompt(callback: CallbackQuery, state: FSMContext):
    event_id = int(callback.data.split(":")[1])
    await state.update_data(attach_event_id=event_id)
    await state.set_state(AttachGiftStates.waiting_for_gift_data)

    await callback.message.answer(
        "🎁 <b>Прикрепи подарок к событию</b>\n\n"
        "Ты можешь отправить:\n"
        "1️⃣ <b>Ссылку на подарок</b> и название (например: <code>Букет пионов https://flowwow.com/...</code>)\n"
        "2️⃣ <b>Фотографию / картинку подарка</b> (можно с подписью и ссылкой в описании к фото)\n\n"
        "Ответь на это сообщение текстом или фотографией.",
        reply_markup=ForceReply(),
    )
    await callback.answer()


@router.message(AttachGiftStates.waiting_for_gift_data)
async def process_attached_gift(
    message: Message, state: FSMContext, session_maker: async_sessionmaker
):
    data = await state.get_data()
    event_id = data.get("attach_event_id")
    if not event_id:
        await state.clear()
        return

    raw_text = (message.caption or message.text or "").strip()
    photo_file_id: Optional[str] = None
    if message.photo:
        # Берём самое высокое разрешение фото из присланных
        photo_file_id = message.photo[-1].file_id

    if not raw_text and not photo_file_id:
        await message.answer("Пожалуйста, пришли ссылку, текст или фотографию подарка.")
        return

    # Извлекаем URL из текста/подписи, если он есть
    url_match = re.search(r"(https?://\S+)", raw_text)
    gift_url = url_match.group(1) if url_match else None
    gift_title = re.sub(r"https?://\S+", "", raw_text).strip() or "Сохранённый подарок"

    async with session_maker() as session:
        user = await get_or_create_user(session, message.from_user)
        event = await get_chat_event(session, event_id, message.chat.id)
        if not event:
            await message.answer("Событие не найдено.")
            await state.clear()
            return

        event.gift_title = gift_title
        if gift_url:
            event.gift_url = gift_url
        if photo_file_id:
            event.gift_photo_file_id = photo_file_id

        _, turning_age = format_event_headline(
            event.title, event.event_date, event.has_birth_year
        )

        # Пополняем каталог пользовательских идей и обучающий датасет (label = 1)
        new_catalog_item = GiftCatalog(
            title=gift_title,
            category="Пользовательский выбор",
            target_gender=event.recipient_gender if event.recipient_gender != "unknown" else "any",
            target_relations=[event.recipient_relation],
            tags=event.recipient_tags or [],
            shop_url=gift_url,
            base_score=1.8,
            times_selected=1,
            created_by_user_id=user.id,
            is_approved=False,
        )
        session.add(new_catalog_item)
        await session.flush()

        ml_record = GiftMLDataset(
            user_id=user.id,
            event_id=event.id,
            gift_id=new_catalog_item.id,
            recipient_gender=event.recipient_gender,
            recipient_age=turning_age,
            recipient_relation=event.recipient_relation,
            event_type=event.event_type,
            recipient_tags=event.recipient_tags or [],
            gift_category="Пользовательский выбор",
            gift_title=gift_title,
            gift_url=gift_url,
            gift_photo_ref=photo_file_id,
            interaction_type="custom_added",
            label=1,
        )
        session.add(ml_record)
        await session.commit()
        await session.refresh(event)

        card_text = render_event_card_text(event)
        kb = build_event_card_keyboard(event)

    await state.clear()

    if event.gift_photo_file_id:
        await message.answer_photo(
            photo=event.gift_photo_file_id,
            caption=f"✅ Подарок сохранён!\n\n{card_text}",
            reply_markup=kb,
        )
    else:
        await message.answer(f"✅ Подарок сохранён!\n\n{card_text}", reply_markup=kb)


# =====================================================================
# 11. РЕКОМЕНДАТЕЛЬНАЯ СИСТЕМА И ОБРАТНАЯ СВЯЗЬ ДЛЯ ОБУЧЕНИЯ МОДЕЛИ
# =====================================================================


EXTRA_GIFT_IDEAS = [
    ("Книга по любимой теме", "Книги", ["книги"], 3000),
    ("Настольная игра для компании", "Игры", ["игры", "семья"], 7000),
    ("Пазл или набор головоломок", "Игры", ["игры", "уют"], 3000),
    ("Скетчбук и материалы для рисования", "Творчество", ["творчество"], 4000),
    ("Набор для приготовления любимого блюда", "Кулинария", ["кулинария"], 5000),
    ("Чайный или кофейный набор", "Еда и напитки", ["кулинария", "уют"], 5000),
    ("Набор любимых сладостей", "Еда и напитки", ["сладости"], 3000),
    ("Фотоальбом с совместными воспоминаниями", "Личные подарки", ["семья", "романтика"], 4000),
    ("Письмо и домашний ужин-сюрприз", "Личные подарки", ["романтика", "кулинария"], 2000),
    ("Бутылка для воды или спортивное полотенце", "Спорт", ["спорт"], 3000),
    ("Коврик для тренировок", "Спорт", ["спорт"], 6000),
    ("Термокружка для прогулок", "Туризм", ["туризм", "отдых"], 5000),
    ("Органайзер для автомобиля", "Авто", ["авто"], 5000),
    ("Подставка для телефона или ноутбука", "Аксессуары", ["гаджеты"], 4000),
    ("Плед или декоративная подушка", "Дом", ["уют", "дом"], 6000),
    ("Комнатное растение", "Растения", ["цветы", "дом"], 4000),
    ("Мастер-класс по интересам получателя", "Впечатления", ["творчество", "кулинария"], 12000),
    ("Билет на концерт любимого исполнителя", "Впечатления", ["музыка"], 15000),
]
REJECTION_REASONS = {"expensive": "💸 Дорого", "already_have": "📦 Уже есть",
                     "not_interest": "🙅 Не его интересы", "other": "🔄 Просто другое", "skip": "Пропустить"}
BUDGET_OPTIONS = (5000, 10000, 20000, 50000, 100000, 0)


def price_hint(gift):
    if gift.price_checked_on and gift.estimated_min_kzt:
        return f"от {gift.estimated_min_kzt:,} ₸ · проверено {gift.price_checked_on:%d.%m.%Y}".replace(",", " ")
    return (f"ориентир от {gift.estimated_min_kzt:,} ₸".replace(",", " ")
            if gift.estimated_min_kzt else "цену нужно уточнить")


def budget_keyboard(event_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=(f"До {amount:,} ₸".replace(",", " ") if amount else "Без ограничения"),
                              callback_data=f"gift_budget_set:{event_id}:{amount}")]
        for amount in BUDGET_OPTIONS
    ] + [[InlineKeyboardButton(text="⬅️ К событию", callback_data=f"show_event:{event_id}")]])


async def edit_gift_message(callback, content, keyboard, force_new=False):
    if force_new or callback.message.photo:
        if callback.message.photo:
            await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(content, reply_markup=keyboard, disable_web_page_preview=True)
    else:
        try:
            await callback.message.edit_text(content, reply_markup=keyboard, disable_web_page_preview=True)
        except TelegramBadRequest as exc:
            if "message is not modified" not in exc.message.lower():
                raise


@router.callback_query(F.data.startswith("gift_budget:"))
async def cb_gift_budget(callback: CallbackQuery, session_maker: async_sessionmaker):
    event_id = int(callback.data.split(":")[1])
    async with session_maker() as session:
        event = await get_chat_event(session, event_id, callback.message.chat.id)
        if event is None:
            await callback.answer("Событие недоступно.", show_alert=True)
            return
    await callback.answer()
    await edit_gift_message(callback, "Выбери максимальный бюджет в тенге. Сохраню его для этого события.", budget_keyboard(event_id))


@router.callback_query(F.data.startswith("gift_budget_set:"))
async def cb_set_gift_budget(callback: CallbackQuery, session_maker: async_sessionmaker):
    _, event_id, amount = callback.data.split(":")
    amount = int(amount)
    if amount not in BUDGET_OPTIONS:
        await callback.answer("Выбери сумму кнопкой.")
        return
    async with session_maker() as session:
        event = await get_chat_event(session, int(event_id), callback.message.chat.id)
        if event is None:
            await callback.answer("Событие недоступно.", show_alert=True)
            return
        event.gift_budget_kzt = amount or None
        event.budget_configured = True
        await session.commit()
    await cb_recommend_gift(callback, session_maker)


@router.callback_query(F.data.startswith("gift_reason:"))
async def cb_gift_reason(callback: CallbackQuery, session_maker: async_sessionmaker):
    _, record_id, reason = callback.data.split(":")
    if reason not in REJECTION_REASONS:
        await callback.answer("Неизвестная причина.")
        return
    async with session_maker() as session:
        record = (await session.execute(select(GiftMLDataset).join(Event).join(User, User.id == GiftMLDataset.user_id).where(
            GiftMLDataset.id == int(record_id), Event.chat_id == callback.message.chat.id,
            User.telegram_id == callback.from_user.id, GiftMLDataset.interaction_type == "recommended_rejected"
        ).with_for_update())).scalar_one_or_none()
        if record is None:
            await callback.answer("Этот ответ доступен только автору отказа.", show_alert=True)
            return
        if record.rejection_reason:
            await callback.answer("Причина уже сохранена.")
            return
        record.rejection_reason = reason
        event_id = record.event_id
        await session.commit()
    await callback.answer("Учтено")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Уменьшить бюджет", callback_data=f"gift_budget:{event_id}")]
    ]) if reason == "expensive" else None
    await callback.message.edit_text("Спасибо! Причина сохранена." if reason != "skip" else "Можно продолжать подбор выше.", reply_markup=kb)


@router.callback_query(F.data.startswith("recommend_gift:"))
async def cb_recommend_gift(
    callback: CallbackQuery, session_maker: async_sessionmaker
):
    event_id = int(callback.data.split(":")[1])
    async with session_maker() as session:
        event = await get_chat_event(session, event_id, callback.message.chat.id)
        if not event:
            await callback.answer("Событие не найдено.", show_alert=True)
            return

        if not event.budget_configured:
            await callback.answer()
            await edit_gift_message(callback, "Какой бюджет на подарок? Суммы в тенге.", budget_keyboard(event.id))
            return

        headline, turning_age = format_event_headline(
            event.title, event.event_date, event.has_birth_year
        )
        recommendations = await get_smart_recommendations(
            session, event, turning_age, limit=1
        )

    await callback.answer()
    await show_gift_recommendations(callback, event_id, headline, recommendations, session_maker)


async def show_gift_recommendations(callback, event_id, headline, recommendations, session_maker=None, new_message=False):
    if not recommendations:
        await edit_gift_message(callback,
            "Подходящие товары с фотографиями закончились.\n"
            "Можно изменить бюджет, уточнить интересы или прикрепить свой подарок.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💰 Изменить бюджет", callback_data=f"gift_budget:{event_id}")],
                [InlineKeyboardButton(text="⚙️ Уточнить интересы", callback_data=f"gift_preferences:{event_id}")],
                [InlineKeyboardButton(text="🎁 Добавить свой подарок", callback_data=f"attach_gift:{event_id}")],
                [InlineKeyboardButton(text="⬅️ К событию", callback_data=f"show_event:{event_id}")],
            ]),
            force_new=new_message,
        )
        return

    lines = [
        f"💡 <b>Идея подарка для «{escape(headline)}»</b>",
        "<i>Цена при проверке в Алматы. Текущую цену, продавца и доставку уточни в магазине.</i>",
        "",
    ]
    for idx, (gift, score) in enumerate(recommendations, start=1):
        tags_fmt = " ".join(f"#{t}" for t in (gift.tags or []))
        url_part = f"\n   🔗 <a href='{escape(gift.shop_url, quote=True)}'>Перейти в магазин</a>" if gift.shop_url else ""
        lines.append(
            f"🎁 <b>{escape(gift.title)}</b>\n"
            f"   📂 {escape(gift.category)} | {price_hint(gift)}\n"
            f"   🏷 {tags_fmt}{url_part}\n"
        )

    lines.append(
        "Нажми <b>«👍 Выбрать»</b>, чтобы прикрепить подарок к событию, "
        "или <b>«👎 Не подходит»</b>, чтобы получить другую идею:"
    )

    kb = build_recommendations_keyboard(event_id, recommendations)
    kb.inline_keyboard.insert(0, [InlineKeyboardButton(text="💰 Бюджет", callback_data=f"gift_budget:{event_id}"), InlineKeyboardButton(text="⚙️ Интересы", callback_data=f"gift_preferences:{event_id}")])
    # Photo + compact caption + actions form one card (Telegram limit: 1024).
    photo_sent = False
    photo_file_id = None
    if len(recommendations) == 1 and recommendations[0][0].image_url:
        try:
            gift = recommendations[0][0]
            caption = (f"🎁 <b>{escape(gift.title[:255])}</b>\n"
                       f"Для: {escape(headline[:100])}\n\n"
                       f"{price_hint(gift)}\n"
                       "Цена при проверке в Алматы. Уточни цену и доставку в магазине.\n\n"
                       "👍 Сохранить к событию · 👎 Другой подарок")
            sent_photo = await callback.message.answer_photo(
                photo=recommendations[0][0].image_file_id or recommendations[0][0].image_url,
                caption=caption,
                reply_markup=kb,
            )
            photo_sent = True
            if sent_photo.photo:
                photo_file_id = sent_photo.photo[-1].file_id
        except TelegramAPIError:
            logger.info("Фото каталога недоступно, gift_id=%s", recommendations[0][0].id)
    if not photo_sent:
        if recommendations[0][0].image_url:
            lines.append("Фото временно недоступно. Его можно посмотреть по ссылке магазина.")
        await edit_gift_message(callback, "\n".join(lines), kb, force_new=new_message)
    if session_maker:
        async with session_maker() as session:
            event = await get_chat_event(session, event_id, callback.message.chat.id)
            if event:
                user = await get_or_create_user(session, callback.from_user)
                if isinstance(photo_file_id, str):
                    await session.execute(update(GiftCatalog).where(GiftCatalog.id == recommendations[0][0].id)
                                          .values(image_file_id=photo_file_id))
                for position, (gift, score) in enumerate(recommendations, 1):
                    session.add(GiftImpression(event_id=event.id, user_id=user.id, gift_id=gift.id,
                        position=position, score=score, budget_kzt=event.gift_budget_kzt,
                        recipient_tags=list(event.recipient_tags or [])))
                await session.commit()


@router.callback_query(F.data.startswith("gift_preferences:"))
async def cb_gift_preferences(callback: CallbackQuery, session_maker: async_sessionmaker):
    event_id = int(callback.data.split(":")[1])
    async with session_maker() as session:
        event = await get_chat_event(session, event_id, callback.message.chat.id)
        if event is None:
            await callback.answer("Событие недоступно.", show_alert=True)
            return
        keyboard = build_profile_selection_keyboard(event.id, event.recipient_gender, event.recipient_tags)
    await callback.answer()
    await edit_gift_message(callback, "Укажи интересы получателя, затем вернись в карточку для подбора.", keyboard)


@router.callback_query(F.data.startswith("gift_fb:"))
async def cb_gift_feedback(
    callback: CallbackQuery, session_maker: async_sessionmaker
):
    _, event_id_str, gift_id_str, label_str = callback.data.split(":")
    event_id = int(event_id_str)
    gift_id = int(gift_id_str)
    label = int(label_str)  # 1 = выбран, 0 = отклонён
    if label not in (0, 1):
        await callback.answer("Неизвестный ответ.")
        return

    async with session_maker() as session:
        user = await get_or_create_user(session, callback.from_user)
        event = await get_chat_event(session, event_id, callback.message.chat.id)
        gift = await session.get(GiftCatalog, gift_id)
        if not event or not gift or gift.created_by_user_id is not None or not gift.is_approved:
            await callback.answer("Запись не найдена.", show_alert=True)
            return

        _, turning_age = format_event_headline(
            event.title, event.event_date, event.has_birth_year
        )

        await session.execute(select(Event.id).where(Event.id == event.id).with_for_update())
        previous = (await session.execute(select(GiftMLDataset.id).where(
            GiftMLDataset.event_id == event.id,
            GiftMLDataset.user_id == user.id,
            GiftMLDataset.gift_id == gift.id,
            GiftMLDataset.label == label,
            GiftMLDataset.interaction_type.in_(["recommended_selected", "recommended_rejected"]),
        ).limit(1))).scalar_one_or_none()
        if previous is not None:
            await callback.answer("Этот ответ уже учтён. Открой подбор заново из карточки.")
            return

        # 1. Обновляем счётчики коллаборативного обучения в каталоге
        if label == 1:
            gift.times_selected += 1
            event.gift_title = gift.title
            event.gift_url = gift.shop_url
            event.gift_photo_file_id = gift.image_file_id or gift.image_url
        else:
            gift.times_rejected += 1

        # 2. Записываем строку в обучающий датасет gift_ml_dataset
        ml_entry = GiftMLDataset(
            user_id=user.id,
            event_id=event.id,
            gift_id=gift.id,
            recipient_gender=event.recipient_gender,
            recipient_age=turning_age,
            recipient_relation=event.recipient_relation,
            event_type=event.event_type,
            recipient_tags=event.recipient_tags or [],
            gift_category=gift.category,
            gift_title=gift.title,
            gift_url=gift.shop_url,
            interaction_type=(
                "recommended_selected" if label == 1 else "recommended_rejected"
            ),
            label=label,
            budget_kzt=event.gift_budget_kzt,
        )
        session.add(ml_entry)
        await session.commit()
        await session.refresh(event)

        if label == 1:
            card_text = render_event_card_text(event)
            kb = build_event_card_keyboard(event)
            await edit_gift_message(callback, f"🎁 Подарок <b>«{escape(gift.title)}»</b> прикреплён к событию!\n\n{card_text}", kb)
            await callback.answer("Подарок выбран и сохранён в событие!")
        else:
            recommendations = await get_smart_recommendations(session, event, turning_age, limit=1)
            headline, _ = format_event_headline(event.title, event.event_date, event.has_birth_year)
            await callback.answer("Убрал этот подарок. Показываю другие идеи.")
            await callback.message.answer(f"Почему не подошёл «{escape(gift.title)}»? Это необязательно.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=title, callback_data=f"gift_reason:{ml_entry.id}:{reason}")]
                    for reason, title in REJECTION_REASONS.items()
                ]))
            await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ К событию", callback_data=f"show_event:{event.id}")]
            ]))
            await show_gift_recommendations(callback, event.id, headline, recommendations, session_maker, new_message=True)


# =====================================================================
# 12. СПИСОК СОБЫТИЙ И ЭКСПОРТ ML-ДАТАСЕТА В CSV
# =====================================================================


@router.callback_query(F.data == "list_my_events")
async def handle_list_events(
    callback: CallbackQuery, state: FSMContext, session_maker: async_sessionmaker
):
    await callback.answer()
    await state.clear()
    await show_events_menu(callback.message, callback.from_user, session_maker)


@router.message(Command("export_dataset"))
async def cmd_export_dataset(message: Message, session_maker: async_sessionmaker):
    """
    Выгружает накопленный датасет выборов подарков всех пользователей в CSV-файл,
    готовый для обучения ML-модели (CatBoost / scikit-learn).
    """
    async with session_maker() as session:
        rows = (
            await session.execute(
                select(GiftMLDataset).join(Event, GiftMLDataset.event_id == Event.id)
                .where(Event.chat_id == message.chat.id)
                .order_by(GiftMLDataset.created_at.desc())
            )
        ).scalars().all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "id",
            "user_id",
            "recipient_gender",
            "recipient_age",
            "recipient_relation",
            "event_type",
            "recipient_tags",
            "gift_category",
            "gift_title",
            "gift_url",
            "interaction_type",
            "label",
            "created_at",
            "event_id", "gift_id", "budget_kzt", "rejection_reason",
        ]
    )
    for r in rows:
        writer.writerow(
            [
                r.id,
                r.user_id,
                r.recipient_gender,
                r.recipient_age or "",
                r.recipient_relation or "",
                r.event_type or "",
                "|".join(r.recipient_tags or []),
                r.gift_category or "",
                r.gift_title,
                r.gift_url or "",
                r.interaction_type,
                r.label,
                r.created_at.isoformat() if r.created_at else "",
                r.event_id, r.gift_id, r.budget_kzt, r.rejection_reason or "",
            ]
        )

    csv_bytes = output.getvalue().encode("utf-8-sig")
    doc = BufferedInputFile(csv_bytes, filename="gift_ml_dataset.csv")
    await message.answer_document(
        document=doc,
        caption=(
            f"📊 Накопленный датасет для обучения ML-модели подарков.\n"
            f"Всего записей взаимодействий: <b>{len(rows)}</b>"
        ),
    )


# =====================================================================
# 13. ПЛАНИРОВЩИК УВЕДОМЛЕНИЙ (APScheduler — 10:00 УТРА)
# =====================================================================


def describe_offset_ru(days_before: int) -> str:
    mapping = {
        30: "ровно через месяц (30 дней)",
        21: "через 3 недели (21 день)",
        14: "через 2 недели (14 дней)",
        7: "ровно через неделю (7 дней)",
        1: "уже ЗАВТРА (1 день)",
    }
    return mapping.get(days_before, f"через {days_before} дн.")


async def check_and_send_daily_notifications(bot, session_maker, today=None):
    token = preserve_messages.set(True)
    try:
        return await _check_and_send_daily_notifications(bot, session_maker, today)
    finally:
        preserve_messages.reset(token)


async def _check_and_send_daily_notifications(
    bot: Bot,
    session_maker: async_sessionmaker,
    today: Optional[date] = None,
) -> int:
    """
    Ежедневная задача планировщика (запускается в 10:00 утра):
    Проверяет все активные события и отправляет уведомления:
    - за 30 дней (месяц)
    - за 21 день (3 недели)
    - за 14 дней (2 недели)
    - за 7 дней (1 неделя)
    - за 1 день до даты
    Возвращает количество отправленных уведомлений.
    """
    if today is None:
        today = datetime.now(ZoneInfo(DEFAULT_TIMEZONE)).date()

    sent_count = 0
    async with session_maker() as session:
        stmt = (
            select(Event, User)
            .join(User, Event.user_id == User.id)
            .where(Event.notifications_enabled.is_(True))
        )
        rows = (await session.execute(stmt)).all()

        for event, user in rows:
            next_occ = get_next_occurrence(event.event_date, today)
            days_until = (next_occ - today).days

            if days_until not in REMINDER_OFFSETS_DAYS:
                continue

            # Проверяем, не отправляли ли уже это уведомление
            log_check = await session.execute(
                select(NotificationLog).where(
                    NotificationLog.event_id == event.id,
                    NotificationLog.target_occurrence_date == next_occ,
                    NotificationLog.days_before == days_until,
                )
            )
            if log_check.scalar_one_or_none() is not None:
                continue

            headline, turning_age = format_event_headline(
                event.title, event.event_date, event.has_birth_year, today
            )
            when_str = describe_offset_ru(days_until)

            text_lines = [
                f"⏰ <b>Напоминание о важной дате!</b>",
                f"Событие <b>«{headline}»</b> наступит <b>{when_str}</b> ({next_occ.strftime('%d.%m.%Y')}).",
            ]
            reminder_photo = event.gift_photo_file_id

            if event.gift_title or event.gift_url:
                text_lines.append("")
                text_lines.append(f"🎁 <b>Запланированный подарок:</b> {event.gift_title or 'Прикреплён'}")
                if event.gift_url:
                    text_lines.append(f"🔗 Ссылка: {event.gift_url}")
            else:
                # Если подарок ещё не выбран, сразу предлагаем топ-1 идею по гендерной группе
                top_recs = await get_smart_recommendations(
                    session, event, turning_age, limit=1
                )
                if top_recs:
                    best_gift, _ = top_recs[0]
                    reminder_photo = best_gift.image_file_id or best_gift.image_url
                    text_lines.append("")
                    text_lines.append(
                        f"💡 <b>Рекомендация подарка ({best_gift.category}):</b> {best_gift.title}"
                    )
                    if best_gift.shop_url:
                        text_lines.append(f"🔗 {best_gift.shop_url}")

            kb = build_event_card_keyboard(event)
            try:
                photo_delivered = False
                if reminder_photo:
                    try:
                        await bot.send_photo(
                            chat_id=event.chat_id,
                            photo=reminder_photo,
                            caption="\n".join(text_lines) if len("\n".join(text_lines)) <= 1024 else "🎁 Подарок к событию",
                            reply_markup=kb if len("\n".join(text_lines)) <= 1024 else None,
                        )
                        photo_delivered = len("\n".join(text_lines)) <= 1024
                    except TelegramBadRequest:
                        logger.info("Фото недоступно; отправляю текст напоминания, event_id=%s", event.id)
                if not photo_delivered:
                    await bot.send_message(
                        chat_id=event.chat_id,
                        text="\n".join(text_lines),
                        reply_markup=kb,
                        disable_web_page_preview=True,
                    )

                session.add(
                    NotificationLog(
                        event_id=event.id,
                        target_occurrence_date=next_occ,
                        days_before=days_until,
                    )
                )
                await session.commit()
                sent_count += 1
            except Exception as exc:
                logger.error(
                    "Ошибка при отправке уведомления пользователю %s по событию %s: %s",
                    user.telegram_id,
                    event.id,
                    exc,
                )

    return sent_count


# =====================================================================
# 14. ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ И ЗАПУСК БОТА
# =====================================================================


async def init_db_and_seed(engine, session_maker: async_sessionmaker):
    """Создаёт таблицы в PostgreSQL и заполняет стартовые правила подарков, если каталог пуст."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for statement in (
            "ALTER TABLE events ADD COLUMN IF NOT EXISTS gift_budget_kzt INTEGER",
            "ALTER TABLE events ADD COLUMN IF NOT EXISTS budget_configured BOOLEAN NOT NULL DEFAULT false",
            "ALTER TABLE gift_catalog ADD COLUMN IF NOT EXISTS estimated_min_kzt INTEGER",
            "ALTER TABLE gift_catalog ADD COLUMN IF NOT EXISTS image_file_id TEXT",
            "ALTER TABLE gift_catalog ADD COLUMN IF NOT EXISTS verified_product BOOLEAN NOT NULL DEFAULT false",
            "ALTER TABLE gift_catalog ADD COLUMN IF NOT EXISTS price_checked_on DATE",
            "ALTER TABLE gift_ml_dataset ADD COLUMN IF NOT EXISTS rejection_reason VARCHAR(32)",
            "ALTER TABLE gift_ml_dataset ADD COLUMN IF NOT EXISTS budget_kzt INTEGER",
        ):
            await conn.execute(text(statement))
        # Upgrade existing installations without moving personal events into groups.
        await conn.execute(text("ALTER TABLE events ADD COLUMN IF NOT EXISTS chat_id BIGINT"))
        await conn.execute(text(
            "UPDATE events SET chat_id = users.telegram_id FROM users "
            "WHERE events.user_id = users.id AND events.chat_id IS NULL"
        ))
        await conn.execute(text("ALTER TABLE events ALTER COLUMN chat_id SET NOT NULL"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_events_chat_id ON events (chat_id)"))

    async with session_maker() as session:
        product_file = Path(__file__).parent / "catalog" / "products.json"
        if product_file.exists():
            for item in json.loads(product_file.read_text()):
                known = (await session.execute(select(GiftCatalog.id).where(
                    GiftCatalog.shop_url == item["shop_url"],
                    GiftCatalog.created_by_user_id.is_(None),
                ).limit(1))).scalar_one_or_none()
                if known is None:
                    item["price_checked_on"] = date.fromisoformat(item["price_checked_on"])
                    session.add(GiftCatalog(**item, verified_product=True, target_gender="any",
                                           target_relations=[], base_score=2.0, is_approved=True))
            await session.commit()
        existing = set((await session.execute(select(GiftCatalog.title).where(GiftCatalog.created_by_user_id.is_(None)))).scalars())
        for title, category, tags, minimum in EXTRA_GIFT_IDEAS:
            if title not in existing:
                session.add(GiftCatalog(title=title, category=category, tags=tags, target_gender="any",
                    target_relations=[], min_age=0, max_age=120, base_score=2.0,
                    estimated_min_kzt=minimum, is_approved=True))
        await session.commit()
        count = len(existing)
        if count == 0:
            for item in DEFAULT_GIFT_RULES:
                session.add(GiftCatalog(**item))
            await session.commit()
            logger.info("Стартовый каталог правил подарков успешно загружен (%d позиций).", len(DEFAULT_GIFT_RULES))


async def main():
    engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
    session_maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    await init_db_and_seed(engine, session_maker)

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    bot.session.middleware(TrackInterfaceMessages(session_maker))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    # Настраиваем ежедневную отправку уведомлений ровно в 10:00 утра
    scheduler = AsyncIOScheduler(timezone=ZoneInfo(DEFAULT_TIMEZONE))
    scheduler.add_job(
        check_and_send_daily_notifications,
        trigger=CronTrigger(hour=10, minute=0, timezone=ZoneInfo(DEFAULT_TIMEZONE)),
        kwargs={"bot": bot, "session_maker": session_maker},
        id="daily_10am_family_dates_notifier",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Планировщик уведомлений запущен (ежедневно в 10:00, %s).", DEFAULT_TIMEZONE)

    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Открыть мои события"),
            BotCommand(command="new", description="➕ Добавить новую карточку"),
            BotCommand(command="events", description="Список сохранённых событий"),
            BotCommand(command="clean", description="🧹 Оставить актуальный список"),
        ])
        await dp.start_polling(bot, session_maker=session_maker)
    finally:
        scheduler.shutdown()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
