import os
from dotenv import load_dotenv

load_dotenv()

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
