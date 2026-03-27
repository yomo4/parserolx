# OLX.ro Telegram Parser Bot

Бот принимает поисковый запрос, ищет объявления на OLX.ro и возвращает только те, где продавец был онлайн сегодня.

## Требования

- Python 3.11
- Доступ в интернет с сервера
- Telegram bot token

## Переменные окружения

Обязательная:

- `BOT_TOKEN` - токен Telegram-бота

Необязательные:

- `MAX_PAGES` - сколько страниц поиска сканировать, по умолчанию `3`
- `MAX_LISTINGS_CHECK` - сколько объявлений дополнительно проверять, по умолчанию `30`
- `MAX_RESULTS` - сколько результатов отправлять, по умолчанию `10`
- `REQUEST_DELAY_MIN` - минимальная пауза между запросами, по умолчанию `0.8`
- `REQUEST_DELAY_MAX` - максимальная пауза между запросами, по умолчанию `1.6`

Шаблон уже лежит в `.env.example`. Можно сделать так:

```bash
cp .env.example .env
```

Пример `.env`:

```env
BOT_TOKEN=123456:example-token
MAX_PAGES=3
MAX_LISTINGS_CHECK=30
MAX_RESULTS=10
REQUEST_DELAY_MIN=0.8
REQUEST_DELAY_MAX=1.6
```

## Локальный запуск

```bash
python3.11 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
python bot.py
```

## Запуск на Ubuntu VDS через systemd

### 1. Установить пакеты

```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3-pip build-essential libxml2-dev libxslt1-dev
```

### 2. Создать сервисного пользователя и каталог проекта

```bash
sudo useradd --system --create-home --home-dir /opt/olx-bot --shell /usr/sbin/nologin olxbot
sudo mkdir -p /opt/olx-bot
sudo chown -R olxbot:olxbot /opt/olx-bot
```

### 3. Загрузить файлы проекта

Скопируй содержимое проекта в `/opt/olx-bot`.

### 4. Создать виртуальное окружение и установить зависимости

```bash
cd /opt/olx-bot
sudo -u olxbot python3.11 -m venv venv
sudo -u olxbot /opt/olx-bot/venv/bin/pip install --upgrade pip
sudo -u olxbot /opt/olx-bot/venv/bin/pip install -r /opt/olx-bot/requirements.txt
```

### 5. Создать `.env`

```bash
sudo -u olxbot nano /opt/olx-bot/.env
```

Минимум должен быть заполнен `BOT_TOKEN`.

### 6. Установить systemd unit

```bash
sudo cp /opt/olx-bot/olx-bot.service /etc/systemd/system/olx-bot.service
sudo systemctl daemon-reload
sudo systemctl enable olx-bot
sudo systemctl start olx-bot
```

### 7. Проверить статус и логи

```bash
sudo systemctl status olx-bot
sudo journalctl -u olx-bot -f
```

## Обновление проекта на сервере

```bash
cd /opt/olx-bot
sudo -u olxbot /opt/olx-bot/venv/bin/pip install -r /opt/olx-bot/requirements.txt
sudo systemctl restart olx-bot
```
