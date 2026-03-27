"""
Async OLX.ro parser with category-aware search, online-status filtering,
review filtering, progress callbacks, and request statistics.
"""

import asyncio
import json
import logging
import random
import re
import socket
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import quote

import aiohttp
from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

import config

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict], Awaitable[None]]

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

_ONLINE_TODAY_RE = re.compile(
    r"Activ\s+(?:acum|azi(?:\s+la\s+\d{1,2}:\d{2})?|la\s+\d{1,2}:\d{2})",
    re.IGNORECASE,
)

_ONLINE_EXTRACT_RE = re.compile(
    r"Activ\s+(?:acum|azi(?:\s+la\s+\d{1,2}:\d{2})?|la\s+\d{1,2}:\d{2}|ieri|acum\s+\d+\s+\w+)",
    re.IGNORECASE,
)

_REVIEWS_RE = re.compile(
    r"(\d+)\s*(?:review(?:-uri)?|reviews|recenzii|evaluari|evalu\u0103ri|opinii)",
    re.IGNORECASE,
)

_NO_REVIEWS_RE = re.compile(
    r"(?:fara\s+evaluari|f\u0103r\u0103\s+evalu\u0103ri|0\s+(?:review(?:-uri)?|reviews|recenzii|evaluari|evalu\u0103ri|opinii))",
    re.IGNORECASE,
)

_REVIEW_KEYS = {
    "reviewcount",
    "reviewscount",
    "totalreviews",
    "feedbackcount",
    "ratingcount",
    "ratingscount",
    "totalratings",
}


@dataclass
class SearchStats:
    max_pages: int
    max_check: int
    review_filter: str
    category_path: str = ""
    requests_made: int = 0
    pages_loaded: int = 0
    listings_seen: int = 0
    listings_checked: int = 0
    listings_matched: int = 0
    started_at: float = field(default_factory=time.monotonic)
    elapsed: float = 0.0

    def finish(self) -> None:
        self.elapsed = time.monotonic() - self.started_at


@dataclass
class SearchResult:
    listings: list[dict]
    stats: SearchStats


