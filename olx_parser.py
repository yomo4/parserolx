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
from pathlib import Path
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
    r"(\d+)\s*(?:review(?:-uri)?|reviews|recenzii|evaluari|evalu\u0103ri|opinii|ratinguri|ratings?)",
    re.IGNORECASE,
)

_NO_REVIEWS_RE = re.compile(
    r"(?:fara\s+evaluari|f\u0103r\u0103\s+evalu\u0103ri|fara\s+ratinguri|0\s+(?:review(?:-uri)?|reviews|recenzii|evaluari|evalu\u0103ri|opinii|ratinguri|ratings?))",
    re.IGNORECASE,
)

_SELLER_RATING_VALUE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*/\s*5", re.IGNORECASE)
_SELLER_PRIVATE_RE = re.compile(r"\b(?:privat|private|persoana fizica)\b", re.IGNORECASE)
_SELLER_BUSINESS_RE = re.compile(
    r"\b(?:firma|companie|company|business|dealer|magazin|persoana juridica)\b",
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
    already_seen_skipped: int = 0
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
        self._cookie_header = self._load_cookie_header()

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
        headers = {
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
        if self._cookie_header:
            headers["Cookie"] = self._cookie_header
        return headers

    @staticmethod
    def _normalize_match_text(text: str) -> str:
        normalized = unicodedata.normalize("NFKD", text)
        ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
        return re.sub(r"\s+", " ", ascii_text).strip().lower()

    def _load_cookie_header(self) -> str:
        source = Path(config.COOKIE_SOURCE)
        if not source.is_absolute():
            source = Path.cwd() / source

        cookie_file = self._discover_cookie_file(source)
        if not cookie_file:
            logger.info("[COOKIE] Cookie source not found: %s", source)
            return ""

        try:
            raw = cookie_file.read_text(encoding="utf-8")
            payload = json.loads(raw)
        except Exception as exc:
            logger.warning("[COOKIE] Failed to read cookie file %s: %r", cookie_file, exc)
            return ""

        if not isinstance(payload, list):
            logger.warning("[COOKIE] Unsupported cookie format in %s", cookie_file)
            return ""

        now_ts = int(time.time())
        cookies: dict[str, str] = {}
        for item in payload:
            if not isinstance(item, dict):
                continue
            domain = str(item.get("domain") or "")
            if "olx.ro" not in domain:
                continue

            name = str(item.get("name") or "").strip()
            value = str(item.get("value") or "")
            if not name:
                continue

            expires = item.get("expires")
            if isinstance(expires, (int, float)) and expires not in (0,):
                if int(expires) < now_ts:
                    continue

            cookies[name] = value

        if not cookies:
            logger.warning("[COOKIE] No active OLX cookies found in %s", cookie_file)
            return ""

        header = "; ".join(f"{name}={value}" for name, value in cookies.items())
        logger.info("[COOKIE] Loaded %d OLX cookies from %s", len(cookies), cookie_file.name)
        return header

    @staticmethod
    def _discover_cookie_file(source: Path) -> Optional[Path]:
        if source.is_file():
            return source
        if source.is_dir():
            files = sorted(
                [path for path in source.iterdir() if path.is_file() and path.suffix.lower() in {".txt", ".json"}],
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            if files:
                return files[0]
        return None

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
                "reviews_source": "empty",
                "has_review_signal": False,
                "has_no_reviews_signal": False,
                "seller_name": None,
                "seller_type": None,
                "seller_rating": None,
            }

        soup = BeautifulSoup(html, "lxml")
        status = self._parse_seller_status_from_soup(soup)
        description = self._parse_description(soup)
        images = self._parse_images(soup)
        seller_name = self._parse_seller_name(soup)
        seller_type = self._parse_seller_type(soup)
        seller_rating = self._parse_seller_rating_value(soup)
        reviews_count, reviews_source, has_review_signal, has_no_reviews_signal = self._parse_reviews_info(
            soup,
            seller_rating,
        )
        seller_debug = self._collect_seller_debug(soup)

        logger.debug(
            "[DETAIL] seller=%r type=%r rating=%r status=%r reviews=%r source=%s signal=%s no_reviews=%s desc=%d images=%d url=%s",
            seller_name,
            seller_type,
            seller_rating,
            status,
            reviews_count,
            reviews_source,
            "yes" if has_review_signal else "no",
            "yes" if has_no_reviews_signal else "no",
            len(description),
            len(images),
            url,
        )
        logger.info(
            "[SELLER] type=%r name=%r rating=%r reviews=%r reviews_source=%s review_signal=%s no_reviews=%s status=%r debug=%s",
            seller_type,
            seller_name,
            seller_rating,
            reviews_count,
            reviews_source,
            "yes" if has_review_signal else "no",
            "yes" if has_no_reviews_signal else "no",
            status,
            seller_debug,
        )
        return {
            "last_online": status,
            "description": description,
            "images": images,
            "reviews_count": reviews_count,
            "reviews_source": reviews_source,
            "has_review_signal": has_review_signal,
            "has_no_reviews_signal": has_no_reviews_signal,
            "seller_name": seller_name,
            "seller_type": seller_type,
            "seller_rating": seller_rating,
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

    def _parse_seller_name(self, soup: BeautifulSoup) -> Optional[str]:
        data = self._get_nextdata(soup)
        if data:
            try:
                page_props = data["props"]["pageProps"]
                ad = page_props.get("ad") or {}
                user = ad.get("user") or page_props.get("user") or {}
                for key in ("name", "sellerName", "userName", "username"):
                    value = user.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
            except (KeyError, TypeError):
                pass

        for test_id in ("seller-name", "user-name", "aside-user-name", "profile-name"):
            element = soup.find(attrs={"data-testid": test_id})
            if element:
                text = element.get_text(" ", strip=True)
                if text:
                    return text

        # Fallback: seller card often contains a short proper-name line near the rating block.
        candidates = soup.find_all(["h4", "h5", "h6", "span", "div"])
        for element in candidates:
            text = element.get_text(" ", strip=True)
            if not text:
                continue
            normalized = self._normalize_match_text(text)
            if normalized in {"privat", "companie", "business"}:
                continue
            if len(text) <= 40 and re.fullmatch(r"[A-Za-zA-ZÀ-ÿ0-9 .'\-]+", text):
                parent_text = self._normalize_match_text(element.parent.get_text(" ", strip=True)) if element.parent else ""
                if "activ" in parent_text or "rating" in parent_text or "olx din" in parent_text:
                    return text.strip()
        return None

    def _parse_seller_type(self, soup: BeautifulSoup) -> Optional[str]:
        data = self._get_nextdata(soup)
        if data:
            try:
                page_props = data["props"]["pageProps"]
                ad = page_props.get("ad") or {}
                user = ad.get("user") or page_props.get("user") or {}
                account_type = user.get("accountType") or user.get("type") or user.get("businessStatus")
                if isinstance(account_type, str) and account_type.strip():
                    normalized = self._normalize_match_text(account_type)
                    if "private" in normalized or "privat" in normalized:
                        return "private"
                    if "business" in normalized or "companie" in normalized or "company" in normalized:
                        return "business"
            except (KeyError, TypeError):
                pass

        seller_text = self._normalize_match_text(" | ".join(self._collect_seller_texts(soup)))
        if _SELLER_BUSINESS_RE.search(seller_text):
            return "business"
        if _SELLER_PRIVATE_RE.search(seller_text):
            return "private"

        full_text = self._normalize_match_text(soup.get_text(" ", strip=True))
        if _SELLER_BUSINESS_RE.search(full_text):
            return "business"
        if _SELLER_PRIVATE_RE.search(full_text):
            return "private"
        return None

    def _parse_seller_rating_value(self, soup: BeautifulSoup) -> Optional[str]:
        data = self._get_nextdata(soup)
        if data:
            rating = self._find_seller_rating_value(data)
            if rating is not None:
                return rating

        full_text = soup.get_text(" ", strip=True)
        normalized = self._normalize_match_text(full_text)
        match = _SELLER_RATING_VALUE_RE.search(normalized)
        if match:
            return match.group(1).replace(",", ".")
        return None

    def _find_seller_rating_value(self, obj: Any) -> Optional[str]:
        if isinstance(obj, dict):
            for key, value in obj.items():
                key_norm = key.lower().replace("-", "").replace("_", "")
                if "rating" in key_norm or "score" in key_norm:
                    rating = self._extract_rating_candidate(value)
                    if rating is not None:
                        return rating
                nested = self._find_seller_rating_value(value)
                if nested is not None:
                    return nested
        elif isinstance(obj, list):
            for item in obj:
                nested = self._find_seller_rating_value(item)
                if nested is not None:
                    return nested
        return None

    def _extract_rating_candidate(self, value: Any) -> Optional[str]:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            if 0 <= float(value) <= 5:
                return f"{float(value):.1f}"
            return None
        if isinstance(value, str):
            cleaned = value.replace(",", ".").strip()
            if re.fullmatch(r"\d+(?:\.\d+)?", cleaned):
                numeric = float(cleaned)
                if 0 <= numeric <= 5:
                    return f"{numeric:.1f}"
        if isinstance(value, dict):
            for nested_key in ("value", "score", "average", "rating"):
                nested_value = value.get(nested_key)
                rating = self._extract_rating_candidate(nested_value)
                if rating is not None:
                    return rating
        return None

    def _collect_seller_texts(self, soup: BeautifulSoup) -> list[str]:
        snippets: list[str] = []
        seen: set[str] = set()
        for element in soup.find_all(True):
            data_testid = str(element.get("data-testid") or "")
            classes = " ".join(element.get("class", [])) if isinstance(element.get("class"), list) else str(element.get("class") or "")
            marker = f"{data_testid} {classes}".lower()
            text = re.sub(r"\s+", " ", element.get_text(" ", strip=True)).strip()
            if not text:
                continue
            normalized_text = self._normalize_match_text(text)
            if any(token in marker for token in ("seller", "user", "profile", "rating", "review", "feedback", "account")) or any(
                token in normalized_text
                for token in (
                    "privat",
                    "firma",
                    "companie",
                    "business",
                    "company",
                    "dealer",
                    "ratinguri",
                    "rating",
                    "review",
                    "evaluari",
                    "feedback",
                    "olx din",
                    "activ azi",
                    "activ acum",
                )
            ):
                if text not in seen:
                    seen.add(text)
                    snippets.append(text[:220])
            if len(snippets) >= 12:
                break
        return snippets

    def _collect_seller_debug(self, soup: BeautifulSoup) -> str:
        snippets = self._collect_seller_texts(soup)
        return " | ".join(snippets[:8]) if snippets else "no-seller-snippets"

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

    def _parse_reviews_info(
        self,
        soup: BeautifulSoup,
        seller_rating: Optional[str],
    ) -> tuple[Optional[int], str, bool, bool]:
        count = self._extract_reviews_count_nextdata(soup)
        if count is not None:
            return count, "nextdata", count > 0, count == 0

        count = self._extract_reviews_count_from_review_nodes(soup)
        if count is not None:
            return count, "review_nodes", count > 0, count == 0

        count = self._extract_reviews_count_from_texts(self._collect_seller_texts(soup))
        if count is not None:
            return count, "seller_text", count > 0, count == 0

        count = self._extract_reviews_count_from_scripts(soup)
        if count is not None:
            return count, "scripts", count > 0, count == 0

        full_text = self._normalize_match_text(soup.get_text(" ", strip=True))
        match = _REVIEWS_RE.search(full_text)
        if match:
            count = int(match.group(1))
            return count, "page_text", count > 0, count == 0

        if _NO_REVIEWS_RE.search(full_text):
            return 0, "page_text_zero", False, True

        seller_texts = self._collect_seller_texts(soup)
        if self._has_generic_reviews_signal(seller_texts):
            return None, "seller_text_signal", True, False

        if seller_rating:
            return None, "rating_fallback", True, False
        return None, "unknown", False, False

    def _extract_reviews_count_from_texts(self, texts: list[str]) -> Optional[int]:
        for text in texts:
            normalized = self._normalize_match_text(text)
            match = _REVIEWS_RE.search(normalized)
            if match:
                return int(match.group(1))
            if _NO_REVIEWS_RE.search(normalized):
                return 0
        return None

    def _has_generic_reviews_signal(self, texts: list[str]) -> bool:
        for text in texts:
            normalized = self._normalize_match_text(text)
            if _NO_REVIEWS_RE.search(normalized):
                continue
            if any(
                token in normalized
                for token in ("ratinguri", "review", "reviews", "recenzii", "evaluari", "opinii")
            ):
                return True
        return False

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
                if (
                    key_norm in _REVIEW_KEYS
                    or "review" in key_norm
                    or "feedback" in key_norm
                    or "rating" in key_norm
                    or "score" in key_norm
                ):
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
    def _matches_review_filter(
        reviews_count: Optional[int],
        review_filter: str,
        seller_rating: Optional[str] = None,
        has_no_reviews_signal: bool = False,
    ) -> bool:
        has_reviews = (reviews_count is not None and reviews_count > 0) or bool((seller_rating or "").strip())
        if review_filter == "with":
            return has_reviews
        if review_filter == "without":
            return has_no_reviews_signal and not has_reviews
        return True

    @staticmethod
    def _matches_seller_type_filter(seller_type: Optional[str], review_filter: str) -> bool:
        normalized = (seller_type or "").strip().lower()
        if normalized == "business":
            return False
        if review_filter == "without":
            return normalized == "private"
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
            seller_type = details.get("seller_type")
            seller_match = self._matches_seller_type_filter(seller_type, review_filter)
            review_match = self._matches_review_filter(
                reviews_count,
                review_filter,
                details.get("seller_rating"),
                bool(details.get("has_no_reviews_signal")),
            )
            decision = "pass" if online and review_match else "filtered"
            if not online:
                decision = "filtered_offline"
            elif not seller_match:
                decision = "filtered_seller_type"
            elif not review_match:
                decision = "filtered_reviews"

            logger.info(
                "[CHECK] %d/%d online=%s seller_type=%r seller_match=%s seller=%r rating=%r reviews=%r source=%s signal=%s no_reviews=%s filter=%s review_match=%s decision=%s requests=%d",
                idx,
                total,
                "yes" if online else "no",
                seller_type,
                "yes" if seller_match else "no",
                details.get("seller_name"),
                details.get("seller_rating"),
                reviews_count,
                details.get("reviews_source"),
                "yes" if details.get("has_review_signal") else "no",
                "yes" if details.get("has_no_reviews_signal") else "no",
                review_filter,
                "yes" if review_match else "no",
                decision,
                stats.requests_made,
            )

            if online and seller_match and review_match:
                listing["last_online"] = details["last_online"]
                listing["description"] = details["description"]
                listing["images"] = details["images"]
                listing["reviews_count"] = reviews_count
                listing["reviews_source"] = details.get("reviews_source")
                listing["has_review_signal"] = details.get("has_review_signal")
                listing["has_no_reviews_signal"] = details.get("has_no_reviews_signal")
                listing["seller_name"] = details.get("seller_name")
                listing["seller_type"] = details.get("seller_type")
                listing["seller_rating"] = details.get("seller_rating")
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
