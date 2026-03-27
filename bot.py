"""
Telegram bot for OLX.ro parsing with subscriptions, admin tools, and inline UI.
"""

import asyncio
import logging
import time
from html import escape
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
    User,
)
from aiogram.utils.markdown import hbold, hcode

import config
from database import BotDatabase, iso_to_dt, utc_now
from olx_parser import OLXParser, SearchResult


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
parser = OLXParser()
db = BotDatabase(config.DB_PATH)


REVIEW_FILTER_LABELS = {
    "any": "любые",
    "with": "только с отзывами",
    "without": "только PRIVAT без отзывов",
}

CATEGORY_OPTIONS = {
    "all": {"label": "Все категории", "path": ""},
    "electronics": {"label": "Электроника", "path": "electronice-si-electrocasnice"},
    "auto": {"label": "Авто", "path": "auto-masini-moto-ambarcatiuni"},
    "realty": {"label": "Недвижимость", "path": "imobiliare"},
    "jobs": {"label": "Работа", "path": "locuri-de-munca"},
    "home": {"label": "Дом и сад", "path": "casa-gradina"},
    "kids": {"label": "Мама и ребенок", "path": "mama-si-copilul"},
    "pets": {"label": "Животные", "path": "animale-de-companie"},
    "fashion": {"label": "Мода", "path": "moda-frumusete"},
}


def _option_values(default_value: int, preset_values: tuple[int, ...]) -> tuple[int, ...]:
    values = set(preset_values)
    values.add(default_value)
    return tuple(sorted(values))


CHECK_OPTIONS = _option_values(config.MAX_LISTINGS_CHECK, (10, 20, 30, 50))
PAGE_OPTIONS = _option_values(config.MAX_PAGES, (1, 2, 3, 5))
ADMIN_CODE_DURATIONS = (7, 30, 90, 365)


class SearchState(StatesGroup):
    waiting_mode = State()
    waiting_query = State()
    configuring_search = State()


class SubscriptionState(StatesGroup):
    waiting_code = State()


class AdminState(StatesGroup):
    waiting_broadcast = State()


def _sync_user(user: Optional[User]) -> None:
    if not user:
        return
    db.upsert_user(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
    )


def _is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


def _has_access(user_id: int) -> bool:
    return _is_admin(user_id) or db.has_active_subscription(user_id)


def _format_datetime(value: Optional[str]) -> str:
    dt = iso_to_dt(value)
    if not dt:
        return "не указано"
    return dt.astimezone().strftime("%d.%m.%Y %H:%M")


def _format_subscription_value(user_id: int) -> str:
    user = db.get_user(user_id) or {}
    expires_at = iso_to_dt(user.get("subscription_until"))
    if not expires_at:
        return "не активна"
    now = utc_now()
    if expires_at <= now:
        return f"истекла {_format_datetime(user.get('subscription_until'))}"
    remaining = expires_at - now
    days = remaining.days
    hours = remaining.seconds // 3600
    return f"активна до {_format_datetime(user.get('subscription_until'))} ({days}д {hours}ч)"


def _access_status_text(user_id: int) -> str:
    if _is_admin(user_id):
        return "админ-доступ"
    if db.has_active_subscription(user_id):
        return "доступ открыт"
    return "нужна подписка"


def _build_block(title: str, lines: list[str], expandable: bool = False) -> str:
    tag = "blockquote expandable" if expandable else "blockquote"
    normalized: list[str] = []
    for line in lines:
        if not line:
            continue
        if isinstance(line, (list, tuple)):
            normalized.extend(str(item) for item in line if item)
        else:
            normalized.append(str(line))
    content = "\n".join(normalized)
    return f"<{tag}><b>{escape(title)}</b>\n{content}</{tag.split()[0]}>"


def _progress_bar(current: int, total: int, width: int = 10) -> str:
    if total <= 0:
        return "░" * width
    ratio = max(0.0, min(1.0, current / total))
    filled = int(round(ratio * width))
    return ("█" * filled) + ("░" * (width - filled))


def _build_home_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="⚡ Начать парс", callback_data="menu:start"),
            InlineKeyboardButton(text="👤 Профиль", callback_data="menu:profile"),
        ],
        [
            InlineKeyboardButton(text="💳 Подписка", callback_data="menu:subscription"),
            InlineKeyboardButton(text="ℹ️ Гайд", callback_data="menu:help"),
        ],
    ]
    if _is_admin(user_id):
        rows.append([InlineKeyboardButton(text="🛠 Админ", callback_data="menu:admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_profile_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="🔑 Активировать код", callback_data="menu:enter_code"),
            InlineKeyboardButton(text="💳 Подписка", callback_data="menu:subscription"),
        ],
        [
            InlineKeyboardButton(text="⚡ Начать парс", callback_data="menu:start"),
            InlineKeyboardButton(text="ℹ️ Гайд", callback_data="menu:help"),
        ],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:home")],
    ]
    if _is_admin(user_id):
        rows.insert(1, [InlineKeyboardButton(text="🛠 Админ", callback_data="menu:admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_subscription_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="🔑 Ввести код", callback_data="menu:enter_code"),
            InlineKeyboardButton(text="⚡ Начать парс", callback_data="menu:start"),
        ],
        [
            InlineKeyboardButton(text="ℹ️ Гайд", callback_data="menu:help"),
            InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:home"),
        ],
    ]
    if _is_admin(user_id):
        rows.insert(1, [InlineKeyboardButton(text="🛠 Админ", callback_data="menu:admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_help_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="⚡ Начать парс", callback_data="menu:start"),
            InlineKeyboardButton(text="💳 Подписка", callback_data="menu:subscription"),
        ],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:home")],
    ]
    if _is_admin(user_id):
        rows.insert(1, [InlineKeyboardButton(text="🛠 Админ", callback_data="menu:admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Код 7 дней", callback_data="admin:gen:7"),
                InlineKeyboardButton(text="Код 30 дней", callback_data="admin:gen:30"),
            ],
            [
                InlineKeyboardButton(text="Код 90 дней", callback_data="admin:gen:90"),
                InlineKeyboardButton(text="Код 365 дней", callback_data="admin:gen:365"),
            ],
            [
                InlineKeyboardButton(text="📢 Рассылка", callback_data="admin:broadcast"),
                InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats"),
            ],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:home")],
        ]
    )


def _build_search_entry_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔎 По запросу", callback_data="searchmode:query"),
                InlineKeyboardButton(text="🗂 Только категория", callback_data="searchmode:category"),
            ],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:home")],
        ]
    )


