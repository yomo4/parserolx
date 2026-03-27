"""
Telegram-бот для парсинга OLX.ro.
Принимает поисковый запрос и перед запуском показывает
инлайн-настройки парсинга.
"""

import asyncio
import logging
import time
from html import escape

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
)
from aiogram.utils.markdown import hbold, hcode

import config
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

REVIEW_FILTER_LABELS = {
    "any": "любые",
    "with": "только с отзывами",
    "without": "без отзывов",
}


def _option_values(default_value: int, preset_values: tuple[int, ...]) -> tuple[int, ...]:
    values = set(preset_values)
    values.add(default_value)
    return tuple(sorted(values))


CHECK_OPTIONS = _option_values(config.MAX_LISTINGS_CHECK, (10, 20, 30, 50))
PAGE_OPTIONS = _option_values(config.MAX_PAGES, (1, 2, 3, 5))


class SearchState(StatesGroup):
    waiting_query = State()
    configuring_search = State()


@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        f"👋 {hbold('OLX.ro Parser Bot')}\n\n"
        "Отправьте ключевое слово для поиска.\n"
        "После этого бот покажет инлайн-настройки:\n"
        "сколько объявлений проверять, сколько страниц сканировать\n"
        "и искать ли только продавцов с отзывами.\n\n"
        f"Пример: {hcode('iPhone 14')} или {hcode('laptop asus')}\n\n"
        "Команды:\n"
        "/search — ввести запрос\n"
        "/help — помощь\n"
        "/cancel — отменить текущее действие",
        parse_mode="HTML",
    )


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        f"{hbold('ℹ️ Как пользоваться ботом:')}\n\n"
        "1. Отправьте поисковый запрос\n"
        "2. Выберите инлайн-настройки парсинга\n"
        "3. Запустите поиск кнопкой «Запустить парсинг»\n"
        "4. Бот соберет объявления и проверит продавцов на онлайн сегодня\n\n"
        f"{hbold('Что можно настроить:')}\n"
        "• сколько объявлений дополнительно проверять\n"
        "• сколько страниц поиска OLX просматривать\n"
        "• брать любых продавцов, только с отзывами или без отзывов\n\n"
        f"{hbold('Статус «онлайн сегодня» включает:')}\n"
        "• Activ acum\n"
        "• Activ azi\n"
        "• Activ la HH:MM\n\n"
        f"{hbold('Текущие дефолты:')}\n"
        f"• Страниц поиска: {config.MAX_PAGES}\n"
        f"• Объявлений на проверку: {config.MAX_LISTINGS_CHECK}\n"
        f"• Результатов в выдаче: до {config.MAX_RESULTS}",
        parse_mode="HTML",
    )


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    await state.clear()
    if current:
        await message.answer("❌ Действие отменено.")
    else:
        await message.answer("Нет активного действия.")


@dp.message(Command("search"))
async def cmd_search(message: Message, state: FSMContext) -> None:
    await state.set_state(SearchState.waiting_query)
    await message.answer("🔎 Введите поисковый запрос:")


@dp.message(SearchState.waiting_query, F.text & ~F.text.startswith("/"))
async def handle_search_state(message: Message, state: FSMContext) -> None:
    await _open_search_settings(message, state, (message.text or "").strip())


@dp.message(SearchState.configuring_search, F.text & ~F.text.startswith("/"))
async def handle_new_query_during_config(message: Message, state: FSMContext) -> None:
    await _open_search_settings(message, state, (message.text or "").strip())


@dp.message(StateFilter(None), F.text & ~F.text.startswith("/"))
async def handle_text(message: Message, state: FSMContext) -> None:
    await _open_search_settings(message, state, (message.text or "").strip())