class OLXParser:
    BASE_URL = "https://www.olx.ro"
    SEARCH_URL = "https://www.olx.ro/oferte/q-{query}/"

    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None

    @staticmethod
    def _build_search_slug(query: str) -> str:
        normalized = re.sub(r"\s+", "-", query.strip())
        return quote(normalized, safe="")

    def _build_search_url(self, query: str, page: int, category_path: str = "") -> str:
        normalized_path = category_path.strip("/")
        cleaned_query = query.strip()
        if cleaned_query:
            slug = self._build_search_slug(cleaned_query)
            if normalized_path:
                url = f"{self.BASE_URL}/{normalized_path}/q-{slug}/"
            else:
                url = self.SEARCH_URL.format(query=slug)
        else:
            if normalized_path:
                url = f"{self.BASE_URL}/{normalized_path}/"
            else:
                url = f"{self.BASE_URL}/oferte/"
        if page > 1:
            url += f"?page={page}"
        return url

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

    @staticmethod
    def _normalize_match_text(text: str) -> str:
        normalized = unicodedata.normalize("NFKD", text)
        ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
        return re.sub(r"\s+", " ", ascii_text).strip().lower()

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

    def _fetch_with_curl(self, url: str, stats: SearchStats) -> Optional[str]:
        stats.requests_made += 1
        try:
            response = curl_requests.get(
                url,
                headers=self._build_headers(),
                impersonate="chrome124",
                timeout=30,
                allow_redirects=True,
                verify=False,
            )
        except Exception as exc:
            logger.error("CURL error | %r | %s", exc, url)
            return None

        if response.status_code == 200:
            text = response.text
            logger.debug("CURL ok | bytes=%d | %s", len(text), url)
            return text

        logger.warning("CURL http=%d | %s", response.status_code, url)
        return None

    async def _fetch(self, url: str, stats: SearchStats) -> Optional[str]:
        session = await self._get_session()
        stats.requests_made += 1
        started = time.monotonic()
        logger.debug("GET %s", url)

        try:
            async with session.get(url, headers=self._build_headers()) as response:
                elapsed = time.monotonic() - started
                if response.status == 200:
                    text = await response.text()
                    logger.debug(
                        "HTTP ok | status=%d | elapsed=%.2fs | bytes=%d | %s",
                        response.status,
                        elapsed,
                        len(text),
                        url,
                    )
                    return text
                logger.warning(
                    "HTTP bad status=%d | elapsed=%.2fs | %s",
                    response.status,
                    elapsed,
                    url,
                )
                return None
        except asyncio.TimeoutError:
            logger.error("HTTP timeout | elapsed=%.2fs | %s", time.monotonic() - started, url)
        except aiohttp.ClientError as exc:
            logger.warning("AIOHTTP error | %r | %s", exc, url)
            text = await asyncio.to_thread(self._fetch_with_curl, url, stats)
            if text:
                return text
        return None

    async def _fetch_listings_page(
        self,
        query: str,
        page: int,
        stats: SearchStats,
        category_path: str = "",
    ) -> list[dict]:
        url = self._build_search_url(query, page, category_path)
        logger.info("[SEARCH] Page %d url=%s", page, url)

        html = await self._fetch(url, stats)
        if not html:
            logger.warning("[SEARCH] Page %d returned empty body", page)
            return []

        stats.pages_loaded += 1
        cards = self._parse_listing_cards(html)
        logger.info("[SEARCH] Page %d cards=%d", page, len(cards))
        return cards

    def _parse_listing_cards(self, html: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")

        items = self._parse_nextdata_listings(soup)
        if items:
            logger.debug("[PARSE] Source=__NEXT_DATA__ cards=%d", len(items))
            return items

        items = self._parse_html_cards(soup)
        logger.debug("[PARSE] Source=HTML cards=%d", len(items))
        return items

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
            return []

        results: list[dict] = []
        try:
            page_props = data["props"]["pageProps"]
            ads = page_props.get("ads") or page_props.get("data", {}).get("ads") or []
        except (KeyError, TypeError) as exc:
            logger.warning("[NEXT_DATA] Ads parse error: %s", exc)
            return []

        for ad in ads:
            url = ad.get("url", "")
            if url and not url.startswith("http"):
                url = self.BASE_URL + url

            price_raw = ad.get("price", {})
            if isinstance(price_raw, dict):
                price = price_raw.get("displayValue", "")
            else:
                price = str(price_raw)

            location_raw = ad.get("location", {})
            if isinstance(location_raw, dict):
                location = location_raw.get("name", "")
            else:
                location = str(location_raw)

            results.append(
                {
                    "title": ad.get("title", ""),
                    "price": price,
                    "location": location,
                    "url": url,
                    "last_online": None,
                }
            )

        return results

    def _parse_html_cards(self, soup: BeautifulSoup) -> list[dict]:
        results: list[dict] = []
        cards = soup.find_all("div", {"data-cy": "l-card"})
        if not cards:
            cards = soup.find_all("div", attrs={"data-testid": "listing-grid-item"})

        for card in cards:
            item = self._extract_card(card)
            if item:
                results.append(item)
        return results

    def _extract_card(self, card: Any) -> Optional[dict]:
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

            location_el = card.find(attrs={"data-testid": "location-date"})
            location = location_el.get_text(strip=True) if location_el else ""

            return {
                "title": title,
                "price": price,
                "location": location,
                "url": url,
                "last_online": None,
            }
        except Exception as exc:
            logger.debug("Card extract error: %s", exc)
            return None

    async def _fetch_listing_details(self, url: str, stats: SearchStats) -> dict:
        logger.debug("[DETAIL] Loading %s", url)
        html = await self._fetch(url, stats)
        if not html:
            logger.warning("[DETAIL] Empty response for %s", url)
            return {
                "last_online": None,
                "description": "",
                "images": [],
                "reviews_count": None,
            }

        soup = BeautifulSoup(html, "lxml")
        status = self._parse_seller_status_from_soup(soup)
        description = self._parse_description(soup)
        images = self._parse_images(soup)
        reviews_count = self._parse_reviews_count(soup)

        logger.debug(
            "[DETAIL] status=%r reviews=%r desc=%d images=%d url=%s",
            status,
            reviews_count,
            len(description),
            len(images),
            url,
        )
        return {
            "last_online": status,
            "description": description,
            "images": images,
            "reviews_count": reviews_count,
        }

    def _parse_seller_status_from_soup(self, soup: BeautifulSoup) -> Optional[str]:
        status = self._extract_status_nextdata(soup)
        if status:
            return status

        for test_id in ("seller-activity", "user-activity", "last-seen"):
            element = soup.find(attrs={"data-testid": test_id})
            if element:
                text = element.get_text(strip=True)
                if text:
                    return text

        full_text = soup.get_text(" ", strip=True)
        match = _ONLINE_EXTRACT_RE.search(full_text)
        if match:
            return match.group(0)
        return None

    def _parse_description(self, soup: BeautifulSoup) -> str:
        data = self._get_nextdata(soup)
        if data:
            try:
                ad = data["props"]["pageProps"].get("ad") or {}
                for key in ("description", "body", "text"):
                    value = ad.get(key)
                    if isinstance(value, str) and len(value) > 10:
                        return value.strip()
            except (KeyError, TypeError):
                pass

        for test_id in ("ad-description", "description", "ad-body"):
            element = soup.find(attrs={"data-testid": test_id})
            if element:
                text = element.get_text(" ", strip=True)
                if text:
                    return text
        return ""

    def _parse_images(self, soup: BeautifulSoup) -> list[str]:
        images: list[str] = []

        data = self._get_nextdata(soup)
        if data:
            try:
                ad = data["props"]["pageProps"].get("ad") or {}
                for key in ("photos", "images", "media"):
                    items = ad.get(key)
                    if not isinstance(items, list):
                        continue
                    for item in items:
                        if isinstance(item, dict):
                            url = item.get("link") or item.get("url") or item.get("src") or ""
                        else:
                            url = str(item)
                        if url.startswith("http"):
                            images.append(url)
                    if images:
                        return images[:10]
            except (KeyError, TypeError):
                pass

        gallery = soup.find(attrs={"data-testid": "ad-photo-gallery"})
        target = gallery if gallery else soup
        for image in target.find_all("img", src=True):
            src = image["src"]
            if src.startswith("http") and "olx" in src and src not in images:
                images.append(src)
            if len(images) >= 10:
                break
        return images

    def _parse_reviews_count(self, soup: BeautifulSoup) -> Optional[int]:
        count = self._extract_reviews_count_nextdata(soup)
        if count is not None:
            return count

        count = self._extract_reviews_count_from_review_nodes(soup)
        if count is not None:
            return count

        count = self._extract_reviews_count_from_scripts(soup)
        if count is not None:
            return count

        full_text = self._normalize_match_text(soup.get_text(" ", strip=True))
        match = _REVIEWS_RE.search(full_text)
        if match:
            return int(match.group(1))

        if _NO_REVIEWS_RE.search(full_text):
            return 0
        return None

    def _extract_reviews_count_nextdata(self, soup: BeautifulSoup) -> Optional[int]:
        data = self._get_nextdata(soup)
        if not data:
            return None
        try:
            page_props = data["props"]["pageProps"]
        except (KeyError, TypeError):
            page_props = None

        if page_props is not None:
            count = self._find_reviews_count(page_props)
            if count is not None:
                return count

        return self._find_reviews_count(data)

    def _extract_reviews_count_from_review_nodes(self, soup: BeautifulSoup) -> Optional[int]:
        for element in soup.find_all(attrs={"data-testid": re.compile(r"(review|rating|feedback)", re.IGNORECASE)}):
            text = self._normalize_match_text(element.get_text(" ", strip=True))
            if not text:
                continue
            match = _REVIEWS_RE.search(text)
            if match:
                return int(match.group(1))
            if _NO_REVIEWS_RE.search(text):
                return 0
        return None

    def _extract_reviews_count_from_scripts(self, soup: BeautifulSoup) -> Optional[int]:
        for script in soup.find_all("script"):
            script_text = script.string or script.get_text(" ", strip=True)
            if not script_text:
                continue
            normalized = self._normalize_match_text(script_text)
            match = _REVIEWS_RE.search(normalized)
            if match:
                return int(match.group(1))
            if _NO_REVIEWS_RE.search(normalized):
                return 0
        return None

    def _find_reviews_count(self, obj: Any) -> Optional[int]:
        if isinstance(obj, dict):
            for key, value in obj.items():
                key_norm = key.lower().replace("-", "").replace("_", "")
                if key_norm in _REVIEW_KEYS or "review" in key_norm or "feedback" in key_norm:
                    extracted = self._extract_count_candidate(value)
                    if extracted is not None:
                        return extracted
                nested = self._find_reviews_count(value)
                if nested is not None:
                    return nested
        elif isinstance(obj, list):
            for item in obj:
                nested = self._find_reviews_count(item)
                if nested is not None:
                    return nested
        return None

    def _extract_count_candidate(self, value: Any) -> Optional[int]:
        if isinstance(value, bool):
            return None
        if isinstance(value, int) and value >= 0:
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
        if isinstance(value, list):
            return len(value)
        if isinstance(value, dict):
            for nested_key in ("count", "total", "value", "all"):
                nested_value = value.get(nested_key)
                if isinstance(nested_value, int) and nested_value >= 0:
                    return nested_value
                if isinstance(nested_value, str) and nested_value.isdigit():
                    return int(nested_value)
            items = value.get("items")
            if isinstance(items, list):
                return len(items)
        return None

    def _extract_status_nextdata(self, soup: BeautifulSoup) -> Optional[str]:
        data = self._get_nextdata(soup)
        if not data:
            return None
        try:
            page_props = data["props"]["pageProps"]
            ad = page_props.get("ad") or {}
            user = ad.get("user") or page_props.get("user") or {}
        except (KeyError, TypeError):
            return None

        for key in (
            "lastSeenAt",
            "last_seen_at",
            "lastSeen",
            "onlineStatus",
            "online_status",
        ):
            value = user.get(key)
            if value:
                return str(value)
        return None

    @staticmethod
    def is_online_today(status: Optional[str]) -> bool:
        if not status:
            return False
        return bool(_ONLINE_TODAY_RE.search(status))

    @staticmethod
    def _matches_review_filter(reviews_count: Optional[int], review_filter: str) -> bool:
        if review_filter == "with":
            return reviews_count is not None and reviews_count > 0
        if review_filter == "without":
            return reviews_count is None or reviews_count == 0
        return True

    async def _notify_progress(
        self,
        callback: Optional[ProgressCallback],
        phase: str,
        stats: SearchStats,
        **extra: Any,
    ) -> None:
        if not callback:
            return

        payload = {
            "phase": phase,
            "requests_made": stats.requests_made,
            "pages_loaded": stats.pages_loaded,
            "listings_seen": stats.listings_seen,
            "checked": stats.listings_checked,
            "matched": stats.listings_matched,
            **extra,
        }
        try:
            await callback(payload)
        except Exception:
            logger.exception("Progress callback failed")

    async def search(
        self,
        query: str,
        max_pages: int = 3,
        max_check: int = 30,
        category_path: str = "",
        review_filter: str = "any",
        progress_callback: Optional[ProgressCallback] = None,
    ) -> SearchResult:
        stats = SearchStats(
            max_pages=max_pages,
            max_check=max_check,
            review_filter=review_filter,
            category_path=category_path.strip("/"),
        )

        logger.info("=" * 55)
        logger.info(
            "[SEARCH] query=%r category=%s pages=%d limit=%d reviews=%s",
            query,
            stats.category_path or "all",
            max_pages,
            max_check,
            review_filter,
        )
        logger.info("=" * 55)

        all_listings: list[dict] = []
        seen_urls: set[str] = set()

        for page in range(1, max_pages + 1):
            cards = await self._fetch_listings_page(
                query,
                page,
                stats,
                category_path=stats.category_path,
            )
            if not cards:
                logger.info("[SEARCH] page=%d empty, stopping", page)
                break

            new_count = 0
            for card in cards:
                url = card.get("url", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    all_listings.append(card)
                    new_count += 1

            stats.listings_seen = len(all_listings)
            logger.info(
                "[SEARCH] page=%d new=%d dup=%d total=%d",
                page,
                new_count,
                len(cards) - new_count,
                len(all_listings),
            )
            await self._notify_progress(
                progress_callback,
                "collect",
                stats,
                page=page,
                collected=len(all_listings),
            )
            await asyncio.sleep(random.uniform(config.REQUEST_DELAY_MIN, config.REQUEST_DELAY_MAX))

        if not all_listings:
            logger.info("[SEARCH] no cards found")
            stats.finish()
            await self._notify_progress(progress_callback, "done", stats, total=0)
            return SearchResult(listings=[], stats=stats)

        filtered: list[dict] = []
        to_check = all_listings[:max_check]
        total = len(to_check)
        logger.info("[CHECK] start total=%d", total)

        for idx, listing in enumerate(to_check, start=1):
            title = listing.get("title", "(untitled)")[:60]
            logger.info("[CHECK] %d/%d title=%s", idx, total, title)

            details = await self._fetch_listing_details(listing["url"], stats)
            stats.listings_checked = idx

            online = self.is_online_today(details["last_online"])
            reviews_count = details["reviews_count"]
            review_match = self._matches_review_filter(reviews_count, review_filter)

            logger.info(
                "[CHECK] %d/%d online=%s reviews=%r filter=%s review_match=%s requests=%d",
                idx,
                total,
                "yes" if online else "no",
                reviews_count,
                review_filter,
                "yes" if review_match else "no",
                stats.requests_made,
            )

            if online and review_match:
                listing["last_online"] = details["last_online"]
                listing["description"] = details["description"]
                listing["images"] = details["images"]
                listing["reviews_count"] = reviews_count
                filtered.append(listing)
                stats.listings_matched = len(filtered)

            await self._notify_progress(
                progress_callback,
                "check",
                stats,
                total=total,
                current_title=listing.get("title", ""),
            )
            await asyncio.sleep(random.uniform(config.REQUEST_DELAY_MIN, config.REQUEST_DELAY_MAX))

        stats.finish()
        logger.info("-" * 55)
        logger.info(
            "[READY] query=%r category=%s checked=%d matched=%d requests=%d elapsed=%.1fs",
            query,
            stats.category_path or "all",
            stats.listings_checked,
            len(filtered),
            stats.requests_made,
            stats.elapsed,
        )
        logger.info("=" * 55)

        await self._notify_progress(progress_callback, "done", stats, total=total)
        return SearchResult(listings=filtered, stats=stats)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
