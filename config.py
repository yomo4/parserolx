import os

from dotenv import load_dotenv


load_dotenv()


def _parse_admin_ids(raw: str) -> tuple[int, ...]:
    values: list[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            values.append(int(chunk))
    return tuple(values)


BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан в .env файле")

# Сколько страниц поиска OLX сканировать
MAX_PAGES: int = int(os.getenv("MAX_PAGES", "3"))

# Сколько отдельных страниц объявлений проверять на статус онлайн
MAX_LISTINGS_CHECK: int = int(os.getenv("MAX_LISTINGS_CHECK", "30"))

# Сколько результатов отправлять пользователю
MAX_RESULTS: int = int(os.getenv("MAX_RESULTS", "10"))

# Задержка между запросами (секунды)
REQUEST_DELAY_MIN: float = float(os.getenv("REQUEST_DELAY_MIN", "0.8"))
REQUEST_DELAY_MAX: float = float(os.getenv("REQUEST_DELAY_MAX", "1.6"))

# Админы бота
ADMIN_IDS: tuple[int, ...] = _parse_admin_ids(os.getenv("ADMIN_IDS", "8458119704"))

# SQLite база данных
DB_PATH: str = os.getenv("DB_PATH", "data/bot.sqlite3")

# Файл или папка с cookies OLX
COOKIE_SOURCE: str = os.getenv("COOKIE_SOURCE", "cookie olx")

# Через сколько часов забывать уже показанные ссылки
SEEN_LINK_TTL_HOURS: int = int(os.getenv("SEEN_LINK_TTL_HOURS", "72"))