@dp.callback_query(F.data.startswith("cfg:"))
async def handle_config_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.message:
        await callback.answer()
        return

    current_state = await state.get_state()
    if current_state != SearchState.configuring_search.state:
        await callback.answer("Настройки уже устарели. Отправьте запрос заново.", show_alert=True)
        return

    data = await state.get_data()
    query = data.get("query", "")
    if not query:
        await state.clear()
        await callback.answer("Запрос не найден. Отправьте его заново.", show_alert=True)
        return

    parts = callback.data.split(":")
    action = parts[1]

    if action == "cancel":
        await state.clear()
        await callback.message.edit_text("❌ Поиск отменен.")
        await callback.answer()
        return

    if action == "start":
        settings = _extract_settings(data)
        logger.info(
            "[UI] Запуск поиска | chat=%s | user=%s | query=%r | settings=%s",
            callback.message.chat.id,
            callback.from_user.id,
            query,
            settings,
        )
        await state.clear()
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.answer("Запускаю парсинг...")
        await run_search(callback.message, query, settings)
        return

    if action == "check" and len(parts) == 3:
        await state.update_data(max_check=int(parts[2]))
    elif action == "pages" and len(parts) == 3:
        await state.update_data(max_pages=int(parts[2]))
    elif action == "reviews" and len(parts) == 3:
        await state.update_data(review_filter=parts[2])
    else:
        await callback.answer()
        return

    updated_data = await state.get_data()
    settings = _extract_settings(updated_data)
    logger.info(
        "[UI] Обновлены настройки | chat=%s | user=%s | query=%r | settings=%s",
        callback.message.chat.id,
        callback.from_user.id,
        query,
        settings,
    )

    try:
        await callback.message.edit_text(
            _build_settings_text(query, settings),
            parse_mode="HTML",
            reply_markup=_build_settings_keyboard(settings),
        )
    except Exception:
        logger.exception("Не удалось обновить сообщение с настройками")
    await callback.answer("Настройки обновлены")


async def _open_search_settings(message: Message, state: FSMContext, query: str) -> None:
    if len(query) < 2:
        await message.answer("❗ Запрос слишком короткий. Введите минимум 2 символа.")
        return

    existing = await state.get_data()
    old_message_id = existing.get("settings_message_id")
    if old_message_id:
        try:
            await bot.delete_message(message.chat.id, old_message_id)
        except Exception:
            pass

    settings = {
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
        "[UI] Открыты настройки | chat=%s | user=%s | query=%r | settings=%s",
        message.chat.id,
        message.from_user.id if message.from_user else "unknown",
        query,
        settings,
    )


def _extract_settings(data: dict) -> dict:
    return {
        "max_check": int(data.get("max_check", config.MAX_LISTINGS_CHECK)),
        "max_pages": int(data.get("max_pages", config.MAX_PAGES)),
        "review_filter": str(data.get("review_filter", "any")),
    }


def _build_settings_text(query: str, settings: dict) -> str:
    return (
        "⚙️ <b>Настройки парсинга</b>\n\n"
        f"Запрос: <code>{escape(query)}</code>\n"
        f"Объявлений на проверку: <b>{settings['max_check']}</b>\n"
        f"Страниц поиска: <b>{settings['max_pages']}</b>\n"
        f"Отзывы: <b>{REVIEW_FILTER_LABELS[settings['review_filter']]}</b>\n\n"
        "Выберите параметры ниже и нажмите <b>«Запустить парсинг»</b>."
    )


