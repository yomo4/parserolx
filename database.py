import secrets
import sqlite3
import string
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(UTC)


def dt_to_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def iso_to_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value)


class BotDatabase:
    def __init__(self, db_path: str) -> None:
        self._db_path = Path(db_path)
        if not self._db_path.is_absolute():
            self._db_path = Path.cwd() / self._db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                full_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                subscription_until TEXT,
                redeemed_code TEXT,
                total_searches INTEGER NOT NULL DEFAULT 0,
                last_query TEXT
            );

            CREATE TABLE IF NOT EXISTS subscription_codes (
                code TEXT PRIMARY KEY,
                duration_days INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                created_by INTEGER NOT NULL,
                redeemed_by INTEGER,
                redeemed_at TEXT,
                is_active INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS broadcast_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL,
                message_text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                delivered_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS seen_listings (
                user_id INTEGER NOT NULL,
                search_key TEXT NOT NULL,
                url TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                PRIMARY KEY (user_id, search_key, url),
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_seen_listings_last_seen_at
            ON seen_listings(last_seen_at);
            """
        )
        self._conn.commit()

    def upsert_user(
        self,
        user_id: int,
        username: Optional[str],
        first_name: Optional[str],
        last_name: Optional[str],
    ) -> None:
        now = dt_to_iso(utc_now())
        full_name = " ".join(part for part in (first_name, last_name) if part).strip()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO users (
                    user_id,
                    username,
                    first_name,
                    last_name,
                    full_name,
                    created_at,
                    last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username=excluded.username,
                    first_name=excluded.first_name,
                    last_name=excluded.last_name,
                    full_name=excluded.full_name,
                    last_seen_at=excluded.last_seen_at
                """,
                (user_id, username, first_name, last_name, full_name, now, now),
            )
            self._conn.commit()

    def get_user(self, user_id: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if not row:
            return None
        return dict(row)

    def has_active_subscription(self, user_id: int) -> bool:
        user = self.get_user(user_id)
        if not user:
            return False
        expires_at = iso_to_dt(user.get("subscription_until"))
        return bool(expires_at and expires_at > utc_now())

    def get_subscription_until(self, user_id: int) -> Optional[datetime]:
        user = self.get_user(user_id)
        if not user:
            return None
        return iso_to_dt(user.get("subscription_until"))

    def record_search(self, user_id: int, query: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE users
                SET total_searches = total_searches + 1,
                    last_query = ?,
                    last_seen_at = ?
                WHERE user_id = ?
                """,
                (query, dt_to_iso(utc_now()), user_id),
            )
            self._conn.commit()

    def filter_new_listings(
        self,
        user_id: int,
        search_key: str,
        listings: list[dict],
        ttl_hours: int,
    ) -> tuple[list[dict], int]:
        now = utc_now()
        cutoff = now - timedelta(hours=max(ttl_hours, 0))
        now_iso = dt_to_iso(now)
        cutoff_iso = dt_to_iso(cutoff)

        with self._lock:
            self._conn.execute(
                "DELETE FROM seen_listings WHERE last_seen_at < ?",
                (cutoff_iso,),
            )

            rows = self._conn.execute(
                """
                SELECT url
                FROM seen_listings
                WHERE user_id = ? AND search_key = ?
                """,
                (user_id, search_key),
            ).fetchall()
            seen_urls = {str(row["url"]) for row in rows}

            fresh_listings: list[dict] = []
            skipped_old = 0
            new_rows: list[tuple[int, str, str, str, str]] = []

            for listing in listings:
                url = str(listing.get("url") or "").strip()
                if not url:
                    fresh_listings.append(listing)
                    continue
                if url in seen_urls:
                    skipped_old += 1
                    continue
                fresh_listings.append(listing)
                new_rows.append((user_id, search_key, url, now_iso, now_iso))

            if new_rows:
                self._conn.executemany(
                    """
                    INSERT INTO seen_listings (
                        user_id,
                        search_key,
                        url,
                        first_seen_at,
                        last_seen_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, search_key, url) DO UPDATE SET
                        last_seen_at = excluded.last_seen_at
                    """,
                    new_rows,
                )

            self._conn.commit()

        return fresh_listings, skipped_old

    def generate_subscription_code(self, duration_days: int, created_by: int) -> str:
        alphabet = string.ascii_uppercase + string.digits
        with self._lock:
            while True:
                left = "".join(secrets.choice(alphabet) for _ in range(4))
                right = "".join(secrets.choice(alphabet) for _ in range(4))
                code = f"OLX-{left}@{right}"
                exists = self._conn.execute(
                    "SELECT 1 FROM subscription_codes WHERE code = ?",
                    (code,),
                ).fetchone()
                if not exists:
                    break

            self._conn.execute(
                """
                INSERT INTO subscription_codes (
                    code,
                    duration_days,
                    created_at,
                    created_by,
                    is_active
                ) VALUES (?, ?, ?, ?, 1)
                """,
                (code, duration_days, dt_to_iso(utc_now()), created_by),
            )
            self._conn.commit()
        return code

    def redeem_subscription_code(self, user_id: int, code: str) -> tuple[bool, str, Optional[datetime]]:
        normalized_code = code.strip().upper()
        if not normalized_code:
            return False, "Пустой код подписки.", None

        now = utc_now()
        with self._lock:
            row = self._conn.execute(
                """
                SELECT *
                FROM subscription_codes
                WHERE code = ? AND is_active = 1
                """,
                (normalized_code,),
            ).fetchone()

            if not row:
                used_row = self._conn.execute(
                    "SELECT redeemed_by FROM subscription_codes WHERE code = ?",
                    (normalized_code,),
                ).fetchone()
                if used_row:
                    return False, "Этот код уже использован.", None
                return False, "Код не найден.", None

            user_row = self._conn.execute(
                "SELECT subscription_until FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()

            current_until = iso_to_dt(user_row["subscription_until"]) if user_row else None
            base = current_until if current_until and current_until > now else now
            new_until = base + timedelta(days=int(row["duration_days"]))

            self._conn.execute(
                """
                UPDATE users
                SET subscription_until = ?,
                    redeemed_code = ?,
                    last_seen_at = ?
                WHERE user_id = ?
                """,
                (dt_to_iso(new_until), normalized_code, dt_to_iso(now), user_id),
            )
            self._conn.execute(
                """
                UPDATE subscription_codes
                SET redeemed_by = ?,
                    redeemed_at = ?,
                    is_active = 0
                WHERE code = ?
                """,
                (user_id, dt_to_iso(now), normalized_code),
            )
            self._conn.commit()

        return True, "Подписка активирована.", new_until

    def list_user_ids(self) -> list[int]:
        with self._lock:
            rows = self._conn.execute("SELECT user_id FROM users ORDER BY user_id").fetchall()
        return [int(row["user_id"]) for row in rows]

    def log_broadcast(self, admin_id: int, message_text: str, delivered_count: int, failed_count: int) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO broadcast_logs (
                    admin_id,
                    message_text,
                    created_at,
                    delivered_count,
                    failed_count
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (admin_id, message_text, dt_to_iso(utc_now()), delivered_count, failed_count),
            )
            self._conn.commit()

    def get_stats(self) -> dict:
        now_iso = dt_to_iso(utc_now())
        with self._lock:
            total_users = self._conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            active_subscriptions = self._conn.execute(
                "SELECT COUNT(*) FROM users WHERE subscription_until IS NOT NULL AND subscription_until > ?",
                (now_iso,),
            ).fetchone()[0]
            total_codes = self._conn.execute("SELECT COUNT(*) FROM subscription_codes").fetchone()[0]
            available_codes = self._conn.execute(
                "SELECT COUNT(*) FROM subscription_codes WHERE is_active = 1"
            ).fetchone()[0]
            redeemed_codes = self._conn.execute(
                "SELECT COUNT(*) FROM subscription_codes WHERE redeemed_by IS NOT NULL"
            ).fetchone()[0]
            total_broadcasts = self._conn.execute(
                "SELECT COUNT(*) FROM broadcast_logs"
            ).fetchone()[0]

        return {
            "total_users": int(total_users),
            "active_subscriptions": int(active_subscriptions),
            "total_codes": int(total_codes),
            "available_codes": int(available_codes),
            "redeemed_codes": int(redeemed_codes),
            "total_broadcasts": int(total_broadcasts),
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()