def _home_text(user_id: int) -> str:
    user = db.get_user(user_id) or {}
    username = f"@{user['username']}" if user.get("username") else "не указан"
    return "\n\n".join(
        [
            "✨ <b>SHAHRAY | OLX Parser</b>\n<i>Умный поиск активных продавцов на OLX.ro</i>",
            _build_block(
                "Ваш кабинет",
                [
                    f"Профиль: <b>{escape(user.get('full_name') or 'Пользователь')}</b>",
                    f"Username: <b>{escape(username)}</b>",
                    f"Доступ: <b>{escape(_access_status_text(user_id))}</b>",
                    f"Подписка: <b>{escape(_format_subscription_value(user_id))}</b>",
                ],
            ),
            _build_block(
                "Быстрый старт",
                [
                    "1. Активируйте код в разделе подписки",
                    "2. Нажмите «Начать парс»",
                    "3. Выберите режим, категорию и фильтры",
                ],
            ),
            _build_block(
                "Что внутри",
                [
                    "• поиск по запросу и по категории",
                    "• фильтры по PRIVAT, отзывам и лимитам",
                    "• антидубль уже показанных ссылок",
                    "• финальная сводка и прямые URL на лоты",
                ],
                expandable=True,
            ),
        ]
    )


def _profile_text(user_id: int) -> str:
    user = db.get_user(user_id) or {}
    username = f"@{user['username']}" if user.get("username") else "не указан"
    last_query = escape(user.get("last_query") or "нет")
    return "\n\n".join(
        [
            "👤 <b>Профиль</b>\n<i>Личный кабинет пользователя</i>",
            _build_block(
                "Паспорт аккаунта",
                [
                    f"ID: <code>{user_id}</code>",
                    f"Имя: <b>{escape(user.get('full_name') or 'не указано')}</b>",
                    f"Username: <b>{escape(username)}</b>",
                    f"Регистрация: <b>{escape(_format_datetime(user.get('created_at')))}</b>",
                    f"Последняя активность: <b>{escape(_format_datetime(user.get('last_seen_at')))}</b>",
                ],
            ),
            _build_block(
                "Активность",
                [
                    f"Поисков выполнено: <b>{user.get('total_searches', 0)}</b>",
                    f"Последний запрос: <code>{last_query}</code>",
                    f"Подписка: <b>{escape(_format_subscription_value(user_id))}</b>",
                ],
            ),
        ]
    )


def _subscription_text(user_id: int) -> str:
    user = db.get_user(user_id) or {}
    redeemed_code = user.get("redeemed_code") or "еще не активировали"
    return "\n\n".join(
        [
            "💳 <b>Подписка</b>\n<i>Управление доступом к парсингу</i>",
            _build_block(
                "Текущий статус",
                [
                    f"Доступ: <b>{escape(_access_status_text(user_id))}</b>",
                    f"Подписка: <b>{escape(_format_subscription_value(user_id))}</b>",
                    f"Последний код: <code>{escape(redeemed_code)}</code>",
                ],
            ),
            _build_block(
                "Что можно сделать",
                [
                    "• активировать новый код",
                    "• продлить уже активную подписку",
                    "• вернуться в поиск без ручного ввода команд",
                ],
            ),
        ]
    )


def _admin_text(admin_id: int) -> str:
    stats = db.get_stats()
    return "\n\n".join(
        [
            "🛠 <b>Админ-панель</b>\n<i>Управление подписками, кодами и рассылками</i>",
            _build_block(
                "KPI бота",
                [
                    f"Админ ID: <code>{admin_id}</code>",
                    f"Пользователей: <b>{stats['total_users']}</b>",
                    f"Активных подписок: <b>{stats['active_subscriptions']}</b>",
                    f"Всего кодов: <b>{stats['total_codes']}</b>",
                    f"Доступных кодов: <b>{stats['available_codes']}</b>",
                    f"Использовано кодов: <b>{stats['redeemed_codes']}</b>",
                    f"Рассылок: <b>{stats['total_broadcasts']}</b>",
                ],
            ),
            _build_block(
                "Быстрые действия",
                [
                    "• генерация кодов 7 / 30 / 90 / 365 дней",
                    "• массовая рассылка всем пользователям",
                    "• просмотр статистики без выхода из бота",
                ],
            ),
        ]
    )


def _help_text() -> str:
    return "\n\n".join(
        [
            "🧭 <b>Гайд по боту</b>\n<i>Коротко, быстро и без лишнего</i>",
            _build_block(
                "Как работать",
                [
                    "1. Активируйте код подписки",
                    "2. Нажмите «Начать парс»",
                    "3. Выберите режим: запрос или категория",
                    "4. Настройте лимиты и фильтры",
                    "5. Получите карточки и финальную сводку со ссылками",
                ],
            ),
            _build_block(
                "Фильтры по отзывам",
                [
                    "• «С отзывами» ищет продавцов с видимым рейтингом или отзывами",
                    "• «PRIVAT без отзывов» режет FIRMA/COMPANIE и оставляет частников без признаков отзывов",
                    "• если Telegram/OLX не отдают точный блок рейтинга, бот пишет это в лог",
                ],
                expandable=True,
            ),
            _build_block(
                "Команды",
                [
                    "/start — главное меню",
                    "/search — новый парсинг",
                    "/help — этот экран",
                    "/cancel — отменить текущее действие",
                    "/admin — админ-панель",
                ],
            ),
        ]
    )


async def _show_panel(message: Message, text: str, reply_markup: InlineKeyboardMarkup) -> None:
    try:
        await message.edit_text(text, parse_mode="HTML", reply_markup=reply_markup)
    except Exception:
        await message.answer(text, parse_mode="HTML", reply_markup=reply_markup)


async def _send_home(message: Message, user_id: int) -> None:
    await message.answer(
        _home_text(user_id),
        parse_mode="HTML",
        reply_markup=_build_home_keyboard(user_id),
    )


def _subscription_required_text() -> str:
    return "\n\n".join(
        [
            "🔒 <b>Доступ к парсингу закрыт</b>\n<i>Поиск доступен только с активной подпиской</i>",
            _build_block(
                "Что сделать дальше",
                [
                    "1. Откройте раздел подписки",
                    "2. Введите код активации",
                    "3. Вернитесь и запустите парсинг",
                ],
            ),
        ]
    )


def _search_mode_label(search_mode: str) -> str:
    return "только категория" if search_mode == "category_only" else "по запросу"


