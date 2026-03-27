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
    "without": "только без подтвержденных отзывов",
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


def _build_home_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="👤 Профиль", callback_data="menu:profile"),
            InlineKeyboardButton(text="🚀 Начать парс", callback_data="menu:start"),
        ],
        [InlineKeyboardButton(text="💳 Моя подписка", callback_data="menu:subscription")],
    ]
    if _is_admin(user_id):
        rows.append([InlineKeyboardButton(text="🛠 Админ", callback_data="menu:admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_profile_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="🔑 Подписка", callback_data="menu:enter_code"),
            InlineKeyboardButton(text="💳 Моя подписка", callback_data="menu:subscription"),
        ],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:home")],
    ]
    if _is_admin(user_id):
        rows.insert(1, [InlineKeyboardButton(text="🛠 Админ", callback_data="menu:admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_subscription_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🔑 Ввести код", callback_data="menu:enter_code")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:home")],
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
    return (
        "👋 <b>OLX.ro Parser Bot</b>\n\n"
        f"Профиль: <b>{escape(user.get('full_name') or 'Пользователь')}</b>\n"
        f"Username: <b>{escape(username)}</b>\n"
        f"Подписка: <b>{escape(_format_subscription_value(user_id))}</b>\n\n"
        "Выберите действие ниже. Поиск доступен только с активной подпиской."
    )


def _profile_text(user_id: int) -> str:
    user = db.get_user(user_id) or {}
    username = f"@{user['username']}" if user.get("username") else "не указан"
    last_query = escape(user.get("last_query") or "нет")
    return (
        "👤 <b>Профиль</b>\n\n"
        f"ID: <code>{user_id}</code>\n"
        f"Имя: <b>{escape(user.get('full_name') or 'не указано')}</b>\n"
        f"Username: <b>{escape(username)}</b>\n"
        f"Регистрация: <b>{escape(_format_datetime(user.get('created_at')))}</b>\n"
        f"Последняя активность: <b>{escape(_format_datetime(user.get('last_seen_at')))}</b>\n"
        f"Поисков выполнено: <b>{user.get('total_searches', 0)}</b>\n"
        f"Последний запрос: <code>{last_query}</code>\n"
        f"Подписка: <b>{escape(_format_subscription_value(user_id))}</b>"
    )


def _subscription_text(user_id: int) -> str:
    user = db.get_user(user_id) or {}
    redeemed_code = user.get("redeemed_code") or "еще не активировали"
    return (
        "💳 <b>Моя подписка</b>\n\n"
        f"Статус: <b>{escape(_format_subscription_value(user_id))}</b>\n"
        f"Последний код: <code>{escape(redeemed_code)}</code>\n\n"
        "Нажмите <b>Ввести код</b>, чтобы активировать или продлить подписку."
    )


def _admin_text(admin_id: int) -> str:
    stats = db.get_stats()
    return (
        "🛠 <b>Админ-панель</b>\n\n"
        f"Админ ID: <code>{admin_id}</code>\n"
        f"Пользователей: <b>{stats['total_users']}</b>\n"
        f"Активных подписок: <b>{stats['active_subscriptions']}</b>\n"
        f"Всего кодов: <b>{stats['total_codes']}</b>\n"
        f"Доступных кодов: <b>{stats['available_codes']}</b>\n"
        f"Использовано кодов: <b>{stats['redeemed_codes']}</b>\n"
        f"Рассылок: <b>{stats['total_broadcasts']}</b>\n\n"
        "Выберите действие ниже."
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
    return (
        "🔒 <b>Доступ к парсингу закрыт</b>\n\n"
        "Чтобы начать поиск, активируйте подписку кодом в разделе <b>Моя подписка</b>."
    )


def _search_mode_label(search_mode: str) -> str:
    return "только категория" if search_mode == "category_only" else "по запросу"


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
        "max_check": int(data.get("max_check", config.MAX_LISTINGS_CHECK)),
        "max_pages": int(data.get("max_pages", config.MAX_PAGES)),
        "review_filter": review_filter,
    }


def _build_settings_text(query: str, settings: dict) -> str:
    return (
        "⚙️ <b>Настройки парсинга</b>\n\n"
        f"Режим: <b>{_search_mode_label(settings['search_mode'])}</b>\n"
        f"Запрос: <code>{escape(_display_query(query, settings))}</code>\n"
        f"Категория: <b>{CATEGORY_OPTIONS[settings['category_key']]['label']}</b>\n"
        f"Объявлений на проверку: <b>{settings['max_check']}</b>\n"
        f"Страниц поиска: <b>{settings['max_pages']}</b>\n"
        f"Отзывы: <b>{REVIEW_FILTER_LABELS[settings['review_filter']]}</b>\n\n"
        "Фильтр по отзывам работает только по подтвержденным данным, чтобы уменьшить ложные отсечки."
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
                ("without", "Без подтв. отзывов"),
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
        f"{hbold('Как пользоваться ботом:')}\n\n"
        "1. Откройте профиль или раздел подписки\n"
        "2. Активируйте код подписки\n"
        "3. Нажмите «Начать парс»\n"
        "4. Выберите режим: по запросу или только по категории\n"
        "5. Настройте категорию, страницы, количество объявлений и фильтр отзывов\n"
        "6. Запустите поиск\n\n"
        f"{hbold('Важно по фильтру отзывов:')}\n"
        "• «с отзывами» пропускает только продавцов с подтвержденным количеством отзывов > 0\n"
        "• «без отзывов» пропускает только продавцов, где явно найдено 0 отзывов\n"
        "• если отзывы не удалось определить, бот не будет ошибочно считать их отсутствующими\n\n"
        f"{hbold('Команды:')}\n"
        "/start — главное меню\n"
        "/search — начать поиск\n"
        "/admin — админ-панель\n"
        "/cancel — отменить текущее действие",
        parse_mode="HTML",
        reply_markup=_build_home_keyboard(message.from_user.id),
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
        "Выберите режим парсинга:\n"
        "• по запросу\n"
        "• только по выбранной категории",
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

    if action in {"home", "profile", "subscription", "admin"}:
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

    if action == "enter_code":
        await state.clear()
        await state.set_state(SubscriptionState.waiting_code)
        await callback.message.answer(
            "🔑 Отправьте код подписки одним сообщением.\n\n"
            f"Пример: {hcode('OLX-ABCD@7K9Q')}",
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
            "Выберите режим парсинга:\n"
            "• по запросу\n"
            "• только по выбранной категории",
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
        await callback.message.answer("🔎 Введите поисковый запрос:")
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

    if action == "mode" and len(parts) == 3:
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
        "max_check": existing.get("max_check", config.MAX_LISTINGS_CHECK),
        "max_pages": existing.get("max_pages", config.MAX_PAGES),
        "review_filter": existing.get("review_filter", "any"),
    }

    await state.set_state(SearchState.configuring_search)
    await state.update_data(query=query, **settings)

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
                f"♻️ Для {hbold(_search_target_text(query, settings))} новых объявлений пока нет.\n"
                f"Старых уже показанных пропущено: {hbold(str(skipped_seen))}\n"
                f"Память по ссылкам: {hbold(str(config.SEEN_LINK_TTL_HOURS))} ч.\n"
                f"Фильтр по отзывам: {hbold(REVIEW_FILTER_LABELS[settings['review_filter']])}",
                parse_mode="HTML",
            )
        else:
            await message.answer(
                f"😔 Для {hbold(_search_target_text(query, settings))} ничего не найдено.\n"
                f"Категория: {hbold(CATEGORY_OPTIONS[settings['category_key']]['label'])}\n"
                f"Фильтр по отзывам: {hbold(REVIEW_FILTER_LABELS[settings['review_filter']])}",
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
        f"✅ Найдено {hbold(str(total))} новых объявлений. "
        f"(показываю {shown} из {stats.listings_checked} проверенных)."
        + (
            f"\n♻️ Уже показывалось раньше и пропущено: {hbold(str(skipped_seen))}"
            if skipped_seen
            else ""
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
    lines = [
        f"🔌 Ищу: {hbold(_search_target_text(query, settings))}",
        (
            f"⚙️ До {settings['max_check']} объявлений, "
            f"{settings['max_pages']} стр., "
            f"режим: {_search_mode_label(settings['search_mode'])}, "
            f"категория: {CATEGORY_OPTIONS[settings['category_key']]['label']}, "
            f"отзывы: {REVIEW_FILTER_LABELS[settings['review_filter']]}"
        ),
    ]

    if not progress:
        lines.append("⏳ Готовлюсь к парсингу...")
        return "\n".join(lines)

    if progress["phase"] == "collect":
        lines.append(f"📄 Собираю объявления: страница {progress['page']}/{settings['max_pages']}")
        lines.append(f"📦 Уникальных объявлений: {progress['collected']}")
    elif progress["phase"] == "check":
        lines.append(f"🧪 Проверяю продавцов: {progress['checked']}/{progress['total']}")
        lines.append(f"🟢 Подошло сейчас: {progress['matched']}")
        current_title = progress.get("current_title")
        if current_title:
            lines.append(f"📌 Сейчас: <code>{escape(current_title[:60])}</code>")
    elif progress["phase"] == "done":
        lines.append(f"✅ Проверка завершена. Найдено: {progress['matched']}")

    lines.append(f"🌐 Запросов к OLX: {progress['requests_made']}")
    return "\n".join(lines)


def _build_completion_text(query: str, settings: dict, stats, found: int) -> str:
    lines = [
        f"✅ Поиск завершен: {hbold(_search_target_text(query, settings))}",
        f"🧭 Режим: {_search_mode_label(settings['search_mode'])}",
        f"🗂 Категория: {CATEGORY_OPTIONS[settings['category_key']]['label']}",
        f"🔎 Проверено объявлений: {stats.listings_checked}",
        f"📄 Страниц поиска: {stats.pages_loaded}",
        f"🌐 Запросов к OLX: {stats.requests_made}",
        f"⭐ Фильтр отзывов: {REVIEW_FILTER_LABELS[settings['review_filter']]}",
        f"🟢 Найдено онлайн: {found}",
        f"⏱ Время: {stats.elapsed:.1f} сек.",
    ]
    if getattr(stats, "already_seen_skipped", 0):
        lines.append(f"♻️ Уже показывались раньше: {stats.already_seen_skipped}")
        lines.append(f"🧠 Память ссылок: {config.SEEN_LINK_TTL_HOURS} ч.")
    return "\n".join(lines)


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
        "📋 <b>Сводка парсинга</b>",
        "",
        f"Цель: <b>{escape(_search_target_text(query, settings))}</b>",
        f"Категория: <b>{escape(CATEGORY_OPTIONS[settings['category_key']]['label'])}</b>",
        f"Проверено объявлений: <b>{stats.listings_checked}</b>",
        f"Страниц поиска: <b>{stats.pages_loaded}</b>",
        f"Запросов к OLX: <b>{stats.requests_made}</b>",
        f"Найдено новых объявлений: <b>{total_found}</b>",
        f"Фильтр отзывов: <b>{escape(REVIEW_FILTER_LABELS[settings['review_filter']])}</b>",
        f"Время: <b>{stats.elapsed:.1f} сек.</b>",
    ]
    if getattr(stats, "already_seen_skipped", 0):
        lines.append(f"Уже показанных ранее пропущено: <b>{stats.already_seen_skipped}</b>")

    lines.append("")
    lines.append(f"<b>Прямые ссылки:</b> <i>первые {shown} из {total_found}</i>")
    if not summary_list:
        lines.append("Нет новых ссылок.")
    else:
        for idx, listing in enumerate(summary_list, start=1):
            url = str(listing.get("url") or "").strip()
            if url:
                safe_url = escape(url, quote=True)
                lines.append(f"{idx}. <a href='{safe_url}'>{safe_url}</a>")
            else:
                lines.append(f"{idx}. ссылка не найдена")

    return "\n".join(lines)


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


async def on_shutdown(dispatcher: Dispatcher) -> None:
    await parser.close()
    db.close()
    logger.info("Shutdown completed")


async def main() -> None:
    dp.shutdown.register(on_shutdown)
    logger.info("Bot started")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
