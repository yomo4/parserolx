"""
OLX.ro async parser.
Ищет объявления по запросу и фильтрует по статусу продавца «онлайн сегодня».
"""

import asyncio
import json
import logging
import random
import re
import socket
import time
from typing import Optional
from urllib.parse import quote

import aiohttp
from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

import config

logger = logging.getLogger(__name__)

# ─── Заголовки ────────────────────────────────────────────────────────────────

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:125.0) Gecko/20100101 Firefox/125.0",
]

# Паттерн «активен сегодня» на OLX.ro (румынский)
# "Activ acum"  → онлайн прямо сейчас
# "Activ azi"   → активен сегодня
# "Activ la HH:MM" (без упоминания дня) → время сегодня
_ONLINE_TODAY_RE = re.compile(
    r"Activ\s+(?:acum|azi(?:\s+la\s+\d{1,2}:\d{2})?|la\s+\d{1,2}:\d{2})",
    re.IGNORECASE,
)

# Паттерн для извлечения статуса из произвольного текста страницы
_ONLINE_EXTRACT_RE = re.compile(
    r"Activ\s+(?:acum|azi(?:\s+la\s+\d{1,2}:\d{2})?|la\s+\d{1,2}:\d{2}|ieri|acum\s+\d+\s+\w+)",
    re.IGNORECASE,
)


