"""
Telegram-бот для парсинга OLX.ro.
Принимает поисковый запрос и возвращает объявления от продавцов,
которые были онлайн сегодня.
"""

import asyncio
import logging
from html import escape

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InputMediaPhoto, Message
from aiogram.utils.markdown import hbold, hcode

import config
from olx_parser import OLXParser


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
parser = OLXParser()


class SearchState(StatesGroup):
    waiting_query = State()


@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        f"👋 {hbold('OLX.ro Parser Bot')}\n\n"
        "Отправьте ключевое слово для поиска, и я найду объявления,\n"
        f"где продавец был в сети {hbold('сегодня')}.\n\n"
        f"Пример: {hcode('iPhone 14')} или {hcode('laptop asus')}\n\n"
        "Команды:\n"
        "/search — начать поиск\n"
        "/help — помощь\n"
        "/cancel — отменить текущее действие",
        parse_mode="HTML",
    )


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        f"{hbold('ℹ️ Как пользоваться ботом:')}\n\n"
        "1. Напишите ключевое слово или фразу\n"
        "2. Бот просматривает страницы поиска OLX.ro\n"
        "3. Для каждого объявления проверяет, был ли продавец онлайн сегодня\n"
        "4. Возвращает только «свежие» объявления\n\n"
        f"{hbold('Настройки:')}\n"
        f"• Страниц поиска: {config.MAX_PAGES}\n"
        f"• Объявлений проверяется: до {config.MAX_LISTINGS_CHECK}\n"
        f"• Результатов выводится: до {config.MAX_RESULTS}\n\n"
        f"{hbold('Статус «онлайн сегодня» включает:')}\n"
        "• Activ acum (онлайн прямо сейчас)\n"
        "• Activ azi (активен сегодня)\n"
        "• Activ la HH:MM (был онлайн в указанное время сегодня)",
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


@dp.message(SearchState.waiting_query)
async def handle_search_state(message: Message, state: FSMContext) -> None:
    await state.clear()
    await run_search(message)


@dp.message(F.text & ~F.text.startswith("/"))
async def handle_text(message: Message) -> None:
    await run_search(message)


async def run_search(message: Message) -> None:
    query = (message.text or "").strip()
    if len(query) < 2:
        await message.answer("❗ Запрос слишком короткий. Введите минимум 2 символа.")
        return

    status_msg = await message.answer(
        f"🔍 Ищу: {hbold(query)}\n"
        f"⏳ Проверяю до {config.MAX_LISTINGS_CHECK} объявлений — подождите немного…",
        parse_mode="HTML",
    )

    try:
        await bot.send_chat_action(message.chat.id, "typing")

        listings = await parser.search(
            query,
            max_pages=config.MAX_PAGES,
            max_check=config.MAX_LISTINGS_CHECK,
        )

        try:
            await status_msg.delete()
        except Exception:
            pass

        if not listings:
            await message.answer(
                f"😔 По запросу {hbold(query)} не найдено объявлений\n"
                "от продавцов, бывших в сети сегодня.",
                parse_mode="HTML",
            )
            return

        total = len(listings)
        shown = min(total, config.MAX_RESULTS)
        await message.answer(
            f"✅ Найдено {hbold(str(total))} объявл. (показываю {shown}):",
            parse_mode="HTML",
        )

        for idx, listing in enumerate(listings[:config.MAX_RESULTS], start=1):
            caption = _format_listing(idx, listing)
            images: list[str] = listing.get("images") or []
            if images:
                media_group = [
                    InputMediaPhoto(
                        media=img_url,
                        caption=caption if i == 0 else None,
                        parse_mode="HTML" if i == 0 else None,
                    )
                    for i, img_url in enumerate(images[:10])
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

    except Exception:
        logger.exception("Ошибка при поиске '%s'", query)
        try:
            await status_msg.delete()
        except Exception:
            pass
        await message.answer("⚠️ Произошла ошибка. Попробуйте позже.")


def _format_listing(idx: int, listing: dict) -> str:
    title = _escape_html(listing.get("title") or "Без названия")
    lines = [f"<b>#{idx} {title}</b>"]

    if listing.get("price"):
        lines.append(f"💰 {_escape_html(listing['price'])}")
    if listing.get("location"):
        lines.append(f"📍 {_escape_html(listing['location'])}")
    if listing.get("last_online"):
        lines.append(f"🟢 {_escape_html(listing['last_online'])}")

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