def _normalize_city_filter(value: str) -> str:
    cleaned = " ".join(str(value or "").split()).strip(" ,.")
    if not cleaned:
        return ""
    if cleaned.casefold() in {"любой", "любой город", "вся страна", "all", "any", "-"}:
        return ""
    return cleaned[:60]


def _city_filter_label(city_filter: str) -> str:
    return city_filter if city_filter else "вся страна"


def _city_button_label(city_filter: str) -> str:
    label = _city_filter_label(city_filter)
    return label if len(label) <= 18 else f"{label[:17]}…"


def _display_query(query: str, settings: dict) -> str:
    if settings.get("search_mode") == "category_only":
        return "не используется"
    cleaned = query.strip()
    return cleaned if cleaned else "не задан"


def _search_target_text(query: str, settings: dict) -> str:
    category_label = CATEGORY_OPTIONS[settings["category_key"]]["label"]
    if settings.get("search_mode") == "category_only":
        return f"категория {category_label}"
    return query


def _normalize_query_for_search(query: str, settings: dict) -> str:
    return "" if settings.get("search_mode") == "category_only" else query.strip()


def _build_search_key(query: str, settings: dict) -> str:
    normalized_query = _normalize_query_for_search(query, settings).casefold()
    return "|".join(
        (
            f"mode={settings['search_mode']}",
            f"category={settings['category_key']}",
            f"city={_normalize_city_filter(settings.get('city_filter', '')).casefold()}",
            f"reviews={settings['review_filter']}",
            f"query={normalized_query}",
        )
    )


def _extract_settings(data: dict) -> dict:
    category_key = str(data.get("category_key", "all"))
    if category_key not in CATEGORY_OPTIONS:
        category_key = "all"
    review_filter = str(data.get("review_filter", "any"))
    if review_filter not in REVIEW_FILTER_LABELS:
        review_filter = "any"
    search_mode = str(data.get("search_mode", "query"))
    if search_mode not in {"query", "category_only"}:
        search_mode = "query"
    return {
        "search_mode": search_mode,
        "category_key": category_key,
        "city_filter": _normalize_city_filter(str(data.get("city_filter", ""))),
        "max_check": int(data.get("max_check", config.MAX_LISTINGS_CHECK)),
        "max_pages": int(data.get("max_pages", config.MAX_PAGES)),
        "review_filter": review_filter,
    }


def _build_settings_text(query: str, settings: dict) -> str:
    return "\n\n".join(
        [
            "🎛 <b>Настройки парсинга</b>\n<i>Соберите сценарий поиска перед запуском</i>",
            _build_block(
                "Сценарий",
                [
                    f"Режим: <b>{_search_mode_label(settings['search_mode'])}</b>",
                    f"Запрос: <code>{escape(_display_query(query, settings))}</code>",
                    f"Категория: <b>{CATEGORY_OPTIONS[settings['category_key']]['label']}</b>",
                    f"Город: <b>{escape(_city_filter_label(settings['city_filter']))}</b>",
                ],
            ),
            _build_block(
                "Лимиты и фильтры",
                [
                    f"Объявлений на проверку: <b>{settings['max_check']}</b>",
                    f"Страниц поиска: <b>{settings['max_pages']}</b>",
                    f"Отзывы: <b>{REVIEW_FILTER_LABELS[settings['review_filter']]}</b>",
                ],
            ),
            _build_block(
                "Важно",
                [
                    "Режим «PRIVAT без отзывов» режет FIRMA/COMPANIE и не пропускает карточки с признаками рейтинга.",
                ],
                expandable=True,
            ),
        ]
    )