class OLXParser:
    BASE_URL = "https://www.olx.ro"
    SEARCH_URL = "https://www.olx.ro/oferte/q-{query}/"

    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None

    @staticmethod
    def _build_search_slug(query: str) -> str:
        normalized = re.sub(r"\s+", "-", query.strip())
        return quote(normalized, safe="")

    # ─── HTTP ─────────────────────────────────────────────────────────────────

    def _build_headers(self) -> dict:
        return {
            "User-Agent": random.choice(_USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "ro-RO,ro;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Cache-Control": "max-age=0",
        }

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(
                    limit=5,
                    family=socket.AF_INET,
                    ssl=False,
                    ttl_dns_cache=300,
                ),
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    def _fetch_with_curl(self, url: str) -> Optional[str]:
        try:
            response = curl_requests.get(
                url,
                headers=self._build_headers(),
                impersonate="chrome124",
                timeout=30,
                allow_redirects=True,
                verify=False,
            )
            if response.status_code == 200:
                text = response.text
                logger.debug("CURL OK %d | %d bytes | %s",
                             response.status_code, len(text), url)
                return text
            logger.warning("CURL HTTP %d | %s", response.status_code, url)
        except Exception as exc:
            logger.error("CURL ERROR | %r | %s", exc, url)
        return None

    async def _fetch(self, url: str) -> Optional[str]:
        session = await self._get_session()
        t0 = time.monotonic()
        logger.debug("GET %s", url)
        try:
            async with session.get(url, headers=self._build_headers()) as resp:
                elapsed = time.monotonic() - t0
                if resp.status == 200:
                    text = await resp.text()
                    logger.debug("OK %d | %.2fs | %d bytes | %s",
                                 resp.status, elapsed, len(text), url)
                    return text
                logger.warning("HTTP %d | %.2fs | %s", resp.status, elapsed, url)
                return None
        except asyncio.TimeoutError:
            logger.error("TIMEOUT (%.2fs) | %s", time.monotonic() - t0, url)
        except aiohttp.ClientError as exc:
            logger.warning("AIOHTTP ERROR | %r | %s", exc, url)
            text = await asyncio.to_thread(self._fetch_with_curl, url)
            if text:
                return text
        return None

    # ─── Страница поиска ──────────────────────────────────────────────────────

    async def _fetch_listings_page(self, query: str, page: int) -> list[dict]:
        slug = self._build_search_slug(query)
        url = self.SEARCH_URL.format(query=slug)
        if page > 1:
            url += f"?page={page}"

        logger.info("[SEARCH] Запрос стр.%d → %s", page, url)
        html = await self._fetch(url)
        if not html:
            logger.warning("[SEARCH] Стр.%d — пустой ответ", page)
            return []
        cards = self._parse_listing_cards(html)
        logger.info("[SEARCH] Стр.%d — найдено карточек: %d", page, len(cards))
        return cards

    def _parse_listing_cards(self, html: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")

        # Сначала пробуем JSON из Next.js (надёжнее HTML-парсинга)
        items = self._parse_nextdata_listings(soup)
        if items:
            logger.debug("[PARSE] Источник: __NEXT_DATA__ (%d карточек)", len(items))
            return items

        # HTML fallback
        logger.debug("[PARSE] __NEXT_DATA__ не найден — переходим на HTML-парсинг")
        items = self._parse_html_cards(soup)
        logger.debug("[PARSE] Источник: HTML (%d карточек)", len(items))
        return items

    # — Next.js __NEXT_DATA__ —

    def _get_nextdata(self, soup: BeautifulSoup) -> Optional[dict]:
        tag = soup.find("script", {"id": "__NEXT_DATA__"})
        if not tag or not tag.string:
            return None
        try:
            return json.loads(tag.string)
        except json.JSONDecodeError:
            return None

    def _parse_nextdata_listings(self, soup: BeautifulSoup) -> list[dict]:
        data = self._get_nextdata(soup)
        if not data:
            logger.debug("[NEXT_DATA] Тег __NEXT_DATA__ не найден")
            return []
        results = []
        try:
            page_props = data["props"]["pageProps"]
            ads = (
                page_props.get("ads")
                or page_props.get("data", {}).get("ads")
                or []
            )
            logger.debug("[NEXT_DATA] Объявлений в JSON: %d", len(ads))
            for ad in ads:
                url = ad.get("url", "")
                if url and not url.startswith("http"):
                    url = self.BASE_URL + url
                price_raw = ad.get("price", {})
                price = (
                    price_raw.get("displayValue", "") if isinstance(price_raw, dict)
                    else str(price_raw)
                )
                loc_raw = ad.get("location", {})
                location = (
                    loc_raw.get("name", "") if isinstance(loc_raw, dict)
                    else str(loc_raw)
                )
                results.append({
                    "title": ad.get("title", ""),
                    "price": price,
                    "location": location,
                    "url": url,
                    "last_online": None,
                })
        except (KeyError, TypeError) as exc:
            logger.warning("[NEXT_DATA] Ошибка разбора ads: %s", exc)
        return results

    # — HTML card fallback —

    def _parse_html_cards(self, soup: BeautifulSoup) -> list[dict]:
        results = []
        cards = soup.find_all("div", {"data-cy": "l-card"})
        if cards:
            logger.debug("[HTML] Селектор data-cy=l-card: %d элементов", len(cards))
        else:
            cards = soup.find_all("div", attrs={"data-testid": "listing-grid-item"})
            logger.debug("[HTML] Резервный селектор listing-grid-item: %d элементов", len(cards))
        if not cards:
            logger.warning("[HTML] Карточки не найдены ни одним селектором")
        for card in cards:
            item = self._extract_card(card)
            if item:
                results.append(item)
        return results

    def _extract_card(self, card) -> Optional[dict]:
        try:
            link = card.find("a", href=True)
            if not link:
                return None
            url = link["href"]
            if not url.startswith("http"):
                url = self.BASE_URL + url

            title_el = card.find(["h6", "h5", "h4", "h3"])
            title = title_el.get_text(strip=True) if title_el else ""
            if not title:
                return None

            price_el = card.find(attrs={"data-testid": "ad-price"})
            price = price_el.get_text(strip=True) if price_el else ""

            loc_el = card.find(attrs={"data-testid": "location-date"})
            location = loc_el.get_text(strip=True) if loc_el else ""

            return {"title": title, "price": price, "location": location,
                    "url": url, "last_online": None}
        except Exception as exc:
            logger.debug("Card extract error: %s", exc)
            return None

    # ─── Страница объявления (статус, описание, фото) ─────────────────────────

    async def _fetch_listing_details(self, url: str) -> dict:
        """Загружает страницу объявления и извлекает статус, описание и фото."""
        logger.debug("[DETAIL] Загрузка объявления: %s", url)
        html = await self._fetch(url)
        if not html:
            logger.warning("[DETAIL] Не удалось загрузить: %s", url)
            return {"last_online": None, "description": "", "images": []}
        soup = BeautifulSoup(html, "lxml")
        status = self._parse_seller_status_from_soup(soup)
        description = self._parse_description(soup)
        images = self._parse_images(soup)
        logger.debug(
            "[DETAIL] Статус: %r | Описание: %d симв. | Фото: %d | %s",
            status, len(description), len(images), url,
        )
        return {"last_online": status, "description": description, "images": images}

    def _parse_seller_status_from_soup(self, soup: BeautifulSoup) -> Optional[str]:
        # 1. __NEXT_DATA__
        status = self._extract_status_nextdata(soup)
        if status:
            logger.debug("[STATUS] Источник: NEXT_DATA → %r", status)
            return status

        # 2. data-testid атрибуты
        for attr_val in ("seller-activity", "user-activity", "last-seen"):
            el = soup.find(attrs={"data-testid": attr_val})
            if el:
                text = el.get_text(strip=True)
                if text:
                    logger.debug("[STATUS] Источник: data-testid=%s → %r", attr_val, text)
                    return text

        # 3. Regex по тексту страницы
        full_text = soup.get_text(" ", strip=True)
        match = _ONLINE_EXTRACT_RE.search(full_text)
        if match:
            logger.debug("[STATUS] Источник: regex → %r", match.group(0))
            return match.group(0)

        logger.debug("[STATUS] Статус не найден")
        return None

    def _parse_description(self, soup: BeautifulSoup) -> str:
        # 1. __NEXT_DATA__
        data = self._get_nextdata(soup)
        if data:
            try:
                ad = data["props"]["pageProps"].get("ad") or {}
                for key in ("description", "body", "text"):
                    val = ad.get(key)
                    if val and isinstance(val, str) and len(val) > 10:
                        logger.debug("[DESC] Источник: NEXT_DATA[%s] (%d симв.)", key, len(val))
                        return val.strip()
            except (KeyError, TypeError):
                pass

        # 2. HTML fallback
        for attr_val in ("ad-description", "description", "ad-body"):
            el = soup.find(attrs={"data-testid": attr_val})
            if el:
                text = el.get_text(" ", strip=True)
                if text:
                    logger.debug("[DESC] Источник: HTML data-testid=%s (%d симв.)", attr_val, len(text))
                    return text
        logger.debug("[DESC] Описание не найдено")
        return ""

    def _parse_images(self, soup: BeautifulSoup) -> list[str]:
        images: list[str] = []

        # 1. __NEXT_DATA__
        data = self._get_nextdata(soup)
        if data:
            try:
                ad = data["props"]["pageProps"].get("ad") or {}
                for key in ("photos", "images", "media"):
                    items = ad.get(key)
                    if items and isinstance(items, list):
                        for item in items:
                            if isinstance(item, dict):
                                url = (
                                    item.get("link")
                                    or item.get("url")
                                    or item.get("src")
                                    or ""
                                )
                            else:
                                url = str(item)
                            if url and url.startswith("http"):
                                images.append(url)
                        if images:
                            logger.debug("[IMG] Источник: NEXT_DATA[%s] → %d фото", key, len(images[:10]))
                            return images[:10]
            except (KeyError, TypeError):
                pass

        # 2. HTML fallback — ищем <img> в галерее
        gallery = soup.find(attrs={"data-testid": "ad-photo-gallery"})
        target = gallery if gallery else soup
        src_tag = "галерея" if gallery else "весь документ"
        for img in target.find_all("img", src=True):
            src = img["src"]
            if src.startswith("http") and "olx" in src and src not in images:
                images.append(src)
            if len(images) >= 10:
                break
        if images:
            logger.debug("[IMG] Источник: HTML (%s) → %d фото", src_tag, len(images))
        else:
            logger.debug("[IMG] Фото не найдены")
        return images

    def _extract_status_nextdata(self, soup: BeautifulSoup) -> Optional[str]:
        data = self._get_nextdata(soup)
        if not data:
            return None
        try:
            page_props = data["props"]["pageProps"]
            # Путь 1: ad.user.lastSeenAt
            ad = page_props.get("ad") or {}
            user = ad.get("user") or page_props.get("user") or {}
            for key in ("lastSeenAt", "last_seen_at", "lastSeen", "onlineStatus", "online_status"):
                val = user.get(key)
                if val:
                    return str(val)
        except (KeyError, TypeError):
            pass
        return None

    # ─── Фильтрация по «онлайн сегодня» ─────────────────────────────────────

    @staticmethod
    def is_online_today(status: Optional[str]) -> bool:
        """Возвращает True, если продавец был в сети сегодня."""
        if not status:
            return False
        return bool(_ONLINE_TODAY_RE.search(status))

    # ─── Публичный метод ──────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        max_pages: int = 3,
        max_check: int = 30,
    ) -> list[dict]:
        """
        Ищет объявления по запросу и возвращает только те,
        где продавец был онлайн сегодня.
        """
        t_start = time.monotonic()
        logger.info("═" * 55)
        logger.info("[SEARCH] Запрос: '%s' | страниц=%d | лимит=%d", query, max_pages, max_check)
        logger.info("═" * 55)

        # 1. Собираем карточки со страниц поиска
        all_listings: list[dict] = []
        seen_urls: set[str] = set()

        for page in range(1, max_pages + 1):
            cards = await self._fetch_listings_page(query, page)
            if not cards:
                logger.info("[SEARCH] Стр.%d пуста — останавливаемся", page)
                break
            new_count = 0
            for card in cards:
                if card["url"] and card["url"] not in seen_urls:
                    seen_urls.add(card["url"])
                    all_listings.append(card)
                    new_count += 1
            logger.info(
                "[SEARCH] Стр.%d: +%d новых | дублей: %d | итого: %d",
                page, new_count, len(cards) - new_count, len(all_listings),
            )
            await asyncio.sleep(random.uniform(config.REQUEST_DELAY_MIN, config.REQUEST_DELAY_MAX))

        if not all_listings:
            logger.info("[SEARCH] Карточки не найдены")
            return []

        # 2. Проверяем каждое объявление на статус онлайн
        filtered: list[dict] = []
        to_check = all_listings[:max_check]
        total = len(to_check)
        logger.info("[CHECK] Начинаем проверку %d объявлений на статус онлайн...", total)

        for idx, listing in enumerate(to_check, start=1):
            logger.info(
                "[CHECK] %d/%d | %s",
                idx, total, listing.get("title", "(без названия)")[:60],
            )
            details = await self._fetch_listing_details(listing["url"])
            online = self.is_online_today(details["last_online"])
            logger.info(
                "[CHECK] %d/%d | Онлайн: %s | Статус: %r | Фото: %d | Описание: %d симв.",
                idx, total,
                "✓ ДА" if online else "✗ нет",
                details["last_online"],
                len(details["images"]),
                len(details["description"]),
            )
            if online:
                listing["last_online"] = details["last_online"]
                listing["description"] = details["description"]
                listing["images"] = details["images"]
                filtered.append(listing)
            await asyncio.sleep(random.uniform(config.REQUEST_DELAY_MIN, config.REQUEST_DELAY_MAX))

        elapsed = time.monotonic() - t_start
        logger.info("─" * 55)
        logger.info(
            "[READY] '%s' | проверено: %d | онлайн: %d | время: %.1fs",
            query, total, len(filtered), elapsed,
        )
        logger.info("═" * 55)
        return filtered

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