def _build_settings_keyboard(settings: dict) -> InlineKeyboardMarkup:
    def mark(selected: bool, label: str) -> str:
        return f"✅ {label}" if selected else label

    rows: list[list[InlineKeyboardButton]] = [
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
                ("without", "Без отзывов"),
            )
        ],
        [
            InlineKeyboardButton(text="🚀 Запустить парсинг", callback_data="cfg:start"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="cfg:cancel"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def run_search(message: Message, query: str, settings: dict) -> None:
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
            logger.exception("Не удалось обновить статус поиска")

    try:
        await bot.send_chat_action(message.chat.id, "typing")

        result: SearchResult = await parser.search(
            query,
            max_pages=settings["max_pages"],
            max_check=settings["max_check"],
            review_filter=settings["review_filter"],
            progress_callback=on_progress,
        )

        await _show_search_result(message, status_msg, query, settings, result)
    except Exception:
        logger.exception("Ошибка при поиске '%s'", query)
        try:
            await status_msg.edit_text("⚠️ Произошла ошибка. Попробуйте позже.")
        except Exception:
            pass


async def _show_search_result(
    message: Message,
    status_msg: Message,
    query: str,
    settings: dict,
    result: SearchResult,
) -> None:
    listings = result.listings
    stats = result.stats

    if not listings:
        await status_msg.edit_text(
            _build_completion_text(query, settings, stats, found=0),
            parse_mode="HTML",
        )
        await message.answer(
            f"😔 По запросу {hbold(query)} ничего не найдено.\n"
            f"Фильтр по отзывам: {hbold(REVIEW_FILTER_LABELS[settings['review_filter']])}",
            parse_mode="HTML",
        )
        return

    total = len(listings)
    shown = min(total, config.MAX_RESULTS)
    await status_msg.edit_text(
        _build_completion_text(query, settings, stats, found=total),
        parse_mode="HTML",
    )
    await message.answer(
        f"✅ Найдено {hbold(str(total))} объявл. "
        f"(показываю {shown} из {stats.listings_checked} проверенных).",
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
                await message.answer(
                    caption,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
        else:
            await message.answer(
                caption,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        await asyncio.sleep(0.5)


def _build_status_text(query: str, settings: dict, progress: dict | None = None) -> str:
    lines = [
        f"🔍 Ищу: {hbold(query)}",
        (
            f"⚙️ До {settings['max_check']} объявлений, "
            f"{settings['max_pages']} стр., "
            f"отзывы: {REVIEW_FILTER_LABELS[settings['review_filter']]}"
        ),
    ]

    if not progress:
        lines.append("⏳ Готовлюсь к парсингу…")
        return "\n".join(lines)

    if progress["phase"] == "collect":
        lines.append(
            f"📄 Собираю объявления: страница {progress['page']}/{settings['max_pages']}"
        )
        lines.append(f"📦 Уникальных объявлений: {progress['collected']}")
    elif progress["phase"] == "check":
        lines.append(
            f"🧪 Проверяю продавцов: {progress['checked']}/{progress['total']}"
        )
        lines.append(f"🟢 Подошло сейчас: {progress['matched']}")
        current_title = progress.get("current_title")
        if current_title:
            title = escape(current_title[:60])
            lines.append(f"📌 Сейчас: <code>{title}</code>")
    elif progress["phase"] == "done":
        lines.append(f"✅ Проверка завершена. Найдено: {progress['matched']}")

    lines.append(f"🌐 Запросов к OLX: {progress['requests_made']}")
    return "\n".join(lines)


def _build_completion_text(query: str, settings: dict, stats, found: int) -> str:
    lines = [
        f"✅ Поиск завершен: {hbold(query)}",
        f"🔎 Проверено объявлений: {stats.listings_checked}",
        f"📄 Страниц поиска: {stats.pages_loaded}",
        f"🌐 Запросов к OLX: {stats.requests_made}",
        f"⭐ Фильтр отзывов: {REVIEW_FILTER_LABELS[settings['review_filter']]}",
        f"🟢 Найдено онлайн: {found}",
        f"⏱ Время: {stats.elapsed:.1f} сек.",
    ]
    return "\n".join(lines)


def _format_listing(idx: int, listing: dict) -> str:
    title = _escape_html(listing.get("title") or "Без названия")
    lines = [f"<b>#{idx} {title}</b>"]

    if listing.get("price"):
        lines.append(f"💰 {_escape_html(listing['price'])}")
    if listing.get("location"):
        lines.append(f"📍 {_escape_html(listing['location'])}")
    if listing.get("last_online"):
        lines.append(f"🟢 {_escape_html(listing['last_online'])}")

    reviews_count = listing.get("reviews_count")
    if reviews_count is None:
        lines.append("⭐ Отзывы: нет")
    else:
        lines.append(f"⭐ Отзывы: {_escape_html(reviews_count)}")

    desc = listing.get("description", "").strip()
    if desc:
        short = desc[:300] + ("…" if len(desc) > 300 else "")
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
    logger.info("HTTP-сессия парсера закрыта")


async def main() -> None:
    dp.shutdown.register(on_shutdown)
    logger.info("Бот запущен")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