def _build_settings_keyboard(settings: dict) -> InlineKeyboardMarkup:
    def mark(selected: bool, label: str) -> str:
        return f"✅ {label}" if selected else label

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=mark(settings["search_mode"] == "query", "По запросу"),
                callback_data="cfg:mode:query",
            ),
            InlineKeyboardButton(
                text=mark(settings["search_mode"] == "category_only", "Только категория"),
                callback_data="cfg:mode:category_only",
            ),
        ],
        [
            InlineKeyboardButton(
                text=mark(settings["category_key"] == "all", CATEGORY_OPTIONS["all"]["label"]),
                callback_data="cfg:category:all",
            ),
            InlineKeyboardButton(
                text=mark(
                    settings["category_key"] == "electronics",
                    CATEGORY_OPTIONS["electronics"]["label"],
                ),
                callback_data="cfg:category:electronics",
            ),
        ],
        [
            InlineKeyboardButton(
                text=mark(settings["category_key"] == "auto", CATEGORY_OPTIONS["auto"]["label"]),
                callback_data="cfg:category:auto",
            ),
            InlineKeyboardButton(
                text=mark(settings["category_key"] == "realty", CATEGORY_OPTIONS["realty"]["label"]),
                callback_data="cfg:category:realty",
            ),
        ],
        [
            InlineKeyboardButton(
                text=mark(settings["category_key"] == "jobs", CATEGORY_OPTIONS["jobs"]["label"]),
                callback_data="cfg:category:jobs",
            ),
            InlineKeyboardButton(
                text=mark(settings["category_key"] == "home", CATEGORY_OPTIONS["home"]["label"]),
                callback_data="cfg:category:home",
            ),
        ],
        [
            InlineKeyboardButton(
                text=mark(settings["category_key"] == "kids", CATEGORY_OPTIONS["kids"]["label"]),
                callback_data="cfg:category:kids",
            ),
            InlineKeyboardButton(
                text=mark(settings["category_key"] == "pets", CATEGORY_OPTIONS["pets"]["label"]),
                callback_data="cfg:category:pets",
            ),
        ],
        [
            InlineKeyboardButton(
                text=mark(settings["category_key"] == "fashion", CATEGORY_OPTIONS["fashion"]["label"]),
                callback_data="cfg:category:fashion",
            ),
        ],
        [
            InlineKeyboardButton(
                text=f"🏙 {_city_button_label(settings.get('city_filter', ''))}",
                callback_data="cfg:city:set",
            ),
            InlineKeyboardButton(
                text="🧹 Сбросить" if settings.get("city_filter") else "🌍 Вся страна",
                callback_data="cfg:city:clear",
            ),
        ],
        [
            InlineKeyboardButton(
                text=mark(settings["max_check"] == value, str(value)),
                callback_data=f"cfg:check:{value}",
            )
            for value in CHECK_OPTIONS
        ],
        [
            InlineKeyboardButton(
                text=mark(settings["max_pages"] == value, f"{value} стр."),
                callback_data=f"cfg:pages:{value}",
            )
            for value in PAGE_OPTIONS
        ],
        [
            InlineKeyboardButton(
                text=mark(settings["review_filter"] == value, label),
                callback_data=f"cfg:reviews:{value}",
            )
            for value, label in (
                ("any", "Любые"),
                ("with", "С отзывами"),
                ("without", "PRIVAT без отзывов"),
            )
        ],
        [
            InlineKeyboardButton(text="🚀 Запустить парсинг", callback_data="cfg:start"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="cfg:cancel"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    _sync_user(message.from_user)
    await state.clear()
    await message.answer(
        _home_text(message.from_user.id),
        parse_mode="HTML",
        reply_markup=_build_home_keyboard(message.from_user.id),
    )


@dp.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext) -> None:
    _sync_user(message.from_user)
    await state.clear()
    await message.answer(
        _help_text(),
        parse_mode="HTML",
        reply_markup=_build_help_keyboard(message.from_user.id),
    )


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    _sync_user(message.from_user)
    current = await state.get_state()
    await state.clear()
    if current:
        await message.answer("❌ Текущее действие отменено.")
    else:
        await message.answer("Нет активного действия.")
    await _send_home(message, message.from_user.id)


@dp.message(Command("search"))
async def cmd_search(message: Message, state: FSMContext) -> None:
    _sync_user(message.from_user)
    if not _has_access(message.from_user.id):
        await message.answer(
            _subscription_required_text(),
            parse_mode="HTML",
            reply_markup=_build_subscription_keyboard(message.from_user.id),
        )
        return
    await state.set_state(SearchState.waiting_mode)
    await message.answer(
        "⚡ <b>Старт парсинга</b>\n\n"
        "<blockquote><b>Выберите режим</b>\n"
        "🔎 По запросу — поиск по ключевой фразе\n"
        "🗂 Только категория — просто свежая лента раздела</blockquote>",
        parse_mode="HTML",
        reply_markup=_build_search_entry_keyboard(),
    )


@dp.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    _sync_user(message.from_user)
    await state.clear()
    if not _is_admin(message.from_user.id):
        await message.answer("У вас нет доступа к админ-панели.")
        return
    await message.answer(
        _admin_text(message.from_user.id),
        parse_mode="HTML",
        reply_markup=_build_admin_keyboard(),
    )


@dp.callback_query(F.data.startswith("menu:"))
async def handle_menu_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.message:
        await callback.answer()
        return

    _sync_user(callback.from_user)
    action = callback.data.split(":", maxsplit=1)[1]

    if action in {"home", "profile", "subscription", "admin", "help"}:
        await state.clear()

    if action == "home":
        await _show_panel(
            callback.message,
            _home_text(callback.from_user.id),
            _build_home_keyboard(callback.from_user.id),
        )
        await callback.answer()
        return

    if action == "profile":
        await _show_panel(
            callback.message,
            _profile_text(callback.from_user.id),
            _build_profile_keyboard(callback.from_user.id),
        )
        await callback.answer()
        return

    if action == "subscription":
        await _show_panel(
            callback.message,
            _subscription_text(callback.from_user.id),
            _build_subscription_keyboard(callback.from_user.id),
        )
        await callback.answer()
        return

    if action == "help":
        await _show_panel(
            callback.message,
            _help_text(),
            _build_help_keyboard(callback.from_user.id),
        )
        await callback.answer()
        return

    if action == "enter_code":
        await state.clear()
        await state.set_state(SubscriptionState.waiting_code)
        await callback.message.answer(
            "🔑 <b>Активация подписки</b>\n\n"
            "<blockquote><b>Отправьте код одним сообщением</b>\n"
            f"Пример: {hcode('OLX-ABCD@7K9Q')}</blockquote>",
            parse_mode="HTML",
        )
        await callback.answer("Жду код подписки")
        return

    if action == "start":
        await state.clear()
        if not _has_access(callback.from_user.id):
            await _show_panel(
                callback.message,
                _subscription_required_text(),
                _build_subscription_keyboard(callback.from_user.id),
            )
            await callback.answer("Нужна активная подписка", show_alert=True)
            return
        await state.set_state(SearchState.waiting_mode)
        await callback.message.answer(
            "⚡ <b>Старт парсинга</b>\n\n"
            "<blockquote><b>Выберите режим</b>\n"
            "🔎 По запросу — поиск по ключевой фразе\n"
            "🗂 Только категория — свежая лента нужного раздела</blockquote>",
            parse_mode="HTML",
            reply_markup=_build_search_entry_keyboard(),
        )
        await callback.answer()
        return

    if action == "admin":
        if not _is_admin(callback.from_user.id):
            await callback.answer("Нет доступа", show_alert=True)
            return
        await _show_panel(
            callback.message,
            _admin_text(callback.from_user.id),
            _build_admin_keyboard(),
        )
        await callback.answer()
        return

    await callback.answer()


@dp.callback_query(F.data.startswith("searchmode:"))
async def handle_search_mode_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.message:
        await callback.answer()
        return

    _sync_user(callback.from_user)
    if not _has_access(callback.from_user.id):
        await state.clear()
        await _show_panel(
            callback.message,
            _subscription_required_text(),
            _build_subscription_keyboard(callback.from_user.id),
        )
        await callback.answer("Нужна активная подписка", show_alert=True)
        return

    mode = callback.data.split(":", maxsplit=1)[1]
    if mode == "query":
        await state.set_state(SearchState.waiting_query)
        await callback.message.answer(
            "🔎 <b>Поиск по запросу</b>\n\n"
            "<blockquote><b>Введите ключевую фразу</b>\nНапример: <code>iphone 15 pro</code></blockquote>",
            parse_mode="HTML",
        )
        await callback.answer()
        return

    if mode == "category":
        await _open_search_settings(callback.message, state, "", search_mode="category_only")
        await callback.answer("Выберите категорию и запустите парсинг")
        return

    await callback.answer()


@dp.callback_query(F.data.startswith("admin:"))
async def handle_admin_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.message:
        await callback.answer()
        return

    _sync_user(callback.from_user)
    if not _is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    parts = callback.data.split(":")
    action = parts[1]

    if action == "stats":
        await state.clear()
        await _show_panel(callback.message, _admin_text(callback.from_user.id), _build_admin_keyboard())
        await callback.answer("Статистика обновлена")
        return

    if action == "broadcast":
        await state.clear()
        await state.set_state(AdminState.waiting_broadcast)
        await callback.message.answer(
            "📢 Отправьте следующим сообщением текст рассылки для всех пользователей.\n"
            "Команда /cancel отменит рассылку."
        )
        await callback.answer("Жду текст рассылки")
        return

    if action == "gen" and len(parts) == 3 and parts[2].isdigit():
        duration_days = int(parts[2])
        if duration_days not in ADMIN_CODE_DURATIONS:
            await callback.answer("Неизвестный срок", show_alert=True)
            return
        code = db.generate_subscription_code(duration_days=duration_days, created_by=callback.from_user.id)
        await callback.message.answer(
            f"✅ Код создан\n\n"
            f"Срок: <b>{duration_days} дней</b>\n"
            f"Код: <code>{code}</code>",
            parse_mode="HTML",
        )
        await callback.answer("Код создан")
        return

    await callback.answer()


@dp.message(AdminState.waiting_broadcast, F.text & ~F.text.startswith("/"))
async def handle_admin_broadcast(message: Message, state: FSMContext) -> None:
    _sync_user(message.from_user)
    if not _is_admin(message.from_user.id):
        await state.clear()
        await message.answer("У вас нет доступа к рассылке.")
        return

    text = (message.text or "").strip()
    if not text:
        await message.answer("Пустая рассылка не отправлена.")
        return

    await state.clear()

    delivered = 0
    failed = 0
    user_ids = db.list_user_ids()
    logger.info("[ADMIN] Broadcast start | admin=%s | users=%d", message.from_user.id, len(user_ids))

    for user_id in user_ids:
        try:
            await bot.send_message(user_id, text)
            delivered += 1
            await asyncio.sleep(0.04)
        except Exception:
            failed += 1
            logger.exception("[ADMIN] Broadcast failed | target=%s", user_id)

    db.log_broadcast(
        admin_id=message.from_user.id,
        message_text=text,
        delivered_count=delivered,
        failed_count=failed,
    )

    await message.answer(
        f"📢 Рассылка завершена.\n\n"
        f"Успешно: <b>{delivered}</b>\n"
        f"Ошибок: <b>{failed}</b>",
        parse_mode="HTML",
        reply_markup=_build_admin_keyboard(),
    )


@dp.message(SubscriptionState.waiting_code, F.text & ~F.text.startswith("/"))
async def handle_subscription_code(message: Message, state: FSMContext) -> None:
    _sync_user(message.from_user)
    code = (message.text or "").strip()
    success, status_text, expires_at = db.redeem_subscription_code(message.from_user.id, code)
    await state.clear()

    if success and expires_at:
        logger.info("[SUB] Code redeemed | user=%s | code=%s | until=%s", message.from_user.id, code, expires_at)
        await message.answer(
            f"✅ {status_text}\n"
            f"Подписка активна до <b>{escape(_format_datetime(expires_at.isoformat()))}</b>.",
            parse_mode="HTML",
        )
    else:
        logger.info("[SUB] Code redeem failed | user=%s | code=%s | reason=%s", message.from_user.id, code, status_text)
        await message.answer(f"❌ {status_text}")

    await message.answer(
        _subscription_text(message.from_user.id),
        parse_mode="HTML",
        reply_markup=_build_subscription_keyboard(message.from_user.id),
    )


@dp.message(SearchState.waiting_mode, F.text & ~F.text.startswith("/"))
async def handle_search_mode_text(message: Message, state: FSMContext) -> None:
    _sync_user(message.from_user)
    if not _has_access(message.from_user.id):
        await state.clear()
        await message.answer(
            _subscription_required_text(),
            parse_mode="HTML",
            reply_markup=_build_subscription_keyboard(message.from_user.id),
        )
        return
    await _open_search_settings(message, state, (message.text or "").strip(), search_mode="query")


@dp.message(SearchState.waiting_query, F.text & ~F.text.startswith("/"))
async def handle_search_state(message: Message, state: FSMContext) -> None:
    _sync_user(message.from_user)
    if not _has_access(message.from_user.id):
        await state.clear()
        await message.answer(
            _subscription_required_text(),
            parse_mode="HTML",
            reply_markup=_build_subscription_keyboard(message.from_user.id),
        )
        return
    await _open_search_settings(message, state, (message.text or "").strip(), search_mode="query")


@dp.message(SearchState.configuring_search, F.text & ~F.text.startswith("/"))
async def handle_new_query_during_config(message: Message, state: FSMContext) -> None:
    _sync_user(message.from_user)
    if not _has_access(message.from_user.id):
        await state.clear()
        await message.answer(
            _subscription_required_text(),
            parse_mode="HTML",
            reply_markup=_build_subscription_keyboard(message.from_user.id),
        )
        return
    data = await state.get_data()
    if data.get("input_target") == "city":
        await state.update_data(
            city_filter=_normalize_city_filter(message.text or ""),
            input_target=None,
        )
        await _open_search_settings(
            message,
            state,
            str(data.get("query", "")),
            search_mode=str(data.get("search_mode", "query")),
        )
        return
    await _open_search_settings(message, state, (message.text or "").strip(), search_mode="query")


@dp.message(StateFilter(None), F.text & ~F.text.startswith("/"))
async def handle_text(message: Message, state: FSMContext) -> None:
    _sync_user(message.from_user)
    if not _has_access(message.from_user.id):
        await message.answer(
            _subscription_required_text(),
            parse_mode="HTML",
            reply_markup=_build_subscription_keyboard(message.from_user.id),
        )
        return
    await _open_search_settings(message, state, (message.text or "").strip(), search_mode="query")


@dp.callback_query(F.data.startswith("cfg:"))
async def handle_config_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.message:
        await callback.answer()
        return

    _sync_user(callback.from_user)
    current_state = await state.get_state()
    if current_state != SearchState.configuring_search.state:
        await callback.answer("Настройки устарели. Отправьте запрос заново.", show_alert=True)
        return

    data = await state.get_data()
    settings = _extract_settings(data)
    query = str(data.get("query", ""))
    normalized_query = _normalize_query_for_search(query, settings)
    if settings["search_mode"] == "query" and not normalized_query:
        await state.clear()
        await callback.answer("Запрос не найден. Отправьте его заново.", show_alert=True)
        return

    parts = callback.data.split(":")
    action = parts[1]

    if action == "cancel":
        await state.clear()
        await _show_panel(
            callback.message,
            _home_text(callback.from_user.id),
            _build_home_keyboard(callback.from_user.id),
        )
        await callback.answer("Поиск отменен")
        return

    if action == "start":
        if not _has_access(callback.from_user.id):
            await state.clear()
            await _show_panel(
                callback.message,
                _subscription_required_text(),
                _build_subscription_keyboard(callback.from_user.id),
            )
            await callback.answer("Нужна активная подписка", show_alert=True)
            return

        settings = _extract_settings(data)
        normalized_query = _normalize_query_for_search(query, settings)
        if settings["search_mode"] == "query" and len(normalized_query) < 2:
            await callback.answer("Введите запрос минимум из 2 символов.", show_alert=True)
            return
        if settings["search_mode"] == "category_only" and settings["category_key"] == "all":
            await callback.answer("Для режима по категории выберите конкретную категорию.", show_alert=True)
            return
        logger.info(
            "[UI] Search started | chat=%s | user=%s | query=%r | settings=%s",
            callback.message.chat.id,
            callback.from_user.id,
            normalized_query,
            settings,
        )
        await state.clear()
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.answer("Запускаю парсинг...")
        await run_search(callback.message, callback.from_user.id, normalized_query, settings)
        return

    if action == "city" and len(parts) == 3:
        city_action = parts[2]
        if city_action == "set":
            await state.update_data(input_target="city")
            await callback.message.answer(
                "🏙 <b>Фильтр по городу</b>\n\n"
                "<blockquote><b>Отправьте название города одним сообщением</b>\n"
                "Например: <code>Bucuresti</code>, <code>Iasi</code>, <code>Cluj-Napoca</code>\n"
                "Чтобы убрать фильтр, отправьте: <code>любой</code></blockquote>",
                parse_mode="HTML",
            )
            await callback.answer("Жду название города")
            return
        if city_action == "clear":
            await state.update_data(city_filter="", input_target=None)
        else:
            await callback.answer()
            return
    elif action == "mode" and len(parts) == 3:
        new_mode = parts[2]
        if new_mode == "query":
            await state.update_data(search_mode="query")
        elif new_mode == "category_only":
            await state.update_data(search_mode="category_only", query="")
        else:
            await callback.answer()
            return
    elif action == "check" and len(parts) == 3:
        await state.update_data(max_check=int(parts[2]))
    elif action == "pages" and len(parts) == 3:
        await state.update_data(max_pages=int(parts[2]))
    elif action == "reviews" and len(parts) == 3:
        await state.update_data(review_filter=parts[2])
    elif action == "category" and len(parts) == 3 and parts[2] in CATEGORY_OPTIONS:
        await state.update_data(category_key=parts[2])
    else:
        await callback.answer()
        return

    await state.update_data(input_target=None)
    updated_data = await state.get_data()
    settings = _extract_settings(updated_data)
    query = str(updated_data.get("query", ""))
    logger.info(
        "[UI] Settings updated | chat=%s | user=%s | query=%r | settings=%s",
        callback.message.chat.id,
        callback.from_user.id,
        query,
        settings,
    )

    await _show_panel(
        callback.message,
        _build_settings_text(query, settings),
        _build_settings_keyboard(settings),
    )
    await callback.answer("Настройки обновлены")


async def _open_search_settings(
    message: Message,
    state: FSMContext,
    query: str,
    search_mode: Optional[str] = None,
) -> None:
    existing = await state.get_data()
    effective_mode = search_mode or str(existing.get("search_mode", "query"))

    if effective_mode == "query" and len(query.strip()) < 2:
        await message.answer("❗ Запрос слишком короткий. Введите минимум 2 символа.")
        return
    if effective_mode == "category_only":
        query = ""

    old_message_id = existing.get("settings_message_id")
    if old_message_id:
        try:
            await bot.delete_message(message.chat.id, old_message_id)
        except Exception:
            pass

    settings = {
        "search_mode": effective_mode,
        "category_key": existing.get("category_key", "all"),
        "city_filter": _normalize_city_filter(str(existing.get("city_filter", ""))),
        "max_check": existing.get("max_check", config.MAX_LISTINGS_CHECK),
        "max_pages": existing.get("max_pages", config.MAX_PAGES),
        "review_filter": existing.get("review_filter", "any"),
    }

    await state.set_state(SearchState.configuring_search)
    await state.update_data(query=query, input_target=None, **settings)

    sent = await message.answer(
        _build_settings_text(query, settings),
        parse_mode="HTML",
        reply_markup=_build_settings_keyboard(settings),
    )
    await state.update_data(settings_message_id=sent.message_id)

    logger.info(
        "[UI] Settings opened | chat=%s | user=%s | query=%r | settings=%s",
        message.chat.id,
        message.from_user.id if message.from_user else "unknown",
        query,
        settings,
    )


async def run_search(message: Message, requester_id: int, query: str, settings: dict) -> None:
    db.record_search(requester_id, _search_target_text(query, settings))
    search_key = _build_search_key(query, settings)

    status_msg = await message.answer(
        _build_status_text(query, settings),
        parse_mode="HTML",
    )

    progress_state = {
        "last_text": "",
        "last_update": 0.0,
    }

    async def on_progress(progress: dict) -> None:
        text = _build_status_text(query, settings, progress)
        now = time.monotonic()
        force = progress["phase"] != "check" or progress.get("checked") in {
            0,
            progress.get("total", 0),
            1,
        }
        if not force and now - progress_state["last_update"] < 1.0:
            return
        if text == progress_state["last_text"]:
            return
        try:
            await status_msg.edit_text(text, parse_mode="HTML")
            progress_state["last_text"] = text
            progress_state["last_update"] = now
        except Exception:
            logger.exception("Failed to update search status")

    try:
        await bot.send_chat_action(message.chat.id, "typing")

        result: SearchResult = await parser.search(
            query,
            max_pages=settings["max_pages"],
            max_check=settings["max_check"],
            category_path=CATEGORY_OPTIONS[settings["category_key"]]["path"],
            city_filter=settings["city_filter"],
            review_filter=settings["review_filter"],
            progress_callback=on_progress,
        )

        fresh_listings, skipped_seen = db.filter_new_listings(
            requester_id,
            search_key,
            result.listings,
            config.SEEN_LINK_TTL_HOURS,
        )
        result.stats.already_seen_skipped = skipped_seen
        result.stats.listings_matched = len(fresh_listings)
        logger.info(
            "[DEDUPE] user=%s key=%r matched=%d new=%d skipped_seen=%d ttl_hours=%d",
            requester_id,
            search_key,
            len(result.listings),
            len(fresh_listings),
            skipped_seen,
            config.SEEN_LINK_TTL_HOURS,
        )
        result = SearchResult(listings=fresh_listings, stats=result.stats)

        await _show_search_result(message, requester_id, status_msg, query, settings, result)
    except Exception:
        logger.exception("Search failed | query=%s | user=%s", query, requester_id)
        try:
            await status_msg.edit_text("⚠️ Произошла ошибка. Попробуйте позже.")
        except Exception:
            pass
        await _send_home(message, requester_id)


async def _show_search_result(
    message: Message,
    requester_id: int,
    status_msg: Message,
    query: str,
    settings: dict,
    result: SearchResult,
) -> None:
    listings = result.listings
    stats = result.stats
    skipped_seen = getattr(stats, "already_seen_skipped", 0)

    if not listings:
        await status_msg.edit_text(
            _build_completion_text(query, settings, stats, found=0),
            parse_mode="HTML",
        )
        if skipped_seen:
            await message.answer(
                "\n\n".join(
                    [
                        "♻️ <b>Новых объявлений пока нет</b>",
                        _build_block(
                            "Антидубль",
                            [
                                f"Цель: <b>{escape(_search_target_text(query, settings))}</b>",
                                f"Уже показанных пропущено: <b>{skipped_seen}</b>",
                                f"Город: <b>{escape(_city_filter_label(settings['city_filter']))}</b>",
                                f"Память ссылок: <b>{config.SEEN_LINK_TTL_HOURS} ч.</b>",
                                f"Фильтр отзывов: <b>{REVIEW_FILTER_LABELS[settings['review_filter']]}</b>",
                            ],
                        ),
                    ]
                ),
                parse_mode="HTML",
            )
        else:
            await message.answer(
                "\n\n".join(
                    [
                        "😔 <b>По этому сценарию ничего не найдено</b>",
                        _build_block(
                            "Параметры поиска",
                            [
                                f"Цель: <b>{escape(_search_target_text(query, settings))}</b>",
                                f"Категория: <b>{CATEGORY_OPTIONS[settings['category_key']]['label']}</b>",
                                f"Город: <b>{escape(_city_filter_label(settings['city_filter']))}</b>",
                                f"Фильтр отзывов: <b>{REVIEW_FILTER_LABELS[settings['review_filter']]}</b>",
                            ],
                        ),
                    ]
                ),
                parse_mode="HTML",
            )
        await _send_home(message, requester_id)
        return

    total = len(listings)
    shown = min(total, config.MAX_RESULTS)
    await status_msg.edit_text(
        _build_completion_text(query, settings, stats, found=total),
        parse_mode="HTML",
    )
    await message.answer(
        "\n\n".join(
            [
                "✅ <b>Новые объявления найдены</b>",
                _build_block(
                    "Краткий итог",
                    [
                        f"Найдено новых: <b>{total}</b>",
                        f"Показываю сейчас: <b>{shown}</b> из <b>{stats.listings_checked}</b> проверенных",
                        f"Уже показывалось раньше: <b>{skipped_seen}</b>"
                        if skipped_seen
                        else "Дубликаты по памяти ссылок не обнаружены",
                    ],
                ),
            ]
        ),
        parse_mode="HTML",
    )

    for idx, listing in enumerate(listings[:config.MAX_RESULTS], start=1):
        caption = _format_listing(idx, listing)
        images: list[str] = listing.get("images") or []
        if images:
            media_group = [
                InputMediaPhoto(
                    media=img_url,
                    caption=caption if image_idx == 0 else None,
                    parse_mode="HTML" if image_idx == 0 else None,
                )
                for image_idx, img_url in enumerate(images[:10])
            ]
            try:
                await bot.send_media_group(message.chat.id, media=media_group)
            except Exception:
                await message.answer(caption, parse_mode="HTML", disable_web_page_preview=True)
        else:
            await message.answer(caption, parse_mode="HTML", disable_web_page_preview=True)
        await asyncio.sleep(0.5)

    await message.answer(
        _build_final_summary_text(query, settings, stats, listings, shown),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await _send_home(message, requester_id)


def _build_status_text(query: str, settings: dict, progress: dict | None = None) -> str:
    header = "⚡ <b>Парсинг запущен</b>\n<i>Бот собирает и проверяет объявления в реальном времени</i>"
    scenario = _build_block(
        "Сценарий",
        [
            f"Цель: <b>{escape(_search_target_text(query, settings))}</b>",
            f"Режим: <b>{_search_mode_label(settings['search_mode'])}</b>",
            f"Категория: <b>{CATEGORY_OPTIONS[settings['category_key']]['label']}</b>",
            f"Город: <b>{escape(_city_filter_label(settings['city_filter']))}</b>",
            f"Фильтр: <b>{REVIEW_FILTER_LABELS[settings['review_filter']]}</b>",
        ],
    )

    if not progress:
        phase_block = _build_block(
            "Статус",
            [
                "Стадия: <b>инициализация</b>",
                "Прогресс: <code>░░░░░░░░░░</code>",
                "Ожидание первого запроса к OLX...",
            ],
        )
        return "\n\n".join([header, scenario, phase_block])

    if progress["phase"] == "collect":
        progress_bar = _progress_bar(progress["page"], settings["max_pages"])
        phase_block = _build_block(
            "Сбор объявлений",
            [
                f"Прогресс: <code>{progress_bar}</code> {progress['page']}/{settings['max_pages']}",
                f"Уникальных карточек: <b>{progress['collected']}</b>",
                f"Запросов к OLX: <b>{progress['requests_made']}</b>",
            ],
        )
    elif progress["phase"] == "check":
        checked = progress["checked"]
        total = max(progress.get("total", 0), 1)
        progress_bar = _progress_bar(checked, total)
        phase_lines = [
            f"Прогресс: <code>{progress_bar}</code> {checked}/{total}",
            f"Подошло сейчас: <b>{progress['matched']}</b>",
            f"Запросов к OLX: <b>{progress['requests_made']}</b>",
        ]
        current_title = progress.get("current_title")
        if current_title:
            phase_lines.append(f"Сейчас проверяю: <code>{escape(current_title[:60])}</code>")
        phase_block = _build_block("Проверка продавцов", phase_lines)
    else:
        phase_block = _build_block(
            "Финализация",
            [
                f"Найдено: <b>{progress['matched']}</b>",
                f"Запросов к OLX: <b>{progress['requests_made']}</b>",
                "Подготавливаю итоговую выдачу...",
            ],
        )

    return "\n\n".join([header, scenario, phase_block])


def _build_completion_text(query: str, settings: dict, stats, found: int) -> str:
    lines = [
        "✅ <b>Парсинг завершен</b>\n<i>Сессия обработки успешно закончена</i>",
        _build_block(
            "Итоги",
            [
                f"Цель: <b>{escape(_search_target_text(query, settings))}</b>",
                f"Режим: <b>{_search_mode_label(settings['search_mode'])}</b>",
                f"Категория: <b>{CATEGORY_OPTIONS[settings['category_key']]['label']}</b>",
                f"Город: <b>{escape(_city_filter_label(settings['city_filter']))}</b>",
                f"Найдено онлайн: <b>{found}</b>",
                f"Фильтр отзывов: <b>{REVIEW_FILTER_LABELS[settings['review_filter']]}</b>",
            ],
        ),
        _build_block(
            "Техническая сводка",
            [
                f"Проверено объявлений: <b>{stats.listings_checked}</b>",
                f"Страниц поиска: <b>{stats.pages_loaded}</b>",
                f"Запросов к OLX: <b>{stats.requests_made}</b>",
                f"Время: <b>{stats.elapsed:.1f} сек.</b>",
            ],
            expandable=True,
        ),
    ]
    if getattr(stats, "already_seen_skipped", 0):
        lines.append(
            _build_block(
                "Антидубль",
                [
                    f"Уже показывались раньше: <b>{stats.already_seen_skipped}</b>",
                    f"Память ссылок: <b>{config.SEEN_LINK_TTL_HOURS} ч.</b>",
                ],
            )
        )
    return "\n\n".join(lines)


def _build_final_summary_text(
    query: str,
    settings: dict,
    stats,
    listings: list[dict],
    shown: int,
) -> str:
    total_found = len(listings)
    summary_list = listings[:shown]
    lines = [
        "📋 <b>Сводка парсинга</b>\n<i>Готовый отчет по текущему запуску</i>",
        _build_block(
            "Результат",
            [
                f"Цель: <b>{escape(_search_target_text(query, settings))}</b>",
                f"Категория: <b>{escape(CATEGORY_OPTIONS[settings['category_key']]['label'])}</b>",
                f"Город: <b>{escape(_city_filter_label(settings['city_filter']))}</b>",
                f"Проверено объявлений: <b>{stats.listings_checked}</b>",
                f"Страниц поиска: <b>{stats.pages_loaded}</b>",
                f"Запросов к OLX: <b>{stats.requests_made}</b>",
                f"Найдено новых объявлений: <b>{total_found}</b>",
                f"Фильтр отзывов: <b>{escape(REVIEW_FILTER_LABELS[settings['review_filter']])}</b>",
                f"Время: <b>{stats.elapsed:.1f} сек.</b>",
            ],
        ),
    ]
    if getattr(stats, "already_seen_skipped", 0):
        lines.append(
            _build_block(
                "Антидубль",
                [f"Уже показанных ранее пропущено: <b>{stats.already_seen_skipped}</b>"],
            )
        )

    if not summary_list:
        lines.append(_build_block("Прямые ссылки", ["Нет новых ссылок."]))
    else:
        link_lines = [f"Первые {shown} из {total_found}:"]
        for idx, listing in enumerate(summary_list, start=1):
            url = str(listing.get("url") or "").strip()
            if url:
                safe_url = escape(url, quote=True)
                link_lines.append(f"{idx}. <a href='{safe_url}'>{safe_url}</a>")
            else:
                link_lines.append(f"{idx}. ссылка не найдена")
        lines.append(_build_block("Прямые ссылки", link_lines, expandable=True))

    return "\n\n".join(lines)


def _format_listing(idx: int, listing: dict) -> str:
    title = _escape_html(listing.get("title") or "Без названия")
    lines = [f"<b>#{idx} {title}</b>"]

    if listing.get("price"):
        lines.append(f"💰 {_escape_html(listing['price'])}")
    if listing.get("location"):
        lines.append(f"📍 {_escape_html(listing['location'])}")
    if listing.get("seller_type"):
        normalized_type = str(listing["seller_type"]).lower()
        if normalized_type == "private":
            seller_type = "частник"
        elif normalized_type == "business":
            seller_type = "бизнес"
        else:
            seller_type = str(listing["seller_type"])
        lines.append(f"👤 Тип: {_escape_html(seller_type)}")
    if listing.get("seller_name"):
        lines.append(f"🙍 Продавец: {_escape_html(listing['seller_name'])}")
    if listing.get("seller_rating"):
        lines.append(f"🏅 Рейтинг: {_escape_html(listing['seller_rating'])}/5")
    if listing.get("last_online"):
        lines.append(f"🟢 {_escape_html(listing['last_online'])}")

    reviews_count = listing.get("reviews_count")
    if reviews_count is None:
        if listing.get("seller_rating"):
            lines.append("⭐ Отзывы: есть рейтинг, но OLX не отдал точное число")
        elif listing.get("has_review_signal"):
            lines.append("⭐ Отзывы: есть сигнал отзывов, точное число не определено")
        else:
            lines.append("⭐ Отзывы: без подтвержденных отзывов")
    else:
        lines.append(f"⭐ Отзывы: {_escape_html(reviews_count)}")

    desc = listing.get("description", "").strip()
    if desc:
        short = desc[:300] + ("..." if len(desc) > 300 else "")
        lines.append(f"\n📝 {_escape_html(short)}")

    url = listing.get("url", "")
    if url:
        safe_url = escape(url, quote=True)
        lines.append(f"\n🔗 <a href='{safe_url}'>Открыть объявление</a>")

    return "\n".join(lines)


def _escape_html(value: object) -> str:
    return escape(str(value), quote=False)


async def _configure_bot_presentation() -> None:
    commands = [
        BotCommand(command="start", description="Открыть главное меню"),
        BotCommand(command="search", description="Запустить новый парсинг"),
        BotCommand(command="help", description="Показать гайд по боту"),
        BotCommand(command="cancel", description="Отменить текущее действие"),
    ]
    if config.ADMIN_IDS:
        commands.append(BotCommand(command="admin", description="Открыть админ-панель"))

    try:
        await bot.set_my_commands(commands)
        await bot.set_my_description(
            "Умный Telegram-бот для поиска активных PRIVAT-продавцов на OLX.ro с фильтрами, "
            "антидублем и финальной сводкой по объявлениям."
        )
        await bot.set_my_short_description("Поиск активных продавцов и свежих объявлений на OLX.ro")
        logger.info("Bot presentation configured")
    except Exception:
        logger.exception("Failed to configure bot presentation")


async def on_shutdown(dispatcher: Dispatcher) -> None:
    await parser.close()
    db.close()
    logger.info("Shutdown completed")


async def main() -> None:
    dp.shutdown.register(on_shutdown)
    await _configure_bot_presentation()
    logger.info("Bot started")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
