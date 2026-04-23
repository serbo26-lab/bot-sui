import asyncio
import re
import json
import logging
from logging.handlers import RotatingFileHandler
import secrets
import string
import uuid
import contextlib
import sqlite3
import tarfile
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse
from typing import Optional
from urllib.parse import quote, urlencode

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, LabeledPrice, ReplyKeyboardMarkup

# =========================
# CONFIG
# =========================

BOT_TOKEN = "token"
ADMIN_IDS = {123456, 78910}

SUI_API_URL = "https://domain.ru:2095/app/apiv2"
SUI_TOKEN = "token"
SUI_SUB_URL = "https://domain.ru:2096/sub/"
SUI_SERVER_NAME = "🌐 YouNameServer"
SUI_SERVER_CODE = "NL"
SUI_DEFAULT_INBOUNDS = "1,2,3"
#DEFAULT_INBOUNDS смотреть в F12 - playload - save при редактировании подписки в s-ui с добавленными нужными INBOUNDS
BASE_DIR = Path("/root/bot-sui")
PAYMENTS_DB = BASE_DIR / "payments.db"
SUPPORT_DB = BASE_DIR / "support.db"
REMINDERS_DB = BASE_DIR / "reminders.db"
ANTIABUSE_DB = BASE_DIR / "antiabuse.db"
TARIFFS_DB = BASE_DIR / "tariffs.db"
TRIALS_DB = BASE_DIR / "trials.db"
TGPROXY_DB = BASE_DIR / "tgproxy.db"
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "bot.log"
HEALTH_FILE = BASE_DIR / "health.json"
BACKUP_DIR = BASE_DIR / "backups"
BACKUP_INTERVAL_SECONDS = 600
BACKUP_RETENTION_DAYS = 7
BACKUP_DB_PATHS = (PAYMENTS_DB, SUPPORT_DB, REMINDERS_DB, ANTIABUSE_DB, TARIFFS_DB, TRIALS_DB, TGPROXY_DB)

http_session: aiohttp.ClientSession | None = None
sui_cache_data: list[dict] | None = None
sui_cache_ts: float = 0.0
sui_cache_lock = asyncio.Lock()
APP_BUILD = "stage38_2_6_tgproxy"

ANTIABUSE_ENABLED = True
ANTIABUSE_JOURNAL_UNIT = "s-ui"
ANTIABUSE_IP_LIMIT = 10
ANTIABUSE_WINDOW_MINUTES = 5
ANTIABUSE_ALERT_COOLDOWN_MINUTES = 60
ANTIABUSE_NOTIFY_USER = True
ANTIABUSE_RETENTION_HOURS = 72
ANTIABUSE_MATCH_WINDOWS = {
    "vless": 2.0,
    "tuic": 1.0,
    "hysteria2": 1.0,
}
ANTIABUSE_POLL_SECONDS = 60
ANTIABUSE_INITIAL_LOOKBACK_SECONDS = 180
ANTIABUSE_WARN1_COOLDOWN_MINUTES = 30
ANTIABUSE_WARN2_COOLDOWN_MINUTES = 60
ANTIABUSE_DISABLE_DEFAULT_MINUTES = 10
ANTIABUSE_DISABLE_MAX_MINUTES = 1440

antiabuse_recent_from = {
    "vless": deque(),
    "tuic": deque(),
    "hysteria2": deque(),
}
antiabuse_recent_name = {
    "vless": deque(),
    "tuic": deque(),
    "hysteria2": deque(),
}
antiabuse_lock = asyncio.Lock()
antiabuse_seen_signatures = set()
antiabuse_last_scan_ts: float | None = None


# =========================
# DATA
# =========================

@dataclass(frozen=True)
class Tariff:
    key: str
    title: str
    days: int
    traffic_gb: int | None
    stars_price: int
    code: str
    is_public: bool = True
    is_admin_only: bool = False
    connection_limit: int = 10
    stars_purchase_url: str | None = None
    is_custom: bool = False


PUBLIC_TARIFFS: dict[str, Tariff] = {}

ADMIN_TARIFFS: dict[str, Tariff] = {}

TEST_TRIAL_TARIFF = Tariff(
    key="test",
    title="Тестовый · 10 дней · 10 ГБ",
    days=10,
    traffic_gb=10,
    stars_price=0,
    code="t1",
    is_public=False,
    is_admin_only=False,
    connection_limit=10,
    stars_purchase_url=None,
    is_custom=False,
)


def is_admin(user_id: int) -> bool:
    return int(user_id) in ADMIN_IDS


def get_tariff_base(key: str) -> Optional[Tariff]:
    if str(key) == "test":
        return TEST_TRIAL_TARIFF
    base = PUBLIC_TARIFFS.get(key) or ADMIN_TARIFFS.get(key)
    if base:
        return base
    row = TARIFF_OVERRIDES_CACHE.get(key)
    if row and int(row.get("is_custom") or 0) == 1:
        traffic = row.get("traffic_gb")
        if traffic == -1:
            traffic = None
        return Tariff(
            key=str(key),
            title=str(row.get("title") or key),
            days=int(row.get("days") or 30),
            traffic_gb=traffic,
            stars_price=int(row.get("stars_price") or 1),
            code=str(row.get("code") or "c1"),
            is_public=not bool(int(row.get("is_admin_only") or 0)),
            is_admin_only=bool(int(row.get("is_admin_only") or 0)),
            connection_limit=max(1, int(row.get("connection_limit") or 10)),
            stars_purchase_url=normalize_stars_purchase_url(row.get("stars_purchase_url")),
            is_custom=True,
        )
    return None


TARIFF_OVERRIDES_CACHE: dict[str, dict] = {}


def normalize_stars_purchase_url(value: str | None) -> str | None:
    raw = str(value or "").strip()
    lowered = raw.lower()
    if not raw or lowered in {"-", "none", "null", "нет", "skip"}:
        return None

    # Явный мусор вроде "1" / "0" / просто цифр не должен попадать в URL-кнопку
    if raw.isdigit():
        return None

    if raw.startswith("t.me/"):
        raw = "https://" + raw
    if raw.startswith("telegram.me/"):
        raw = "https://" + raw
    if raw.startswith("www.t.me/"):
        raw = "https://" + raw
    if raw.startswith("@"):
        raw = "https://t.me/" + raw[1:]
    if raw.startswith("http://"):
        raw = "https://" + raw[len("http://"):]

    if not raw.startswith("https://"):
        return None

    try:
        parsed = urlparse(raw)
        if parsed.scheme != "https" or not parsed.netloc:
            return None
    except Exception:
        return None

    return raw


def is_resettable_stars_url_input(value: str | None) -> bool:
    lowered = str(value or "").strip().lower()
    return lowered in {"", "-", "none", "null", "нет", "skip"}


async def init_tariffs_db() -> None:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(TARIFFS_DB) as conn:
        await conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tariff_overrides (
                key TEXT PRIMARY KEY,
                title TEXT,
                days INTEGER,
                traffic_gb INTEGER,
                stars_price INTEGER,
                enabled INTEGER,
                is_admin_only INTEGER,
                connection_limit INTEGER,
                stars_purchase_url TEXT,
                code TEXT,
                is_custom INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL
            );
            """
        )
        async with conn.execute("PRAGMA table_info(tariff_overrides)") as cur:
            cols = {row[1] for row in await cur.fetchall()}
        if "connection_limit" not in cols:
            await conn.execute("ALTER TABLE tariff_overrides ADD COLUMN connection_limit INTEGER")
        if "code" not in cols:
            await conn.execute("ALTER TABLE tariff_overrides ADD COLUMN code TEXT")
        if "stars_purchase_url" not in cols:
            await conn.execute("ALTER TABLE tariff_overrides ADD COLUMN stars_purchase_url TEXT")
        if "is_custom" not in cols:
            await conn.execute("ALTER TABLE tariff_overrides ADD COLUMN is_custom INTEGER NOT NULL DEFAULT 0")
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT * FROM tariff_overrides") as cur:
            rows = await cur.fetchall()
        global TARIFF_OVERRIDES_CACHE
        TARIFF_OVERRIDES_CACHE = {str(row["key"]): dict(row) for row in rows}
        await conn.commit()


def get_tariff_override(key: str):
    return TARIFF_OVERRIDES_CACHE.get(key)


def merge_tariff(base: Tariff, override_row) -> Tariff:
    if not override_row:
        return base
    traffic = override_row["traffic_gb"]
    if traffic == -1:
        traffic = None
    elif traffic is None:
        traffic = base.traffic_gb
    return Tariff(
        key=base.key,
        title=override_row["title"] if override_row["title"] is not None else base.title,
        days=int(override_row["days"]) if override_row["days"] is not None else base.days,
        traffic_gb=traffic,
        stars_price=int(override_row["stars_price"]) if override_row["stars_price"] is not None else base.stars_price,
        code=str(override_row["code"]) if override_row.get("code") is not None else base.code,
        is_public=not (bool(int(override_row["is_admin_only"])) if override_row["is_admin_only"] is not None else base.is_admin_only),
        is_admin_only=bool(int(override_row["is_admin_only"])) if override_row["is_admin_only"] is not None else base.is_admin_only,
        connection_limit=int(override_row["connection_limit"]) if override_row.get("connection_limit") is not None else base.connection_limit,
        stars_purchase_url=normalize_stars_purchase_url(override_row.get("stars_purchase_url")) if override_row.get("stars_purchase_url") is not None else base.stars_purchase_url,
        is_custom=base.is_custom or bool(int(override_row.get("is_custom") or 0)),
    )


def tariff_enabled(key: str) -> bool:
    override_row = get_tariff_override(key)
    if override_row and override_row.get("enabled") is not None:
        return bool(int(override_row["enabled"]))
    return True


def get_tariff(key: str) -> Optional[Tariff]:
    base = get_tariff_base(key)
    if not base:
        return None
    return merge_tariff(base, get_tariff_override(key))


def all_tariff_keys() -> list[str]:
    keys = list(PUBLIC_TARIFFS.keys()) + list(ADMIN_TARIFFS.keys())
    for key, row in TARIFF_OVERRIDES_CACHE.items():
        if int(row.get("is_custom") or 0) == 1 and key not in keys:
            keys.append(str(key))
    return keys


def list_tariffs(user_id: int) -> list[Tariff]:
    items = []
    for key in all_tariff_keys():
        tariff = get_tariff(key)
        if not tariff or not tariff_enabled(key):
            continue
        if tariff.is_admin_only and not is_admin(user_id):
            continue
        items.append(tariff)
    return items


def list_all_tariffs_for_admin() -> list[Tariff]:
    result = []
    for key in all_tariff_keys():
        tariff = get_tariff(key)
        if tariff:
            result.append(tariff)
    return result


async def upsert_tariff_override(key: str, **fields) -> None:
    allowed = {"title", "days", "traffic_gb", "stars_price", "enabled", "is_admin_only", "connection_limit", "stars_purchase_url", "code", "is_custom"}
    patch = {k: v for k, v in fields.items() if k in allowed}
    if "stars_purchase_url" in patch:
        patch["stars_purchase_url"] = normalize_stars_purchase_url(patch.get("stars_purchase_url"))
    if not patch:
        return
    existing = get_tariff_override(key)
    now_ts = datetime.now().timestamp()
    if existing:
        sets = ", ".join([f"{k}=?" for k in patch.keys()]) + ", updated_at=?"
        values = list(patch.values()) + [now_ts, key]
        async with aiosqlite.connect(TARIFFS_DB) as conn:
            await conn.execute(f"UPDATE tariff_overrides SET {sets} WHERE key=?", values)
            await conn.commit()
    else:
        columns = ["key"] + list(patch.keys()) + ["updated_at"]
        placeholders = ",".join(["?"] * len(columns))
        values = [key] + list(patch.values()) + [now_ts]
        async with aiosqlite.connect(TARIFFS_DB) as conn:
            await conn.execute(
                f"INSERT INTO tariff_overrides ({','.join(columns)}) VALUES ({placeholders})",
                values,
            )
            await conn.commit()
    current = dict(existing) if existing else {"key": key, "title": None, "days": None, "traffic_gb": None, "stars_price": None, "enabled": None, "is_admin_only": None, "connection_limit": None, "stars_purchase_url": None, "code": None, "is_custom": 0, "updated_at": now_ts}
    current.update(patch)
    current["updated_at"] = now_ts
    TARIFF_OVERRIDES_CACHE[key] = current

async def delete_custom_tariff(key: str) -> None:
    existing = get_tariff_override(key)
    if not existing or int(existing.get("is_custom") or 0) != 1:
        raise ValueError("Можно удалить только пользовательский тариф.")
    async with aiosqlite.connect(TARIFFS_DB) as conn:
        await conn.execute("DELETE FROM tariff_overrides WHERE key=?", (key,))
        await conn.commit()
    TARIFF_OVERRIDES_CACHE.pop(key, None)


async def create_custom_tariff(*, key: str, title: str, days: int, traffic_gb: int | None, stars_price: int, is_admin_only: bool, connection_limit: int, stars_purchase_url: str | None = None) -> None:
    if get_tariff_base(key) or get_tariff_override(key):
        raise ValueError("Тариф с таким ключом уже существует.")
    code = re.sub(r"[^a-z0-9]+", "", key.lower())[:6] or "c1"
    await upsert_tariff_override(
        key,
        title=title,
        days=int(days),
        traffic_gb=-1 if traffic_gb is None else int(traffic_gb),
        stars_price=int(stars_price),
        enabled=1,
        is_admin_only=1 if is_admin_only else 0,
        connection_limit=max(1, int(connection_limit)),
        stars_purchase_url=normalize_stars_purchase_url(stars_purchase_url),
        code=code,
        is_custom=1,
    )

def human_traffic(traffic_gb: int | None) -> str:
    return "∞" if traffic_gb is None else f"{traffic_gb} ГБ"



def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    for handler in list(root.handlers):
        root.removeHandler(handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(formatter)

    root.addHandler(stream_handler)
    root.addHandler(file_handler)


def write_health_snapshot(*, status: str, extra: dict | None = None) -> None:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": status,
        "timestamp": datetime.now().isoformat(),
        "build": APP_BUILD,
    }
    if extra:
        payload.update(extra)
    HEALTH_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


async def health_loop() -> None:
    while True:
        try:
            write_health_snapshot(status="ok")
        except Exception as exc:
            logging.exception("health_loop failed: %s", exc)
        await asyncio.sleep(30)


def _sqlite_backup_database(src_path: Path, dst_path: Path) -> None:
    src_conn = sqlite3.connect(str(src_path))
    dst_conn = sqlite3.connect(str(dst_path))
    try:
        src_conn.backup(dst_conn)
    finally:
        with contextlib.suppress(Exception):
            dst_conn.close()
        with contextlib.suppress(Exception):
            src_conn.close()


def _tar_logs_to_path(log_dir: Path, target_path: Path) -> None:
    with tarfile.open(target_path, "w:gz") as tar:
        tar.add(log_dir, arcname="logs")


async def create_backup_snapshot() -> Path | None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    snapshot_dir = BACKUP_DIR / stamp
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    created_any = False
    for db_path in BACKUP_DB_PATHS:
        if not db_path.exists():
            continue
        await asyncio.to_thread(_sqlite_backup_database, db_path, snapshot_dir / db_path.name)
        created_any = True

    if HEALTH_FILE.exists():
        (snapshot_dir / HEALTH_FILE.name).write_text(HEALTH_FILE.read_text(encoding="utf-8"), encoding="utf-8")
        created_any = True

    if LOG_DIR.exists():
        await asyncio.to_thread(_tar_logs_to_path, LOG_DIR, snapshot_dir / "logs.tar.gz")
        created_any = True

    if not created_any:
        with contextlib.suppress(Exception):
            snapshot_dir.rmdir()
        return None

    return snapshot_dir


def cleanup_old_backups() -> None:
    if not BACKUP_DIR.exists():
        return
    cutoff_ts = datetime.now().timestamp() - (BACKUP_RETENTION_DAYS * 86400)
    for item in BACKUP_DIR.iterdir():
        with contextlib.suppress(Exception):
            if item.stat().st_mtime >= cutoff_ts:
                continue
            if item.is_dir():
                for nested in sorted(item.rglob('*'), reverse=True):
                    if nested.is_file() or nested.is_symlink():
                        nested.unlink(missing_ok=True)
                    elif nested.is_dir():
                        nested.rmdir()
                item.rmdir()
            else:
                item.unlink(missing_ok=True)


async def backup_loop() -> None:
    while True:
        try:
            snapshot_dir = await create_backup_snapshot()
            cleanup_old_backups()
            if snapshot_dir is not None:
                logging.info("backup snapshot created: %s", snapshot_dir)
        except Exception as exc:
            logging.exception("backup_loop failed: %s", exc)
        await asyncio.sleep(BACKUP_INTERVAL_SECONDS)


async def antiabuse_unique_ip_count_for_name(name: str) -> int:
    if not ANTIABUSE_ENABLED or not name:
        return 0
    window_start = datetime.now().timestamp() - (ANTIABUSE_WINDOW_MINUTES * 60)
    async with aiosqlite.connect(ANTIABUSE_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT COUNT(DISTINCT ip) AS c FROM matches WHERE name=? AND ts_epoch>=?",
            (name, window_start),
        ) as cur:
            row = await cur.fetchone()
            return int(row["c"] or 0) if row else 0

async def get_http_session() -> aiohttp.ClientSession:
    global http_session
    if http_session is None or http_session.closed:
        timeout = aiohttp.ClientTimeout(total=30)
        connector = aiohttp.TCPConnector(ssl=False)
        http_session = aiohttp.ClientSession(timeout=timeout, connector=connector)
    return http_session


async def close_http_session() -> None:
    global http_session
    if http_session is not None and not http_session.closed:
        await http_session.close()
    http_session = None


def normalize_support_waiting_for(status: str, waiting_for: str | None) -> str:
    raw_status = str(status or "")
    raw_waiting = str(waiting_for or "").strip()

    if raw_waiting in {"admin", "user", "closed"}:
        return raw_waiting
    if raw_status == "closed":
        return "closed"
    if raw_status == "answered":
        return "user"
    return "admin"


def support_waiting_label(waiting_for: str) -> str:
    normalized = normalize_support_waiting_for("", waiting_for)
    return {
        "admin": "🟢 ждёт администратора",
        "user": "🟡 ждёт пользователя",
        "closed": "⚫ закрыт",
    }.get(normalized, normalized)


def support_status_label(status: str) -> str:
    raw_status = str(status or "")
    return {
        "open": "🟢 открыт",
        "answered": "🟡 отвечён",
        "closed": "⚫ закрыт",
    }.get(raw_status, raw_status)


async def log_runtime_audit() -> None:
    logging.info("BUILD=%s", APP_BUILD)
    logging.info("DB backend: aiosqlite")
    logging.info("HTTP session mode: shared aiohttp ClientSession")
    logging.info("sqlite3 sync connect usage in runtime path: disabled")
    logging.info("Log file: %s", LOG_FILE)
    logging.info("Health file: %s", HEALTH_FILE)


# =========================
# DB: PAYMENTS
# =========================


async def init_payments_db() -> None:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(PAYMENTS_DB) as conn:
        await conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payment_uid TEXT NOT NULL UNIQUE,
                user_id INTEGER NOT NULL,
                username TEXT,
                kind TEXT NOT NULL DEFAULT 'buy',
                tarif_key TEXT NOT NULL,
                client_id INTEGER,
                amount INTEGER NOT NULL,
                currency TEXT NOT NULL DEFAULT 'XTR',
                status TEXT NOT NULL DEFAULT 'pending',
                provider TEXT NOT NULL DEFAULT 'stars',
                provider_charge_id TEXT,
                created_at REAL NOT NULL,
                paid_at REAL,
                failed_at REAL,
                notes TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_payments_user_created
            ON payments(user_id, created_at DESC);

            CREATE INDEX IF NOT EXISTS idx_payments_status_created
            ON payments(status, created_at DESC);
            """
        )
        async with conn.execute("PRAGMA table_info(payments)") as cur:
            cols_rows = await cur.fetchall()
        cols = {row[1] for row in cols_rows}
        if "kind" not in cols:
            await conn.execute("ALTER TABLE payments ADD COLUMN kind TEXT NOT NULL DEFAULT 'buy'")
        if "client_id" not in cols:
            await conn.execute("ALTER TABLE payments ADD COLUMN client_id INTEGER")
        if "application_status" not in cols:
            await conn.execute("ALTER TABLE payments ADD COLUMN application_status TEXT NOT NULL DEFAULT 'not_applied'")
        if "apply_started_at" not in cols:
            await conn.execute("ALTER TABLE payments ADD COLUMN apply_started_at REAL")
        if "applied_at" not in cols:
            await conn.execute("ALTER TABLE payments ADD COLUMN applied_at REAL")
        if "notified_at" not in cols:
            await conn.execute("ALTER TABLE payments ADD COLUMN notified_at REAL")
        if "result_payload" not in cols:
            await conn.execute("ALTER TABLE payments ADD COLUMN result_payload TEXT")
        await conn.commit()


def new_payment_uid() -> str:
    return uuid.uuid4().hex[:16]


async def create_payment(
    *,
    user_id: int,
    username: str | None,
    tarif_key: str,
    amount: int,
    kind: str = "buy",
    client_id: int | None = None,
    notes: str = "",
) -> int:
    now_ts = datetime.now().timestamp()
    payment_uid = new_payment_uid()
    async with aiosqlite.connect(PAYMENTS_DB) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            """
            INSERT INTO payments (
                payment_uid, user_id, username, kind, tarif_key, client_id, amount,
                currency, status, provider, created_at, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'XTR', 'pending', 'stars', ?, ?)
            """,
            (
                payment_uid,
                int(user_id),
                username or "",
                kind,
                tarif_key,
                client_id,
                int(amount),
                now_ts,
                notes,
            ),
        )
        await conn.commit()
        return int(cur.lastrowid)


async def get_payment(payment_id: int):
    async with aiosqlite.connect(PAYMENTS_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT * FROM payments WHERE id=?", (int(payment_id),)) as cur:
            return await cur.fetchone()


async def get_payment_by_uid(payment_uid: str):
    async with aiosqlite.connect(PAYMENTS_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT * FROM payments WHERE payment_uid=?", (payment_uid,)) as cur:
            return await cur.fetchone()


async def get_latest_pending_payment_for_user(user_id: int):
    async with aiosqlite.connect(PAYMENTS_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """
            SELECT *
            FROM payments
            WHERE user_id=? AND status='pending'
            ORDER BY id DESC
            LIMIT 1
            """,
            (int(user_id),),
        ) as cur:
            return await cur.fetchone()


async def get_pending_payment_for_target(
    *,
    user_id: int,
    kind: str,
    tarif_key: str,
    client_id: int | None = None,
):
    async with aiosqlite.connect(PAYMENTS_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """
            SELECT *
            FROM payments
            WHERE user_id=?
              AND kind=?
              AND tarif_key=?
              AND COALESCE(client_id, 0)=COALESCE(?, 0)
              AND status='pending'
            ORDER BY id DESC
            LIMIT 1
            """,
            (int(user_id), kind, tarif_key, client_id),
        ) as cur:
            return await cur.fetchone()


def payment_result_payload_dumps(payload: dict | None) -> str | None:
    if not payload:
        return None
    try:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return None


def payment_result_payload_loads(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def payment_success_text_from_payload(kind: str, tariff: Tariff, payload: dict) -> str:
    raw_kind = str(kind or payload.get("kind") or "buy")
    if raw_kind == "buy":
        return created_key_text(
            tariff,
            str(payload.get("sub_link") or "—"),
            str(payload.get("clash_link") or "—"),
        )
    item = {
        "name": str(payload.get("name") or payload.get("client_name") or "unknown"),
        "sub_link": str(payload.get("sub_link") or "—"),
        "clash_link": str(payload.get("clash_link") or "—"),
    }
    return renew_success_text(item)


async def mark_payment_processing(payment_id: int, provider_charge_id: str = "") -> bool:
    now_ts = datetime.now().timestamp()
    note = " payment_processing"
    async with aiosqlite.connect(PAYMENTS_DB) as conn:
        cur = await conn.execute(
            """
            UPDATE payments
            SET status='processing',
                paid_at=COALESCE(paid_at, ?),
                apply_started_at=COALESCE(apply_started_at, ?),
                provider_charge_id=COALESCE(NULLIF(?, ''), provider_charge_id),
                notes=TRIM(COALESCE(notes,'') || ?)
            WHERE id=? AND status='pending'
            """,
            (now_ts, now_ts, provider_charge_id, note, int(payment_id)),
        )
        await conn.commit()
        return int(cur.rowcount or 0) > 0


async def mark_payment_applied(
    payment_id: int,
    provider_charge_id: str = "",
    result_payload: dict | None = None,
    note: str = "",
) -> None:
    now_ts = datetime.now().timestamp()
    result_payload_raw = payment_result_payload_dumps(result_payload)
    suffix = f" {note.strip()}" if note and note.strip() else ""
    async with aiosqlite.connect(PAYMENTS_DB) as conn:
        await conn.execute(
            """
            UPDATE payments
            SET status='success',
                application_status='applied',
                paid_at=COALESCE(paid_at, ?),
                applied_at=?,
                provider_charge_id=COALESCE(NULLIF(?, ''), provider_charge_id),
                result_payload=COALESCE(?, result_payload),
                notes=TRIM(COALESCE(notes,'') || ?)
            WHERE id=?
            """,
            (now_ts, now_ts, provider_charge_id, result_payload_raw, suffix, int(payment_id)),
        )
        await conn.commit()


async def mark_payment_apply_failed(payment_id: int, note: str = "") -> None:
    now_ts = datetime.now().timestamp()
    suffix = f" apply_failed {note.strip()}" if note and note.strip() else " apply_failed"
    async with aiosqlite.connect(PAYMENTS_DB) as conn:
        await conn.execute(
            """
            UPDATE payments
            SET status='processing',
                application_status='apply_failed',
                failed_at=?,
                notes=TRIM(COALESCE(notes,'') || ?)
            WHERE id=?
            """,
            (now_ts, suffix, int(payment_id)),
        )
        await conn.commit()


async def mark_payment_notified(payment_id: int, note: str = "") -> None:
    now_ts = datetime.now().timestamp()
    suffix = f" {note.strip()}" if note and note.strip() else " notification_sent"
    async with aiosqlite.connect(PAYMENTS_DB) as conn:
        await conn.execute(
            """
            UPDATE payments
            SET notified_at=COALESCE(notified_at, ?),
                notes=TRIM(COALESCE(notes,'') || ?)
            WHERE id=?
            """,
            (now_ts, suffix, int(payment_id)),
        )
        await conn.commit()


async def mark_payment_success(payment_id: int, provider_charge_id: str = "") -> None:
    await mark_payment_applied(payment_id, provider_charge_id=provider_charge_id)


async def mark_payment_success_manual(payment_id: int, admin_id: int, note: str = "", result_payload: dict | None = None) -> None:
    suffix = f"admin_manual_by={int(admin_id)}"
    if note:
        suffix += f" {note.strip()}"
    await mark_payment_applied(
        payment_id,
        provider_charge_id=f"admin_manual:{int(admin_id)}",
        result_payload=result_payload,
        note=suffix,
    )


async def cancel_pending_payment(payment_id: int, user_id: int) -> bool:
    now_ts = datetime.now().timestamp()
    async with aiosqlite.connect(PAYMENTS_DB) as conn:
        cur = await conn.execute(
            """
            UPDATE payments
            SET status='failed', failed_at=?, notes=COALESCE(notes,'') || ' user_cancelled'
            WHERE id=? AND user_id=? AND status='pending'
            """,
            (now_ts, int(payment_id), int(user_id)),
        )
        await conn.commit()
        return int(cur.rowcount or 0) > 0


async def list_user_payments(user_id: int, limit: int = 20):
    async with aiosqlite.connect(PAYMENTS_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """
            SELECT *
            FROM payments
            WHERE user_id=?
            ORDER BY id DESC
            LIMIT ?
            """,
            (int(user_id), int(limit)),
        ) as cur:
            return await cur.fetchall()
def payment_status_label(status: str) -> str:
    return {
        "pending": "🟡 ожидает оплату",
        "processing": "🟠 в обработке",
        "success": "🟢 успешно",
        "failed": "🔴 ошибка",
    }.get(str(status), str(status))


def payment_kind_label(kind: str) -> str:
    return {"buy": "Покупка", "renew": "Продление"}.get(str(kind), str(kind))

def ticket_status_label(status: str) -> str:
    return {
        "open": "🟢 открыт",
        "answered": "🟡 отвечён",
        "closed": "⚫ закрыт",
    }.get(str(status), str(status))


def ticket_waiting_label(waiting_state: str, status: str) -> str:
    if str(status) == "closed":
        return "⚫ закрыт"
    return {
        "admin": "🟢 ждёт администратора",
        "user": "🟡 ждёт пользователя",
    }.get(str(waiting_state), "🟢 ждёт администратора")



def payment_amount_text(amount: int) -> str:
    return f"{int(amount)} ⭐"


def payment_dt_text(value) -> str:
    try:
        ts = float(value or 0)
    except Exception:
        ts = 0
    return "—" if ts <= 0 else datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")


# =========================
# DB: SUPPORT
# =========================



async def init_trials_db() -> None:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(TRIALS_DB) as conn:
        await conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS trials (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                client_name TEXT,
                granted_at REAL NOT NULL,
                notes TEXT
            );
            """
        )
        await conn.commit()




async def init_tgproxy_db() -> None:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(TGPROXY_DB) as conn:
        await conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS telegram_proxies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_telegram_proxies_sort
            ON telegram_proxies(sort_order, id);
            """
        )
        await conn.commit()


def normalize_tgproxy_url(value: str | None) -> str | None:
    raw = str(value or "").strip()
    return raw if raw.startswith("tg://") else None


async def list_telegram_proxies(include_disabled: bool = False):
    async with aiosqlite.connect(TGPROXY_DB) as conn:
        conn.row_factory = aiosqlite.Row
        query = "SELECT * FROM telegram_proxies"
        params = []
        if not include_disabled:
            query += " WHERE enabled=1"
        query += " ORDER BY sort_order ASC, id ASC"
        async with conn.execute(query, tuple(params)) as cur:
            return await cur.fetchall()


async def get_telegram_proxy(proxy_id: int):
    async with aiosqlite.connect(TGPROXY_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT * FROM telegram_proxies WHERE id=?", (int(proxy_id),)) as cur:
            return await cur.fetchone()


async def create_telegram_proxy(title: str, url: str) -> int:
    normalized_url = normalize_tgproxy_url(url)
    if not normalized_url:
        raise ValueError("Ссылка должна начинаться с tg://")
    title = str(title or "").strip()
    if not title:
        raise ValueError("Название не может быть пустым.")
    now_ts = datetime.now().timestamp()
    async with aiosqlite.connect(TGPROXY_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT COALESCE(MAX(sort_order), 0) AS max_order FROM telegram_proxies") as cur:
            row = await cur.fetchone()
            next_order = int(row["max_order"] or 0) + 1 if row else 1
        cur = await conn.execute(
            """
            INSERT INTO telegram_proxies (title, url, enabled, sort_order, created_at, updated_at)
            VALUES (?, ?, 1, ?, ?, ?)
            """,
            (title[:128], normalized_url, next_order, now_ts, now_ts),
        )
        await conn.commit()
        return int(cur.lastrowid)


async def update_telegram_proxy(proxy_id: int, *, title: str | None = None, url: str | None = None) -> None:
    patch = {}
    if title is not None:
        title = str(title).strip()
        if not title:
            raise ValueError("Название не может быть пустым.")
        patch["title"] = title[:128]
    if url is not None:
        normalized_url = normalize_tgproxy_url(url)
        if not normalized_url:
            raise ValueError("Ссылка должна начинаться с tg://")
        patch["url"] = normalized_url
    if not patch:
        return
    patch["updated_at"] = datetime.now().timestamp()
    sets = ", ".join(f"{key}=?" for key in patch.keys())
    values = list(patch.values()) + [int(proxy_id)]
    async with aiosqlite.connect(TGPROXY_DB) as conn:
        await conn.execute(f"UPDATE telegram_proxies SET {sets} WHERE id=?", values)
        await conn.commit()


async def set_telegram_proxy_enabled(proxy_id: int, enabled: bool) -> None:
    async with aiosqlite.connect(TGPROXY_DB) as conn:
        await conn.execute(
            "UPDATE telegram_proxies SET enabled=?, updated_at=? WHERE id=?",
            (1 if enabled else 0, datetime.now().timestamp(), int(proxy_id)),
        )
        await conn.commit()


async def delete_telegram_proxy(proxy_id: int) -> None:
    async with aiosqlite.connect(TGPROXY_DB) as conn:
        await conn.execute("DELETE FROM telegram_proxies WHERE id=?", (int(proxy_id),))
        await conn.commit()


async def user_has_paid_active_subscription(user_id: int) -> bool:
    try:
        items = await get_user_subscriptions(int(user_id))
    except Exception:
        return False
    for item in items:
        if int(item.get("days_left") or 0) <= 0:
            continue
        if str(item.get("plan_key") or "") == "test":
            continue
        return True
    return False

async def get_trial_record(user_id: int):
    async with aiosqlite.connect(TRIALS_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT * FROM trials WHERE user_id=?", (int(user_id),)) as cur:
            return await cur.fetchone()


async def save_trial_record(user_id: int, username: str | None, client_name: str, notes: str = "") -> None:
    async with aiosqlite.connect(TRIALS_DB) as conn:
        await conn.execute(
            """
            INSERT INTO trials(user_id, username, client_name, granted_at, notes)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                client_name=excluded.client_name,
                granted_at=excluded.granted_at,
                notes=excluded.notes
            """,
            (int(user_id), username or "", client_name, datetime.now().timestamp(), notes),
        )
        await conn.commit()


async def user_has_trial_access(user_id: int) -> bool:
    row = await get_trial_record(int(user_id))
    if row:
        return True
    try:
        clients = await get_all_clients()
        for client in clients:
            desc = str(client.get("desc") or "")
            if parse_tgid_from_desc(desc) == int(user_id) and (" TEST" in desc or parse_plan_key_from_desc(desc) == "test"):
                return True
    except Exception:
        pass
    return False


def trial_menu_text() -> str:
    return (
        "🎁 Тестовый доступ\n\n"
        "Тестовый доступ выдаётся один раз на пользователя.\n\n"
        "Условия:\n"
        "• срок: 10 дней\n"
        "• трафик: 10 ГБ\n"
        "• все доступные протоколы\n\n"
        "Нажмите кнопку ниже, чтобы получить тестовый доступ."
    )


def trial_denied_text() -> str:
    return (
        "🎁 Тестовый доступ\n\n"
        "Тестовый доступ уже был выдан ранее.\n\n"
        "Если вы хотите продолжить пользоваться сервисом, используйте покупку обычной подписки."
    )


def trial_granted_text(sub_link: str, clash_link: str) -> str:
    return (
        "🎁 Тестовый доступ выдан\n\n"
        f"Тариф: {TEST_TRIAL_TARIFF.title}\n"
        f"Срок: {TEST_TRIAL_TARIFF.days} дн.\n"
        f"Трафик: {human_traffic(TEST_TRIAL_TARIFF.traffic_gb)}\n\n"
        "Подписка:\n"
        f"{sub_link}\n\n"
        "Clash:\n"
        f"{clash_link}"
    )


def trial_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Получить тестовый доступ", callback_data="trial:get")],
            [InlineKeyboardButton(text="✖️ Закрыть", callback_data="menu:back")],
        ]
    )


async def create_trial_client(user_id: int, username: str | None) -> tuple[str, str, str]:
    return await create_client(user_id=user_id, username=username, tariff=TEST_TRIAL_TARIFF, is_test=True)

async def init_support_db() -> None:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(SUPPORT_DB) as conn:
        await conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS support_tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                waiting_for TEXT NOT NULL DEFAULT 'admin',
                subject TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                closed_at REAL
            );

            CREATE TABLE IF NOT EXISTS support_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL,
                sender_user_id INTEGER NOT NULL,
                sender_role TEXT NOT NULL,
                message_text TEXT NOT NULL,
                is_note INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                FOREIGN KEY(ticket_id) REFERENCES support_tickets(id)
            );

            CREATE INDEX IF NOT EXISTS idx_support_tickets_user_status
            ON support_tickets(user_id, status);

            CREATE INDEX IF NOT EXISTS idx_support_messages_ticket_created
            ON support_messages(ticket_id, created_at);
            """
        )
        async with conn.execute("PRAGMA table_info(support_tickets)") as cur:
            ticket_cols = {row[1] for row in await cur.fetchall()}
        if "waiting_for" not in ticket_cols:
            await conn.execute("ALTER TABLE support_tickets ADD COLUMN waiting_for TEXT NOT NULL DEFAULT 'admin'")

        if "last_user_reopen_at" not in ticket_cols:
            await conn.execute("ALTER TABLE support_tickets ADD COLUMN last_user_reopen_at REAL")

        async with conn.execute("PRAGMA table_info(support_messages)") as cur:
            msg_cols = {row[1] for row in await cur.fetchall()}
        if "is_note" not in msg_cols:
            await conn.execute("ALTER TABLE support_messages ADD COLUMN is_note INTEGER NOT NULL DEFAULT 0")
        if "content_type" not in msg_cols:
            await conn.execute("ALTER TABLE support_messages ADD COLUMN content_type TEXT NOT NULL DEFAULT 'text'")
        if "file_id" not in msg_cols:
            await conn.execute("ALTER TABLE support_messages ADD COLUMN file_id TEXT")
        if "file_name" not in msg_cols:
            await conn.execute("ALTER TABLE support_messages ADD COLUMN file_name TEXT")
        if "mime_type" not in msg_cols:
            await conn.execute("ALTER TABLE support_messages ADD COLUMN mime_type TEXT")

        await conn.commit()

async def get_open_ticket_for_user(user_id: int):
    async with aiosqlite.connect(SUPPORT_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """
            SELECT *
            FROM support_tickets
            WHERE user_id=? AND status IN ('open', 'answered')
            ORDER BY id DESC
            LIMIT 1
            """,
            (int(user_id),),
        ) as cur:
            return await cur.fetchone()


async def get_latest_ticket_for_user(user_id: int):
    async with aiosqlite.connect(SUPPORT_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM support_tickets WHERE user_id=? ORDER BY created_at DESC, id DESC LIMIT 1",
            (int(user_id),),
        ) as cur:
            return await cur.fetchone()


async def get_new_ticket_cooldown_remaining(user_id: int) -> int:
    row = await get_latest_ticket_for_user(int(user_id))
    if not row:
        return 0
    last_created = float(row["created_at"] or 0)
    remaining = int((last_created + SUPPORT_NEW_TICKET_COOLDOWN_SECONDS) - datetime.now().timestamp())
    return max(0, remaining)


async def create_ticket(user_id: int, username: str | None, subject: str, message_text: str, *, content_type: str = "text", file_id: str | None = None, file_name: str | None = None, mime_type: str | None = None) -> int:
    now_ts = datetime.now().timestamp()
    async with aiosqlite.connect(SUPPORT_DB) as conn:
        cur = await conn.execute(
            """
            INSERT INTO support_tickets (user_id, username, status, waiting_for, subject, created_at, updated_at)
            VALUES (?, ?, 'open', 'admin', ?, ?, ?)
            """,
            (int(user_id), username or "", subject.strip(), now_ts, now_ts),
        )
        ticket_id = int(cur.lastrowid)
        await conn.execute(
            """
            INSERT INTO support_messages (ticket_id, sender_user_id, sender_role, message_text, is_note, content_type, file_id, file_name, mime_type, created_at)
            VALUES (?, ?, 'user', ?, 0, ?, ?, ?, ?, ?)
            """,
            (ticket_id, int(user_id), message_text.strip(), str(content_type or "text"), file_id, file_name, mime_type, now_ts),
        )
        await conn.commit()
        return ticket_id


async def add_support_message(ticket_id: int, sender_user_id: int, sender_role: str, message_text: str, is_note: bool = False, *, content_type: str = "text", file_id: str | None = None, file_name: str | None = None, mime_type: str | None = None) -> None:
    now_ts = datetime.now().timestamp()
    if is_note:
        new_status = None
        waiting_for = None
    elif sender_role == "admin":
        new_status = "answered"
        waiting_for = "user"
    else:
        new_status = "open"
        waiting_for = "admin"

    async with aiosqlite.connect(SUPPORT_DB) as conn:
        await conn.execute(
            """
            INSERT INTO support_messages (ticket_id, sender_user_id, sender_role, message_text, is_note, content_type, file_id, file_name, mime_type, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (int(ticket_id), int(sender_user_id), sender_role, message_text.strip(), 1 if is_note else 0, str(content_type or "text"), file_id, file_name, mime_type, now_ts),
        )
        if new_status is not None:
            await conn.execute(
                """
                UPDATE support_tickets
                SET status=?, waiting_for=?, updated_at=?
                WHERE id=?
                """,
                (new_status, waiting_for, now_ts, int(ticket_id)),
            )
        await conn.commit()


async def close_ticket(ticket_id: int) -> None:
    now_ts = datetime.now().timestamp()
    async with aiosqlite.connect(SUPPORT_DB) as conn:
        await conn.execute(
            """
            UPDATE support_tickets
            SET status='closed', waiting_for='closed', updated_at=?, closed_at=?
            WHERE id=?
            """,
            (now_ts, now_ts, int(ticket_id)),
        )
        await conn.commit()


async def reopen_ticket(ticket_id: int) -> None:
    now_ts = datetime.now().timestamp()
    async with aiosqlite.connect(SUPPORT_DB) as conn:
        await conn.execute(
            """
            UPDATE support_tickets
            SET status='open', waiting_for='admin', updated_at=?, closed_at=NULL
            WHERE id=?
            """,
            (now_ts, int(ticket_id)),
        )
        await conn.commit()


async def get_ticket(ticket_id: int):
    async with aiosqlite.connect(SUPPORT_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT * FROM support_tickets WHERE id=?", (int(ticket_id),)) as cur:
            return await cur.fetchone()


async def list_user_tickets(user_id: int, limit: int = 20):
    async with aiosqlite.connect(SUPPORT_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """
            SELECT *
            FROM support_tickets
            WHERE user_id=?
            ORDER BY id DESC
            LIMIT ?
            """,
            (int(user_id), int(limit)),
        ) as cur:
            return await cur.fetchall()


async def list_admin_tickets(limit: int = 50):
    async with aiosqlite.connect(SUPPORT_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """
            SELECT *
            FROM support_tickets
            ORDER BY
                CASE waiting_for
                    WHEN 'admin' THEN 0
                    WHEN 'user' THEN 1
                    ELSE 2
                END,
                CASE status
                    WHEN 'open' THEN 0
                    WHEN 'answered' THEN 1
                    WHEN 'closed' THEN 2
                    ELSE 3
                END,
                updated_at DESC
            LIMIT ?
            """,
            (int(limit),),
        ) as cur:
            return await cur.fetchall()


async def list_admin_tickets_filtered(filter_mode: str = "all", limit: int = 50):
    async with aiosqlite.connect(SUPPORT_DB) as conn:
        conn.row_factory = aiosqlite.Row

        async with conn.execute("PRAGMA table_info(support_tickets)") as cur:
            cols = {row[1] for row in await cur.fetchall()}

        has_waiting_for = "waiting_for" in cols
        order_expr = (
            "CASE "
            "WHEN status='closed' THEN 2 "
            + ("WHEN waiting_for='user' THEN 1 " if has_waiting_for else "WHEN status='answered' THEN 1 ")
            + "ELSE 0 END, updated_at DESC"
        )

        query = "SELECT * FROM support_tickets"
        params = []

        if filter_mode == "open":
            query += " WHERE status='open'"
        elif filter_mode == "answered":
            query += " WHERE status='answered'"
        elif filter_mode == "closed":
            query += " WHERE status='closed'"
        elif filter_mode == "review":
            if has_waiting_for:
                query += " WHERE waiting_for='admin' AND status IN ('open','answered')"
            else:
                query += " WHERE status='open'"

        query += f" ORDER BY {order_expr} LIMIT ?"
        params.append(int(limit))

        async with conn.execute(query, tuple(params)) as cur:
            rows = await cur.fetchall()

    normalized = []
    for row in rows:
        d = dict(row)
        if "waiting_for" not in d or not d["waiting_for"]:
            status = str(d.get("status") or "")
            if status == "closed":
                d["waiting_for"] = "closed"
            elif status == "answered":
                d["waiting_for"] = "user"
            else:
                d["waiting_for"] = "admin"
        normalized.append(d)
    return normalized


async def search_admin_tickets(query: str, limit: int = 50):
    raw = (query or "").strip()
    if not raw:
        return []
    q = raw
    if q.startswith("#") or q.startswith("№"):
        q = q[1:].strip()

    async with aiosqlite.connect(SUPPORT_DB) as conn:
        conn.row_factory = aiosqlite.Row
        if q.isdigit():
            async with conn.execute(
                """
                SELECT * FROM support_tickets
                WHERE id=? OR user_id=?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (int(q), int(q), int(limit)),
            ) as cur:
                rows = await cur.fetchall()
        else:
            like = f"%{raw}%"
            async with conn.execute(
                """
                SELECT * FROM support_tickets
                WHERE subject LIKE ? OR username LIKE ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (like, like, int(limit)),
            ) as cur:
                rows = await cur.fetchall()

    normalized = []
    for row in rows:
        d = dict(row)
        if "waiting_for" not in d or not d["waiting_for"]:
            status = str(d.get("status") or "")
            if status == "closed":
                d["waiting_for"] = "closed"
            elif status == "answered":
                d["waiting_for"] = "user"
            else:
                d["waiting_for"] = "admin"
        normalized.append(d)
    return normalized

async def list_ticket_messages(ticket_id: int, limit: int = 200, include_notes: bool = True):
    async with aiosqlite.connect(SUPPORT_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("PRAGMA table_info(support_messages)") as cur:
            cols = {row[1] for row in await cur.fetchall()}
        has_is_note = "is_note" in cols
        query = """
            SELECT *
            FROM support_messages
            WHERE ticket_id=?
        """
        params = [int(ticket_id)]
        if not include_notes:
            if has_is_note:
                query += " AND COALESCE(is_note, 0)=0"
            else:
                query += " AND sender_role!='note'"
        query += " ORDER BY id ASC LIMIT ?"
        params.append(int(limit))
        async with conn.execute(query, tuple(params)) as cur:
            return await cur.fetchall()

async def init_reminders_db() -> None:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(REMINDERS_DB) as conn:
        await conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS reminder_marks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                client_id INTEGER NOT NULL,
                reminder_type TEXT NOT NULL,
                sent_at REAL NOT NULL,
                UNIQUE(user_id, client_id, reminder_type)
            );

            CREATE TABLE IF NOT EXISTS reminder_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_at REAL NOT NULL,
                checked_count INTEGER NOT NULL DEFAULT 0,
                sent_3d INTEGER NOT NULL DEFAULT 0,
                sent_1d INTEGER NOT NULL DEFAULT 0,
                sent_expired INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        await conn.commit()


async def reminder_already_sent(user_id: int, client_id: int, reminder_type: str) -> bool:
    async with aiosqlite.connect(REMINDERS_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT 1 FROM reminder_marks WHERE user_id=? AND client_id=? AND reminder_type=? LIMIT 1",
            (int(user_id), int(client_id), reminder_type),
        ) as cur:
            row = await cur.fetchone()
            return row is not None


async def mark_reminder_sent(user_id: int, client_id: int, reminder_type: str) -> None:
    async with aiosqlite.connect(REMINDERS_DB) as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO reminder_marks (user_id, client_id, reminder_type, sent_at) VALUES (?, ?, ?, ?)",
            (int(user_id), int(client_id), reminder_type, datetime.now().timestamp()),
        )
        await conn.commit()


async def log_reminder_run(*, checked_count: int, sent_3d: int, sent_1d: int, sent_expired: int, failed_count: int) -> None:
    async with aiosqlite.connect(REMINDERS_DB) as conn:
        await conn.execute(
            """
            INSERT INTO reminder_runs (run_at, checked_count, sent_3d, sent_1d, sent_expired, failed_count)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now().timestamp(),
                int(checked_count),
                int(sent_3d),
                int(sent_1d),
                int(sent_expired),
                int(failed_count),
            ),
        )
        await conn.commit()


async def get_last_reminder_run():
    async with aiosqlite.connect(REMINDERS_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT * FROM reminder_runs ORDER BY id DESC LIMIT 1") as cur:
            return await cur.fetchone()


async def count_reminder_marks() -> dict:
    async with aiosqlite.connect(REMINDERS_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """
            SELECT
                SUM(CASE WHEN reminder_type='expiring_3' THEN 1 ELSE 0 END) AS c3,
                SUM(CASE WHEN reminder_type='expiring_1' THEN 1 ELSE 0 END) AS c1,
                SUM(CASE WHEN reminder_type='expired' THEN 1 ELSE 0 END) AS ce
            FROM reminder_marks
            """
        ) as cur:
            row = await cur.fetchone()
            return {
                "expiring_3": int(row["c3"] or 0),
                "expiring_1": int(row["c1"] or 0),
                "expired": int(row["ce"] or 0),
            }



async def init_antiabuse_db() -> None:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(ANTIABUSE_DB) as conn:
        await conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS antiabuse_cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE,
                admin_status TEXT NOT NULL DEFAULT 'new',
                admin_note TEXT,
                ip_limit_override INTEGER,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS raw_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_text TEXT NOT NULL,
                ts_epoch REAL NOT NULL,
                protocol TEXT NOT NULL,
                event_type TEXT NOT NULL,
                ip TEXT,
                port INTEGER,
                name TEXT,
                destination TEXT,
                raw_line TEXT NOT NULL,
                matched INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_epoch REAL NOT NULL,
                protocol TEXT NOT NULL,
                name TEXT NOT NULL,
                ip TEXT NOT NULL,
                port INTEGER,
                event_from_id INTEGER NOT NULL,
                event_name_id INTEGER NOT NULL,
                confidence TEXT NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ip_state (
                name TEXT NOT NULL,
                ip TEXT NOT NULL,
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL,
                hits INTEGER NOT NULL DEFAULT 1,
                protocols TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(name, ip)
            );

            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                unique_ip_count INTEGER NOT NULL,
                window_minutes INTEGER NOT NULL,
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL,
                sent_at REAL NOT NULL,
                user_notified INTEGER NOT NULL DEFAULT 0,
                admin_notified INTEGER NOT NULL DEFAULT 0,
                details TEXT
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_matches_unique_pair
            ON matches(event_from_id, event_name_id);

            CREATE INDEX IF NOT EXISTS idx_matches_name_ts
            ON matches(name, ts_epoch);

            CREATE INDEX IF NOT EXISTS idx_ip_state_name_last_seen
            ON ip_state(name, last_seen);

            CREATE INDEX IF NOT EXISTS idx_alerts_name_sent
            ON alerts(name, sent_at DESC);

            CREATE TABLE IF NOT EXISTS antiabuse_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                sent_at REAL NOT NULL,
                user_notified INTEGER NOT NULL DEFAULT 0,
                admin_notified INTEGER NOT NULL DEFAULT 0,
                details TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_antiabuse_notifications_name_kind_sent
            ON antiabuse_notifications(name, kind, sent_at DESC);

            CREATE TABLE IF NOT EXISTS antiabuse_enforcement (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                disabled_until REAL NOT NULL,
                disabled_at REAL NOT NULL,
                disabled_by INTEGER,
                enabled_at REAL,
                enabled_by INTEGER,
                reason TEXT,
                details TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_antiabuse_enforcement_name_active
            ON antiabuse_enforcement(name, is_active, disabled_until);
            """
        )

        async with conn.execute("PRAGMA table_info(antiabuse_cases)") as cur:
            cols_rows = await cur.fetchall()

        cols = {row[1]: {"type": row[2], "notnull": row[3], "pk": row[5]} for row in cols_rows}

        needs_rebuild = False
        if "tgid" in cols and int(cols["tgid"]["notnull"] or 0) == 1:
            needs_rebuild = True
        if "name" not in cols:
            needs_rebuild = True
        if "updated_at" not in cols:
            needs_rebuild = True

        if needs_rebuild:
            now_ts = datetime.now().timestamp()
            await conn.execute("ALTER TABLE antiabuse_cases RENAME TO antiabuse_cases_legacy")
            await conn.execute(
                """
                CREATE TABLE antiabuse_cases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE,
                    admin_status TEXT NOT NULL DEFAULT 'new',
                    admin_note TEXT,
                    ip_limit_override INTEGER,
                    updated_at REAL NOT NULL
                )
                """
            )

            async with conn.execute("PRAGMA table_info(antiabuse_cases_legacy)") as cur:
                legacy_cols = {row[1] for row in await cur.fetchall()}

            if "name" in legacy_cols:
                await conn.execute(
                    """
                    INSERT OR IGNORE INTO antiabuse_cases (name, admin_status, admin_note, ip_limit_override, updated_at)
                    SELECT
                        TRIM(name),
                        COALESCE(admin_status, 'new'),
                        admin_note,
                        ip_limit_override,
                        COALESCE(updated_at, ?)
                    FROM antiabuse_cases_legacy
                    WHERE name IS NOT NULL AND TRIM(name) != ''
                    """,
                    (now_ts,),
                )

            await conn.execute("DROP TABLE antiabuse_cases_legacy")
        else:
            if "name" not in cols:
                await conn.execute("ALTER TABLE antiabuse_cases ADD COLUMN name TEXT")
            if "ip_limit_override" not in cols:
                await conn.execute("ALTER TABLE antiabuse_cases ADD COLUMN ip_limit_override INTEGER")
            if "updated_at" not in cols:
                await conn.execute("ALTER TABLE antiabuse_cases ADD COLUMN updated_at REAL")

        try:
            await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_antiabuse_cases_name ON antiabuse_cases(name)")
        except Exception:
            pass

        await conn.commit()


def antiabuse_log(message: str) -> None:
    logging.info("[antiabuse] %s", message)


def antiabuse_parse_ts(ts_text: str) -> float:
    try:
        if "T" in ts_text:
            return datetime.fromisoformat(ts_text).timestamp()
        return datetime.strptime(ts_text, "%Y/%m/%d %H:%M:%S").timestamp()
    except Exception:
        antiabuse_log(f"bad timestamp in log: {ts_text!r}")
        return 0.0


ANTIABUSE_TS_RE = r"(?P<ts>\d{4}-\d{2}-\d{2}T[0-9:.+\-]+|\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2})"
ANTIABUSE_FROM_RE = re.compile(
    rf"^{ANTIABUSE_TS_RE} .*?INFO - inbound/(?P<proto>vless|tuic|hysteria2)\[[^\]]+\]\s*inbound(?: packet)? connection from (?P<ip>[^: ]+):(?P<port>\d+)",
    re.IGNORECASE,
)
ANTIABUSE_NAME_RE = re.compile(
    rf"^{ANTIABUSE_TS_RE} .*?INFO - inbound/(?P<proto>vless|tuic|hysteria2)\[[^\]]+\]\s*\[(?P<name>[^\]]+)\]\s*inbound(?: packet)? connection to (?P<dst>.+)$",
    re.IGNORECASE,
)


def parse_antiabuse_event(line: str):
    m = ANTIABUSE_FROM_RE.search(line)
    if m:
        ts_text = m.group("ts")
        ts_epoch = antiabuse_parse_ts(ts_text)
        if ts_epoch <= 0:
            return None
        return {
            "ts_text": ts_text,
            "ts_epoch": ts_epoch,
            "protocol": m.group("proto").lower(),
            "event_type": "from",
            "ip": m.group("ip"),
            "port": int(m.group("port")),
            "name": None,
            "destination": None,
            "raw_line": line.strip(),
        }

    m = ANTIABUSE_NAME_RE.search(line)
    if m:
        ts_text = m.group("ts")
        ts_epoch = antiabuse_parse_ts(ts_text)
        if ts_epoch <= 0:
            return None
        return {
            "ts_text": ts_text,
            "ts_epoch": ts_epoch,
            "protocol": m.group("proto").lower(),
            "event_type": "name",
            "ip": None,
            "port": None,
            "name": m.group("name"),
            "destination": m.group("dst"),
            "raw_line": line.strip(),
        }
    return None


def prune_recent_events(protocol: str, now_ts: float) -> None:
    ttl = max(ANTIABUSE_MATCH_WINDOWS.values()) * 3
    for queue in (antiabuse_recent_from[protocol], antiabuse_recent_name[protocol]):
        while queue and (now_ts - queue[0]["ts_epoch"]) > ttl:
            queue.popleft()


async def antiabuse_insert_raw_event(event: dict) -> int:
    async with aiosqlite.connect(ANTIABUSE_DB) as conn:
        cur = await conn.execute(
            """
            INSERT INTO raw_events (
                ts_text, ts_epoch, protocol, event_type, ip, port, name, destination, raw_line, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event["ts_text"], event["ts_epoch"], event["protocol"], event["event_type"],
                event.get("ip"), event.get("port"), event.get("name"), event.get("destination"),
                event["raw_line"], datetime.now().timestamp(),
            ),
        )
        await conn.commit()
        return int(cur.lastrowid)


async def antiabuse_mark_matched(event_from_id: int, event_name_id: int) -> None:
    async with aiosqlite.connect(ANTIABUSE_DB) as conn:
        await conn.execute("UPDATE raw_events SET matched=1 WHERE id IN (?, ?)", (int(event_from_id), int(event_name_id)))
        await conn.commit()


async def antiabuse_save_match(from_event: dict, name_event: dict) -> None:
    now_ts = datetime.now().timestamp()
    async with aiosqlite.connect(ANTIABUSE_DB) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(
            """
            INSERT OR IGNORE INTO matches (
                ts_epoch, protocol, name, ip, port, event_from_id, event_name_id, confidence, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                max(float(from_event["ts_epoch"]), float(name_event["ts_epoch"])),
                str(from_event["protocol"]),
                str(name_event["name"]),
                str(from_event["ip"]),
                int(from_event.get("port") or 0),
                int(from_event["id"]),
                int(name_event["id"]),
                "unique_window",
                now_ts,
            ),
        )
        async with conn.execute("SELECT protocols FROM ip_state WHERE name=? AND ip=?", (str(name_event["name"]), str(from_event["ip"]))) as cur:
            existing = await cur.fetchone()
        proto_set = {str(from_event["protocol"])}
        if existing and existing["protocols"]:
            proto_set.update(x for x in str(existing["protocols"]).split(",") if x)
        if existing:
            await conn.execute(
                "UPDATE ip_state SET last_seen=?, hits=hits+1, protocols=? WHERE name=? AND ip=?",
                (now_ts, ",".join(sorted(proto_set)), str(name_event["name"]), str(from_event["ip"])),
            )
        else:
            await conn.execute(
                "INSERT INTO ip_state (name, ip, first_seen, last_seen, hits, protocols) VALUES (?, ?, ?, ?, ?, ?)",
                (str(name_event["name"]), str(from_event["ip"]), now_ts, now_ts, 1, ",".join(sorted(proto_set))),
            )
        await conn.commit()


async def antiabuse_try_match(new_event: dict) -> None:
    protocol = str(new_event["protocol"])
    now_ts = float(new_event["ts_epoch"])
    prune_recent_events(protocol, now_ts)
    window = float(ANTIABUSE_MATCH_WINDOWS.get(protocol, 1.0))
    if new_event["event_type"] == "from":
        antiabuse_recent_from[protocol].append(new_event)
        return

    candidates = [
        item for item in antiabuse_recent_from[protocol]
        if (not item.get("matched")) and 0 <= (now_ts - float(item["ts_epoch"])) <= window
    ]
    if not candidates:
        candidates = [
            item for item in antiabuse_recent_from[protocol]
            if (not item.get("matched")) and abs(now_ts - float(item["ts_epoch"])) <= window
        ]
    if not candidates:
        antiabuse_recent_name[protocol].append(new_event)
        return

    candidate = max(candidates, key=lambda item: float(item["ts_epoch"]))
    candidate["matched"] = True
    new_event["matched"] = True
    await antiabuse_mark_matched(int(candidate["id"]), int(new_event["id"]))
    await antiabuse_save_match(candidate, new_event)


async def antiabuse_cleanup_db() -> None:
    threshold = datetime.now().timestamp() - (ANTIABUSE_RETENTION_HOURS * 3600)
    async with aiosqlite.connect(ANTIABUSE_DB) as conn:
        await conn.execute("DELETE FROM raw_events WHERE ts_epoch < ?", (threshold,))
        await conn.execute("DELETE FROM matches WHERE ts_epoch < ?", (threshold,))
        await conn.execute("DELETE FROM ip_state WHERE last_seen < ?", (threshold,))
        await conn.execute("DELETE FROM alerts WHERE sent_at < ?", (threshold,))
        await conn.execute("DELETE FROM antiabuse_notifications WHERE sent_at < ?", (threshold,))
        await conn.commit()


async def antiabuse_get_case_by_name(name: str):
    async with aiosqlite.connect(ANTIABUSE_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT * FROM antiabuse_cases WHERE name=? LIMIT 1", (str(name),)) as cur:
            return await cur.fetchone()


async def antiabuse_get_case_by_id(case_id: int):
    async with aiosqlite.connect(ANTIABUSE_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT * FROM antiabuse_cases WHERE id=? LIMIT 1", (int(case_id),)) as cur:
            return await cur.fetchone()


async def antiabuse_ensure_case(name: str):
    now_ts = datetime.now().timestamp()
    async with aiosqlite.connect(ANTIABUSE_DB) as conn:
        await conn.execute(
            """
            INSERT INTO antiabuse_cases (name, admin_status, admin_note, ip_limit_override, updated_at)
            VALUES (?, 'new', '', NULL, ?)
            ON CONFLICT(name) DO NOTHING
            """,
            (str(name), now_ts),
        )
        await conn.commit()
    return await antiabuse_get_case_by_name(name)


async def antiabuse_set_status(name: str, status: str) -> None:
    await antiabuse_ensure_case(name)
    now_ts = datetime.now().timestamp()
    async with aiosqlite.connect(ANTIABUSE_DB) as conn:
        await conn.execute("UPDATE antiabuse_cases SET admin_status=?, updated_at=? WHERE name=?", (str(status), now_ts, str(name)))
        await conn.commit()


async def antiabuse_set_note(name: str, note: str) -> None:
    await antiabuse_ensure_case(name)
    now_ts = datetime.now().timestamp()
    async with aiosqlite.connect(ANTIABUSE_DB) as conn:
        await conn.execute("UPDATE antiabuse_cases SET admin_note=?, updated_at=? WHERE name=?", (str(note), now_ts, str(name)))
        await conn.commit()


async def antiabuse_set_limit_override(name: str, limit_value: int) -> None:
    await antiabuse_ensure_case(name)
    now_ts = datetime.now().timestamp()
    async with aiosqlite.connect(ANTIABUSE_DB) as conn:
        await conn.execute("UPDATE antiabuse_cases SET ip_limit_override=?, updated_at=? WHERE name=?", (int(limit_value), now_ts, str(name)))
        await conn.commit()


async def antiabuse_clear_limit_override(name: str) -> None:
    await antiabuse_ensure_case(name)
    now_ts = datetime.now().timestamp()
    async with aiosqlite.connect(ANTIABUSE_DB) as conn:
        await conn.execute("UPDATE antiabuse_cases SET ip_limit_override=NULL, updated_at=? WHERE name=?", (now_ts, str(name)))
        await conn.commit()


async def antiabuse_effective_limit_for_name(name: str) -> int:
    case = await antiabuse_get_case_by_name(name)
    if case and case["ip_limit_override"] is not None:
        try:
            return max(1, int(case["ip_limit_override"]))
        except Exception:
            return ANTIABUSE_IP_LIMIT
    client = await antiabuse_get_client_meta(name)
    if client:
        plan_key = parse_plan_key_from_desc(str(client.get("desc") or ""))
        tariff = get_tariff(plan_key) if plan_key else None
        if tariff:
            try:
                return max(1, int(tariff.connection_limit))
            except Exception:
                pass
    return ANTIABUSE_IP_LIMIT


async def antiabuse_get_client_meta(name: str):
    clients = await get_all_clients()
    for client in clients:
        if str(client.get("name") or "") == str(name):
            meta = dict(client)
            meta["server_name"] = SUI_SERVER_NAME
            return meta
    clients = await get_all_clients(force_refresh=True)
    for client in clients:
        if str(client.get("name") or "") == str(name):
            meta = dict(client)
            meta["server_name"] = SUI_SERVER_NAME
            return meta
    return None


async def antiabuse_recent_ips(name: str, since_ts: float, limit: int = 10) -> list[str]:
    async with aiosqlite.connect(ANTIABUSE_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT DISTINCT ip FROM matches WHERE name=? AND ts_epoch>=? ORDER BY ts_epoch DESC LIMIT ?",
            (str(name), float(since_ts), int(limit)),
        ) as cur:
            rows = await cur.fetchall()
            return [str(r["ip"]) for r in rows if r["ip"]]


async def antiabuse_notification_last_ts(name: str, kind: str) -> float:
    async with aiosqlite.connect(ANTIABUSE_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT sent_at FROM antiabuse_notifications WHERE name=? AND kind=? ORDER BY sent_at DESC LIMIT 1",
            (str(name), str(kind)),
        ) as cur:
            row = await cur.fetchone()
            return float(row["sent_at"] or 0) if row else 0.0


async def antiabuse_notification_count(name: str, kind: str) -> int:
    async with aiosqlite.connect(ANTIABUSE_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT COUNT(*) AS c FROM antiabuse_notifications WHERE name=? AND kind=?",
            (str(name), str(kind)),
        ) as cur:
            row = await cur.fetchone()
            return int(row["c"] or 0) if row else 0


async def antiabuse_record_notification(name: str, kind: str, *, user_notified: bool, admin_notified: bool, details: dict | None = None) -> None:
    async with aiosqlite.connect(ANTIABUSE_DB) as conn:
        await conn.execute(
            "INSERT INTO antiabuse_notifications (name, kind, sent_at, user_notified, admin_notified, details) VALUES (?, ?, ?, ?, ?, ?)",
            (
                str(name),
                str(kind),
                datetime.now().timestamp(),
                1 if user_notified else 0,
                1 if admin_notified else 0,
                json.dumps(details or {}, ensure_ascii=False),
            ),
        )
        await conn.commit()


def antiabuse_warning_text(level: int) -> str:
    if int(level) <= 1:
        return "⚠️ По вашей подписке превышено количество разрешенных одновременных подключений."
    return "⚠️ По вашей подписке превышено количество разрешенных одновременных подключений, за шаринг подписки доступ может быть приостановлен."


async def antiabuse_maybe_send_user_warning(bot: Bot, name: str, unique_ip_count: int) -> None:
    if not ANTIABUSE_NOTIFY_USER:
        return

    now_ts = datetime.now().timestamp()
    last_warn2 = await antiabuse_notification_last_ts(name, "warn2")
    last_warn1 = await antiabuse_notification_last_ts(name, "warn1")

    level = 0
    if last_warn2 > 0:
        return
    if last_warn1 <= 0:
        if now_ts - last_warn1 >= ANTIABUSE_WARN1_COOLDOWN_MINUTES * 60:
            level = 1
    elif now_ts - last_warn1 >= ANTIABUSE_WARN2_COOLDOWN_MINUTES * 60:
        level = 2

    if level <= 0:
        return

    client = await antiabuse_get_client_meta(name)
    desc = str((client or {}).get("desc") or "")
    tgid = parse_tgid_from_desc(desc)
    if not tgid:
        return

    user_notified = False
    try:
        await bot.send_message(int(tgid), antiabuse_warning_text(level), reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📚 Почему это произошло?", callback_data="faq:restricted")]]))
        user_notified = True
    except Exception as exc:
        logging.warning("antiabuse warning failed for %s: %s", tgid, exc)

    if user_notified:
        await antiabuse_record_notification(
            name,
            f"warn{level}",
            user_notified=True,
            admin_notified=False,
            details={"unique_ip_count": int(unique_ip_count)},
        )



async def antiabuse_send_user_state_message(bot: Bot, name: str, kind: str, minutes: int | None = None) -> bool:
    client = await antiabuse_get_client_meta(name)
    desc = str((client or {}).get("desc") or "")
    tgid = parse_tgid_from_desc(desc)
    if not tgid:
        return False

    if kind == "disabled":
        if minutes is None:
            row = await antiabuse_get_active_enforcement(name)
            if row:
                try:
                    minutes = max(1, int((float(row["disabled_until"]) - datetime.now().timestamp()) / 60))
                except Exception:
                    minutes = ANTIABUSE_DISABLE_DEFAULT_MINUTES
            else:
                minutes = ANTIABUSE_DISABLE_DEFAULT_MINUTES
        text = (
            "⛔ Доступ к вашей VPN-подписке временно приостановлен.\n\n"
            f"Срок ограничения: {int(minutes)} мин."
        )
        kind_key = "disabled"
    elif kind == "enabled":
        text = "✅ Доступ к вашей VPN-подписке восстановлен."
        kind_key = "enabled"
    else:
        return False

    try:
        btn_cb = "faq:restricted" if kind == "disabled" else "faq:connections"
        await bot.send_message(int(tgid), text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📚 Почему это произошло?", callback_data=btn_cb)]]))
        await antiabuse_record_notification(
            name,
            kind_key,
            user_notified=True,
            admin_notified=False,
            details={"minutes": int(minutes or 0), "tgid": int(tgid)},
        )
        return True
    except Exception as exc:
        logging.warning("antiabuse %s notify failed for %s: %s", kind_key, tgid, exc)
        return False


def build_edit_client_payload(client: dict, *, enable: bool) -> dict:
    client_id = client.get("id")
    name = str(client.get("name") or "")
    if not client_id:
        raise RuntimeError("У клиента нет id для edit")
    if not name:
        raise RuntimeError("У клиента пустой name для edit")

    payload = {
        "enable": bool(enable),
        "name": name,
        "config": client.get("config") or {},
        "inbounds": normalize_inbound_ids(client.get("inbounds")),
        "links": client.get("links") or [],
        "volume": int(client.get("volume") or 0),
        "expiry": int(client.get("expiry") or 0),
        "up": int(client.get("up") or 0),
        "down": int(client.get("down") or 0),
        "desc": str(client.get("desc") or ""),
        "group": str(client.get("group") or "User"),
        "delayStart": bool(client.get("delayStart", False)),
        "autoReset": bool(client.get("autoReset", False)),
        "resetDays": int(client.get("resetDays") or 0),
        "nextReset": int(client.get("nextReset") or 0),
        "totalUp": int(client.get("totalUp") or 0),
        "totalDown": int(client.get("totalDown") or 0),
        "id": int(client_id),
    }
    tgid_raw = client.get("tgId")
    if tgid_raw not in (None, ""):
        payload["tgId"] = str(tgid_raw)
    return payload


async def antiabuse_post_client_editbulk(updated: dict) -> None:
    payload = {
        "object": "clients",
        "action": "editbulk",
        "data": json.dumps([updated], ensure_ascii=False, separators=(",", ":")),
    }
    headers = {"Token": SUI_TOKEN, "Content-Type": "application/x-www-form-urlencoded"}
    session = await get_http_session()
    async with session.post(f"{SUI_API_URL}/save", data=urlencode(payload), headers=headers) as resp:
        raw_text = await resp.text()
        if resp.status != 200:
            raise RuntimeError(f"S-UI HTTP {resp.status}: {raw_text[:500]}")
        result = json.loads(raw_text)
        if not result.get("success"):
            raise RuntimeError(f"S-UI editbulk error: {raw_text[:1200]}")
    global sui_cache_ts
    sui_cache_ts = 0.0


async def antiabuse_set_client_enabled(name: str, enable: bool) -> None:
    client = await antiabuse_get_client_meta(name)
    if not client:
        raise RuntimeError("Клиент в S-UI не найден")
    updated = build_editbulk_client_payload(
        client=client,
        new_expiry=int(client.get("expiry") or 0),
        new_volume=int(client.get("volume") or 0),
    )
    updated["enable"] = bool(enable)
    await antiabuse_post_client_editbulk(updated)


async def antiabuse_get_active_enforcement(name: str):
    async with aiosqlite.connect(ANTIABUSE_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM antiabuse_enforcement WHERE name=? AND is_active=1 ORDER BY id DESC LIMIT 1",
            (str(name),),
        ) as cur:
            return await cur.fetchone()


async def antiabuse_disable_for_minutes(name: str, minutes: int, admin_id: int | None, reason: str) -> None:
    minutes = max(1, min(int(minutes), ANTIABUSE_DISABLE_MAX_MINUTES))
    await antiabuse_set_client_enabled(name, False)
    now_ts = datetime.now().timestamp()
    disabled_until = now_ts + minutes * 60
    details = {
        "minutes": minutes,
        "disabled_via": "editbulk",
    }
    async with aiosqlite.connect(ANTIABUSE_DB) as conn:
        await conn.execute(
            "UPDATE antiabuse_enforcement SET is_active=0, enabled_at=?, enabled_by=?, details=? WHERE name=? AND is_active=1",
            (now_ts, int(admin_id or 0), json.dumps({"closed_by": "new_disable"}, ensure_ascii=False), str(name)),
        )
        await conn.execute(
            "INSERT INTO antiabuse_enforcement (name, is_active, disabled_until, disabled_at, disabled_by, reason, details) VALUES (?, 1, ?, ?, ?, ?, ?)",
            (
                str(name),
                float(disabled_until),
                float(now_ts),
                int(admin_id or 0),
                str(reason or "antiabuse manual disable"),
                json.dumps(details, ensure_ascii=False),
            ),
        )
        await conn.commit()


async def antiabuse_enable_now(name: str, admin_id: int | None, reason: str) -> None:
    await antiabuse_set_client_enabled(name, True)
    now_ts = datetime.now().timestamp()
    async with aiosqlite.connect(ANTIABUSE_DB) as conn:
        await conn.execute(
            "UPDATE antiabuse_enforcement SET is_active=0, enabled_at=?, enabled_by=?, reason=? WHERE name=? AND is_active=1",
            (now_ts, int(admin_id or 0), str(reason or "manual enable"), str(name)),
        )
        await conn.commit()


async def antiabuse_process_enforcement(bot: Bot) -> None:
    now_ts = datetime.now().timestamp()
    async with aiosqlite.connect(ANTIABUSE_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM antiabuse_enforcement WHERE is_active=1 AND disabled_until<=? ORDER BY id ASC",
            (float(now_ts),),
        ) as cur:
            rows = await cur.fetchall()

    for row in rows:
        name = str(row["name"] or "")
        if not name:
            continue
        try:
            await antiabuse_enable_now(name, None, "auto restore")
            await antiabuse_send_user_state_message(bot, name, "enabled")
        except Exception as exc:
            logging.warning("antiabuse auto-enable failed for %s: %s", name, exc)


async def antiabuse_build_cases(force_refresh: bool = False) -> list[dict]:
    now_ts = datetime.now().timestamp()
    window_start = now_ts - (ANTIABUSE_WINDOW_MINUTES * 60)
    hour_start = now_ts - 3600
    day_start = now_ts - 86400

    clients = await get_all_clients(force_refresh=force_refresh)
    meta_by_name = {}
    for client in clients:
        name = str(client.get("name") or "").strip()
        if not name:
            continue
        meta = dict(client)
        meta["server_name"] = SUI_SERVER_NAME
        meta_by_name[name] = meta

    async with aiosqlite.connect(ANTIABUSE_DB) as conn:
        conn.row_factory = aiosqlite.Row

        names = set(meta_by_name.keys())

        cases = []
        for name in sorted(names):
            case_row = await antiabuse_ensure_case(name)

            async with conn.execute("SELECT COUNT(DISTINCT ip) AS c FROM matches WHERE name=? AND ts_epoch>=?", (name, window_start)) as cur:
                row = await cur.fetchone()
                unique_window = int(row["c"] or 0) if row else 0
            async with conn.execute("SELECT COUNT(DISTINCT ip) AS c FROM matches WHERE name=? AND ts_epoch>=?", (name, hour_start)) as cur:
                row = await cur.fetchone()
                unique_hour = int(row["c"] or 0) if row else 0
            async with conn.execute("SELECT COUNT(DISTINCT ip) AS c FROM matches WHERE name=? AND ts_epoch>=?", (name, day_start)) as cur:
                row = await cur.fetchone()
                unique_day = int(row["c"] or 0) if row else 0
            async with conn.execute("SELECT COUNT(*) AS c, MAX(last_seen) AS last_seen FROM ip_state WHERE name=?", (name,)) as cur:
                row = await cur.fetchone()
                unique_total = int(row["c"] or 0) if row else 0
                last_seen_ts = float(row["last_seen"] or 0) if row else 0.0

            ips = await antiabuse_recent_ips(name, window_start, 10)
            limit_value = max(1, int(case_row["ip_limit_override"] or ANTIABUSE_IP_LIMIT))
            suspicious = unique_window >= limit_value

            meta = meta_by_name.get(name)
            desc = str((meta or {}).get("desc") or "")
            tgid = parse_tgid_from_desc(desc)
            username = parse_username_from_desc(desc)
            plan_key = parse_plan_key_from_desc(desc)
            server_name = str((meta or {}).get("server_name") or SUI_SERVER_NAME)

            reasons = []
            if unique_window >= limit_value:
                reasons.append(f"за {ANTIABUSE_WINDOW_MINUTES} мин: {unique_window} IP при лимите {limit_value}")
            if unique_hour >= (limit_value * 2):
                reasons.append(f"за 1 час: {unique_hour} уникальных IP")
            if unique_day >= (limit_value * 3):
                reasons.append(f"за 24 часа: {unique_day} уникальных IP")

            warn1_count = await antiabuse_notification_count(name, "warn1")
            warn2_count = await antiabuse_notification_count(name, "warn2")
            enforcement = await antiabuse_get_active_enforcement(name)

            cases.append(
                {
                    "case_id": int(case_row["id"]),
                    "name": name,
                    "tgid": tgid,
                    "username": username,
                    "plan_key": plan_key,
                    "server_name": server_name,
                    "unique_window": unique_window,
                    "unique_hour": unique_hour,
                    "unique_day": unique_day,
                    "unique_total": unique_total,
                    "limit_value": limit_value,
                    "has_override": case_row["ip_limit_override"] is not None,
                    "recent_ips": ips,
                    "last_seen_text": payment_dt_text(last_seen_ts),
                    "admin_status": str(case_row["admin_status"] or "new"),
                    "admin_note": str(case_row["admin_note"] or ""),
                    "suspicious": suspicious,
                    "reasons": reasons,
                    "risk_score": unique_window * 10 + unique_hour * 3 + min(unique_day, 50),
                    "warn1_count": warn1_count,
                    "warn2_count": warn2_count,
                    "disabled_active": bool(enforcement),
                    "disabled_until_text": payment_dt_text(float(enforcement["disabled_until"] or 0)) if enforcement else "—",
                }
            )

    cases.sort(
        key=lambda x: (
            0 if x["admin_status"] == "check" else 1,
            0 if x["suspicious"] else 1,
            -x["risk_score"],
            -x["unique_window"],
            -x["unique_day"],
        )
    )
    return cases


async def antiabuse_send_alert(bot: Bot, name: str, unique_ip_count: int, ips: list[str], first_seen: float, last_seen: float) -> None:
    now_ts = datetime.now().timestamp()
    async with aiosqlite.connect(ANTIABUSE_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT sent_at FROM alerts WHERE name=? ORDER BY sent_at DESC LIMIT 1", (str(name),)) as cur:
            recent = await cur.fetchone()
        if recent and (now_ts - float(recent["sent_at"])) < (ANTIABUSE_ALERT_COOLDOWN_MINUTES * 60):
            return

        client = await antiabuse_get_client_meta(name)
        desc = str((client or {}).get("desc") or "")
        tgid = parse_tgid_from_desc(desc)
        username = parse_username_from_desc(desc)
        plan_key = parse_plan_key_from_desc(desc)
        server_name = str((client or {}).get("server_name") or SUI_SERVER_NAME)
        effective_limit = await antiabuse_effective_limit_for_name(name)

        one_hour_start = now_ts - 3600
        day_start = now_ts - 86400
        async with conn.execute("SELECT COUNT(DISTINCT ip) AS c FROM matches WHERE name=? AND ts_epoch>=?", (name, one_hour_start)) as cur:
            row = await cur.fetchone()
            hour_count = int(row["c"] or 0) if row else 0
        async with conn.execute("SELECT COUNT(DISTINCT ip) AS c FROM matches WHERE name=? AND ts_epoch>=?", (name, day_start)) as cur:
            row = await cur.fetchone()
            day_count = int(row["c"] or 0) if row else 0

        ip_lines = "\n".join(f"• {ip}" for ip in ips[:10]) or "• нет данных"
        admin_text = (
            "⚠️ Подозрение на шаринг подписки\n\n"
            f"Name: {name}\n"
            f"Сервер: {server_name}\n"
            f"Тариф: {plan_key or '—'}\n"
            f"TGID: {tgid or '—'}\n"
            f"User: {username or '—'}\n\n"
            f"Уникальных IP:\n"
            f"• {ANTIABUSE_WINDOW_MINUTES} мин: {unique_ip_count}\n"
            f"• 1 час: {hour_count}\n"
            f"• 24 часа: {day_count}\n"
            f"• Лимит: {effective_limit}\n\n"
            f"Последние IP:\n{ip_lines}"
        )
        admin_notified = 0
        case_row = await antiabuse_ensure_case(name)
        case_id = int(case_row["id"])
        alert_kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🛡 Открыть кейс", callback_data=f"antiabuse:view:{case_id}:suspicious")]]
        )
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(int(admin_id), admin_text, reply_markup=alert_kb)
                admin_notified = 1
            except Exception as exc:
                logging.warning("antiabuse admin notify failed for %s: %s", admin_id, exc)

        user_notified = 0
        await antiabuse_maybe_send_user_warning(bot, name, unique_ip_count)

        await conn.execute(
            "INSERT INTO alerts (name, unique_ip_count, window_minutes, first_seen, last_seen, sent_at, user_notified, admin_notified, details) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(name),
                int(unique_ip_count),
                ANTIABUSE_WINDOW_MINUTES,
                float(first_seen),
                float(last_seen),
                now_ts,
                int(user_notified),
                int(admin_notified),
                json.dumps({"ips": ips, "hour_count": hour_count, "day_count": day_count}, ensure_ascii=False),
            ),
        )
        await conn.commit()


async def antiabuse_check_alerts(bot: Bot) -> None:
    if not ANTIABUSE_ENABLED:
        return
    window_start = datetime.now().timestamp() - (ANTIABUSE_WINDOW_MINUTES * 60)
    async with aiosqlite.connect(ANTIABUSE_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """
            SELECT name, MIN(ts_epoch) AS first_seen, MAX(ts_epoch) AS last_seen, COUNT(DISTINCT ip) AS unique_ip_count
            FROM matches
            WHERE ts_epoch >= ?
            GROUP BY name
            """,
            (window_start,),
        ) as cur:
            rows = await cur.fetchall()
        pending = []
        for row in rows:
            name = str(row["name"] or "")
            unique_ip_count = int(row["unique_ip_count"] or 0)
            effective_limit = await antiabuse_effective_limit_for_name(name)
            if unique_ip_count < effective_limit:
                continue
            async with conn.execute("SELECT DISTINCT ip FROM matches WHERE name=? AND ts_epoch>=? ORDER BY ts_epoch DESC LIMIT 20", (name, window_start)) as cur:
                ip_rows = await cur.fetchall()
            ips = [str(r["ip"]) for r in ip_rows if r["ip"]]
            pending.append((name, unique_ip_count, ips, float(row["first_seen"]), float(row["last_seen"])))
    for item in pending:
        await antiabuse_send_alert(bot, *item)


async def antiabuse_scan_once() -> None:
    global antiabuse_last_scan_ts
    if not ANTIABUSE_ENABLED:
        return
    await init_antiabuse_db()
    now_ts = datetime.now().timestamp()
    if antiabuse_last_scan_ts is None:
        antiabuse_last_scan_ts = now_ts - ANTIABUSE_INITIAL_LOOKBACK_SECONDS

    since_ts = antiabuse_last_scan_ts - 2
    since_str = datetime.fromtimestamp(max(since_ts, 0)).strftime("%Y-%m-%d %H:%M:%S")

    proc = await asyncio.create_subprocess_exec(
        "journalctl", "-u", ANTIABUSE_JOURNAL_UNIT, "-o", "short-iso-precise",
        "--since", since_str, "--no-pager",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        antiabuse_log(f"journalctl scan failed rc={proc.returncode} stderr={stderr.decode('utf-8', 'ignore')[:500]}")
        antiabuse_last_scan_ts = now_ts
        return

    lines = stdout.decode("utf-8", errors="ignore").splitlines()
    inserted = 0
    async with antiabuse_lock:
        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            event = parse_antiabuse_event(line)
            if not event:
                continue
            signature = (
                event["ts_text"], event["protocol"], event["event_type"],
                event.get("ip"), event.get("port"), event.get("name"), event.get("destination"),
            )
            if signature in antiabuse_seen_signatures:
                continue
            antiabuse_seen_signatures.add(signature)
            if len(antiabuse_seen_signatures) > 50000:
                antiabuse_seen_signatures.clear()

            event["id"] = await antiabuse_insert_raw_event(event)
            inserted += 1
            await antiabuse_try_match(event)
    antiabuse_last_scan_ts = now_ts
    antiabuse_log(f"scan complete lines={len(lines)} inserted={inserted}")


async def antiabuse_worker(bot: Bot) -> None:
    if not ANTIABUSE_ENABLED:
        logging.info("antiabuse worker disabled")
        return
    await init_antiabuse_db()
    await antiabuse_cleanup_db()
    logging.info("antiabuse worker started")
    while True:
        try:
            await antiabuse_scan_once()
            await antiabuse_check_alerts(bot)
            await antiabuse_process_enforcement(bot)
            await antiabuse_cleanup_db()
        except Exception as exc:
            logging.exception("antiabuse worker failed: %s", exc)
        await asyncio.sleep(ANTIABUSE_POLL_SECONDS)
# =========================
# S-UI
# =========================

def random_suffix(length: int = 4) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def generate_secret_password(length: int = 10) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def build_desc(user_id: int, username: str | None, tarif_key: str, server_code: str, is_test: bool = False) -> str:
    desc = f"TGID={int(user_id)}"
    if username:
        desc += f" @{username}"
    desc += f" PLAN={tarif_key}_{server_code}"
    if is_test:
        desc += " TEST"
    return desc


def parse_tgid_from_desc(desc: str) -> int | None:
    marker = "TGID="
    if marker not in (desc or ""):
        return None
    try:
        raw = (desc.split(marker, 1)[1]).split()[0]
        return int(raw)
    except Exception:
        return None


def parse_username_from_desc(desc: str) -> str:
    m = re.search(r"TGID=\d+\s+(@[A-Za-z0-9_]+)", desc or "")
    return str(m.group(1) if m else "")


def parse_plan_key_from_desc(desc: str) -> str:
    marker = "PLAN="
    if marker not in (desc or ""):
        return ""
    raw = (desc.split(marker, 1)[1]).split()[0].strip()
    if not raw:
        return ""

    # Новый формат: PLAN=<tariff_key>_<server_code>
    # Но для старых/переходных подписок нужно не терять исходный ключ,
    # если обрезанный вариант не соответствует реальному тарифу.
    suffix = f"_{SUI_SERVER_CODE}"
    if raw.endswith(suffix):
        candidate = raw[: -len(suffix)]
        if candidate:
            try:
                if get_tariff_base(candidate) is not None or get_tariff_override(candidate) is not None:
                    return candidate
            except Exception:
                return candidate

    try:
        if get_tariff_base(raw) is not None or get_tariff_override(raw) is not None:
            return raw
    except Exception:
        pass

    # Fallback для старых подписок, где серверный код был последним сегментом
    if "_" in raw:
        return raw.rsplit("_", 1)[0]
    return raw


def make_client_name(server_code: str, tariff_code: str) -> str:
    return f"YouNameBot-{server_code}-{tariff_code}-{random_suffix()}"


def subscription_links(client_name: str) -> tuple[str, str]:
    main_link = f"{SUI_SUB_URL}{quote(client_name, safe='')}"
    clash_link = f"{main_link}?format=clash"
    return main_link, clash_link


def bytes_from_gb(gb: int | None) -> int:
    return 0 if gb is None else int(gb) * 1024 * 1024 * 1024


def extract_clients(data: dict) -> list[dict]:
    if not isinstance(data, dict):
        return []
    obj = data.get("obj")
    if isinstance(obj, dict):
        clients = obj.get("clients")
        return clients if isinstance(clients, list) else []
    if isinstance(obj, list):
        return obj
    clients = data.get("clients")
    return clients if isinstance(clients, list) else []


def compute_subscription_stats(client: dict) -> tuple[int, str]:
    volume = int(client.get("volume") or 0)
    up = int(client.get("up") or 0)
    down = int(client.get("down") or 0)
    used = up + down
    expiry = int(client.get("expiry") or 0)
    now_ts = int(datetime.now().timestamp())
    days_left = max(0, int((expiry - now_ts) / 86400)) if expiry > 0 else 0

    if volume == 0:
        traffic_text = f"{round(used / (1024**3), 2)} ГБ / ∞"
    else:
        used_gb = round(used / (1024**3), 2)
        volume_gb = round(volume / (1024**3), 2)
        traffic_text = f"{used_gb} / {volume_gb} ГБ"
    return days_left, traffic_text


def format_expiry(client: dict) -> str:
    expiry = int(client.get("expiry") or 0)
    if expiry <= 0:
        return "—"
    return datetime.fromtimestamp(expiry).strftime("%d.%m.%Y")


async def create_client(*, user_id: int, username: str | None, tariff: Tariff, is_test: bool = False) -> tuple[str, str, str]:
    expiry_sec = int((datetime.now() + timedelta(days=tariff.days)).timestamp())
    volume_bytes = bytes_from_gb(tariff.traffic_gb)

    client_name = make_client_name(SUI_SERVER_CODE, tariff.code)
    client_uuid = str(uuid.uuid4())
    secret_password = generate_secret_password(10)
    desc = build_desc(user_id=user_id, username=username, tarif_key=tariff.key, server_code=SUI_SERVER_CODE, is_test=is_test)

    data_obj = {
        "enable": True,
        "name": client_name,
        "tgId": str(user_id),
        "config": {
            "mixed": {"username": client_name, "password": secret_password},
            "socks": {"username": client_name, "password": secret_password},
            "http": {"username": client_name, "password": secret_password},
            "shadowsocks": {"name": client_name, "password": secret_password},
            "shadowsocks16": {"name": client_name, "password": secret_password},
            "shadowtls": {"name": client_name, "password": secret_password},
            "vmess": {"name": client_name, "uuid": client_uuid, "alterId": 0},
            "vless": {"name": client_name, "uuid": client_uuid, "flow": "xtls-rprx-vision"},
            "anytls": {"name": client_name, "password": secret_password},
            "trojan": {"name": client_name, "password": secret_password},
            "naive": {"username": client_name, "password": secret_password},
            "hysteria": {"name": client_name, "auth_str": secret_password},
            "tuic": {"name": client_name, "uuid": client_uuid, "password": secret_password},
            "hysteria2": {"name": client_name, "password": secret_password},
        },
        "inbounds": get_default_inbound_ids(),
        "links": [],
        "volume": volume_bytes,
        "expiry": expiry_sec,
        "up": 0,
        "down": 0,
        "desc": desc,
        "group": "User",
        "delayStart": False,
        "autoReset": False,
        "resetDays": 0,
        "nextReset": 0,
        "totalUp": 0,
        "totalDown": 0,
    }

    payload = {
        "object": "clients",
        "action": "new",
        "data": json.dumps(data_obj, ensure_ascii=False),
    }
    headers = {"Token": SUI_TOKEN, "Content-Type": "application/x-www-form-urlencoded"}

    session = await get_http_session()
    async with session.post(f"{SUI_API_URL}/save", data=urlencode(payload), headers=headers) as resp:
            raw_text = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"S-UI HTTP {resp.status}: {raw_text[:500]}")
            result = json.loads(raw_text)
            if not result.get("success"):
                raise RuntimeError(f"S-UI error: {raw_text[:700]}")

    sub_link, clash_link = subscription_links(client_name)
    return sub_link, clash_link, client_name


async def get_all_clients(force_refresh: bool = False) -> list[dict]:
    global sui_cache_data, sui_cache_ts

    now_ts = datetime.now().timestamp()

    async with sui_cache_lock:
        if not force_refresh and sui_cache_data is not None and (now_ts - sui_cache_ts) < 30.0:
            return list(sui_cache_data)

        headers = {"Token": SUI_TOKEN}
        session = await get_http_session()
        async with session.get(f"{SUI_API_URL}/clients", headers=headers) as resp:
            raw_text = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"S-UI HTTP {resp.status}: {raw_text[:500]}")
            data = json.loads(raw_text)

        sui_cache_data = list(extract_clients(data))
        sui_cache_ts = datetime.now().timestamp()
        return list(sui_cache_data)


async def get_client_by_id(client_id: int) -> dict | None:
    clients = await get_all_clients()
    for client in clients:
        try:
            if int(client.get("id") or 0) == int(client_id):
                return client
        except Exception:
            continue

    clients = await get_all_clients(force_refresh=True)
    for client in clients:
        try:
            if int(client.get("id") or 0) == int(client_id):
                return client
        except Exception:
            continue
    return None


async def get_user_subscriptions(user_id: int) -> list[dict]:
    clients = await get_all_clients()
    result: list[dict] = []

    for client in clients:
        desc = str(client.get("desc") or "")
        tgid = parse_tgid_from_desc(desc)
        tg_id_raw = str(client.get("tgId") or "")
        tgid_match = False
        if tgid is not None and int(tgid) == int(user_id):
            tgid_match = True
        elif tg_id_raw.isdigit() and int(tg_id_raw) == int(user_id):
            tgid_match = True

        if not tgid_match:
            continue

        name = str(client.get("name") or "unknown")
        sub_link, clash_link = subscription_links(name)
        plan_key = parse_plan_key_from_desc(desc)
        days_left, traffic_text = compute_subscription_stats(client)

        unique_connections = await antiabuse_unique_ip_count_for_name(name)
        connection_limit = await antiabuse_effective_limit_for_name(name)
        result.append(
            {
                "id": int(client.get("id") or 0),
                "name": name,
                "desc": desc,
                "plan_key": plan_key,
                "sub_link": sub_link,
                "clash_link": clash_link,
                "days_left": days_left,
                "traffic_text": traffic_text,
                "expiry_text": format_expiry(client),
                "unique_connections": unique_connections,
                "connection_limit": connection_limit,
            }
        )

    result.sort(key=lambda x: (x["days_left"], x["id"]), reverse=True)
    return result


def format_subscription_card(item: dict) -> str:
    plan_key = str(item.get("plan_key") or "")
    plan_label = plan_key if plan_key else "без плана"

    return (
        "________________\n"
        f"{SUI_SERVER_NAME} · {plan_label}\n"
        f"Name: {item.get('name', 'unknown')}\n"
        f"Осталось дней: {item.get('days_left', 0)}\n"
        f"Истекает: {item.get('expiry_text', '—')}\n"
        f"Трафик: {item.get('traffic_text', '—')}\n"
        f"Подключения: {item.get('unique_connections', 0)}/{item.get('connection_limit', 10)}\n\n"
        "Подписка:\n"
        f"{item.get('sub_link', '—')}\n\n"
        "Clash:\n"
        f"{item.get('clash_link', '—')}"
    )



def get_default_inbound_ids() -> list[int]:
    raw = str(SUI_DEFAULT_INBOUNDS or "1,2,3").strip()
    result: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except Exception:
            continue
        if value > 0 and value not in result:
            result.append(value)
    return result or [1, 2, 3]


def normalize_inbound_ids(raw_value) -> list[int]:
    result: list[int] = []
    if isinstance(raw_value, list):
        for item in raw_value:
            try:
                value = int(item)
            except Exception:
                continue
            if value > 0 and value not in result:
                result.append(value)
    return result or [1, 2, 3]


def build_editbulk_client_payload(client: dict, new_expiry: int, new_volume: int) -> dict:
    client_id = client.get("id")
    name = str(client.get("name") or "")
    if not client_id:
        raise RuntimeError("У клиента нет id для editbulk")
    if not name:
        raise RuntimeError("У клиента пустой name для editbulk")

    return {
        "id": int(client_id),
        "enable": bool(client.get("enable", True)),
        "name": name,
        "inbounds": normalize_inbound_ids(client.get("inbounds")),
        "volume": int(new_volume),
        "expiry": int(new_expiry),
        "down": int(client.get("down") or 0),
        "up": int(client.get("up") or 0),
        "desc": str(client.get("desc") or ""),
        "group": str(client.get("group") or "User"),
        "delayStart": bool(client.get("delayStart", False)),
        "autoReset": bool(client.get("autoReset", False)),
        "resetDays": int(client.get("resetDays") or 0),
        "nextReset": int(client.get("nextReset") or 0),
        "totalUp": int(client.get("totalUp") or 0),
        "totalDown": int(client.get("totalDown") or 0),
    }


async def renew_client_in_sui(client: dict, tariff: Tariff) -> dict:
    current_expiry = int(client.get("expiry") or 0)
    now_ts = int(datetime.now().timestamp())
    base_expiry = current_expiry if current_expiry > now_ts else now_ts
    new_expiry = base_expiry + int(tariff.days) * 86400

    current_volume = int(client.get("volume") or 0)
    if tariff.traffic_gb is None:
        new_volume = 0
    else:
        add_bytes = bytes_from_gb(tariff.traffic_gb)
        new_volume = add_bytes if current_volume == 0 else current_volume + add_bytes

    updated = build_editbulk_client_payload(client=client, new_expiry=new_expiry, new_volume=new_volume)

    payload = {
        "object": "clients",
        "action": "editbulk",
        "data": json.dumps([updated], ensure_ascii=False, separators=(",", ":")),
    }
    headers = {"Token": SUI_TOKEN, "Content-Type": "application/x-www-form-urlencoded"}

    session = await get_http_session()
    async with session.post(f"{SUI_API_URL}/save", data=urlencode(payload), headers=headers) as resp:
            raw_text = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"S-UI HTTP {resp.status}: {raw_text[:500]}")
            result = json.loads(raw_text)
            if not result.get("success"):
                raise RuntimeError(f"S-UI editbulk error: {raw_text[:1200]}")
    return updated


# =========================
# TEXTS
# =========================

def welcome_text() -> str:
    return (
        "👋 TG бот для S-UI\n\n"
        "Одна стабильная сборка:\n"
        "• Покупка через Telegram Stars\n"
        "• Автовыдача подписки после успешной оплаты\n"
        "• Синхронизация подписок с S-UI\n"
        "• Продление подписок\n"
        "• Раздел платежей\n"
        "• Профиль пользователя\n"
        "• FAQ\n"
        "• Поддержка / тикеты\n\n"
        f"Сервер: {SUI_SERVER_NAME}"
    )


def menu_text() -> str:
    return "Выберите действие:"


def tariffs_text() -> str:
    return "🛒 Покупка подписки\n\nВыберите тариф ниже."


def tariff_preview_text(tariff: Tariff) -> str:
    return (
        "💳 Предпросмотр покупки\n\n"
        f"Тариф: {tariff.title}\n"
        f"Срок: {tariff.days} дн.\n"
        f"Трафик: {human_traffic(tariff.traffic_gb)}\n"
        f"Стоимость: {tariff.stars_price} ⭐\n\n"
        "После успешной оплаты бот автоматически создаст подписку и сразу пришлёт ссылки.\n\n"
        "Если у вас недостаточно Telegram Stars на балансе, сначала купите их в официальном @PremiumBot, выбрав в Menu «Купить звезды Telegram».\n\n"
        "Также Telegram Stars можно купить через сторонние боты и сервисы.\n\n"
        "Оплата Telegram Stars позволяет вам анонимизировать вашу покупку."
    )


def renew_preview_text(item: dict, tariff: Tariff) -> str:
    return (
        "🔄 Предпросмотр продления\n\n"
        f"Подписка: {item['name']}\n"
        f"Текущий план: {item['plan_key']}\n"
        f"Сейчас осталось: {item['days_left']} дн.\n"
        f"Текущий трафик: {item['traffic_text']}\n\n"
        "Будет добавлено:\n"
        f"• Срок: +{tariff.days} дн.\n"
        f"• Трафик: +{human_traffic(tariff.traffic_gb)}\n\n"
        f"Стоимость: {tariff.stars_price} ⭐"
    )


def created_key_text(tariff: Tariff, sub_link: str, clash_link: str) -> str:
    return (
        "✅ Подписка выдана\n\n"
        f"Тариф: {tariff.title}\n"
        f"Срок: {tariff.days} дн.\n"
        f"Трафик: {human_traffic(tariff.traffic_gb)}\n\n"
        "Подписка:\n"
        f"{sub_link}\n\n"
        "Clash:\n"
        f"{clash_link}"
    )


def renew_success_text(item: dict) -> str:
    return (
        "✅ Продление выполнено\n\n"
        f"Подписка: {item['name']}\n"
        "Ссылка подписки осталась прежней.\n\n"
        "Подписка:\n"
        f"{item['sub_link']}\n\n"
        "Clash:\n"
        f"{item['clash_link']}"
    )


def payment_created_text(payment_id: int, tariff: Tariff, kind: str) -> str:
    return (
        "⭐ Счёт создан\n\n"
        f"Payment ID: #{payment_id}\n"
        f"Тип: {payment_kind_label(kind)}\n"
        f"Тариф: {tariff.title}\n"
        f"Сумма: {tariff.stars_price} ⭐"
    )


def no_subscriptions_text() -> str:
    return "🔑 Мои подписки\n\nПодписок пока нет."


def subscriptions_header_text(count: int) -> str:
    line = "━━━━━━━━━━━━━━"
    return (
        "🔑 Мои подписки\n"
        f"{line}\n"
        f"Найдено подписок: {count}\n"
        f"Сервер: {SUI_SERVER_NAME}\n"
        f"{line}"
    )


def no_renewable_subscriptions_text() -> str:
    return "🔄 Продлить\n\nПодписок для продления пока нет."


def renew_menu_text(count: int) -> str:
    return f"🔄 Продлить\n\nДоступно подписок для продления: {count}\nВыберите подписку ниже."


def my_payments_empty_text() -> str:
    return "💳 У вас пока нет платежей."


def payment_detail_text(row, tariff_title: str) -> str:
    line = "━━━━━━━━━━━━━━"
    return (
        "💳 Платёж\n"
        f"{line}\n"
        f"ID: #{row['id']}\n"
        f"Тип: {payment_kind_label(str(row['kind']))}\n"
        f"Тариф: {tariff_title}\n"
        f"Сумма: {payment_amount_text(int(row['amount'] or 0))}\n"
        f"Статус: {payment_status_label(str(row['status']))}\n"
        f"Применение: {str(row['application_status'] or '—')}\n"
        f"Создан: {payment_dt_text(row['created_at'])}\n"
        f"Оплачен: {payment_dt_text(row['paid_at'])}\n"
        f"Применён: {payment_dt_text(row['applied_at'])}\n"
        f"Уведомлён: {payment_dt_text(row['notified_at'])}\n"
        f"Ошибка/закрыт: {payment_dt_text(row['failed_at'])}\n"
        f"Provider charge: {str(row['provider_charge_id'] or '—')}\n"
        f"Заметки: {str(row['notes'] or '—')}\n"
        f"{line}"
    )


def profile_text(
    *,
    user_id: int,
    username: str,
    subscriptions_count: int,
    active_count: int,
    expiring_next_text: str,
    has_pending_payment: bool,
    latest_plans: list[str],
) -> str:
    line = "━━━━━━━━━━━━━━"
    plans_block = "\n".join(f"• {plan}" for plan in latest_plans) if latest_plans else "• Пока нет подписок"
    return (
        "👤 Профиль\n"
        f"{line}\n"
        f"TGID: {user_id}\n"
        f"Username: {username}\n"
        f"Подписок всего: {subscriptions_count}\n"
        f"Активных: {active_count}\n"
        f"Ближайшее окончание: {expiring_next_text}\n"
        f"Есть неоплаченный счёт: {'да' if has_pending_payment else 'нет'}\n"
        f"{line}\n"
        "Последние планы:\n"
        f"{plans_block}"
    )


def faq_main_text() -> str:
    return "❓ FAQ\n\nЗдесь собраны инструкции по подключению, покупке Stars, ограничениям подключений и типовым проблемам."


def faq_connect_menu_text() -> str:
    return "📱 Подключить устройство\n\nВыберите платформу ниже."


def faq_stars_text() -> str:
    return (
        "⭐ Как купить Telegram Stars\n\n"
        "1. Перейдите в @PremiumBot.\n"
        "2. В Menu выберите «Купить или подарить звезды Telegram».\n"
        "3. Или отправьте команду /stars.\n"
        "4. Нажмите «Купить звезды Telegram».\n"
        "5. Выберите нужное количество звезд, например 150⭐️.\n"
        "6. В окне «Оплатить» выберите способ оплаты.\n"
        "7. Оплатить можно банковской картой или через SberPay.\n"
        "8. После оплаты звезды поступят на баланс Telegram.\n"
        "9. Вернитесь в бот и откройте «💳 Предпросмотр покупки».\n"
        "10. Нажмите «⭐ Оплатить Telegram Stars».\n\n"
        "Также Telegram Stars можно купить через сторонние боты и сервисы.\nИногда это выгоднее (до 30%), но условия могут отличаться — используйте проверенные источники.\n\n"
        "Если вы уже создали ⭐️ счёт и не оплатили его, он появится в разделе «💳 Мои платежи», где можно выбрать «⭐ Продолжить оплату»."
    )


def faq_connections_text() -> str:
    return (
        "🛡 Подключения и ограничения\n\n"
        "Для защиты от шаринга подписки используется собственная разработка «AntiAbuse»\n\n"
        "В карточке подписки показывается строка вида «Подключения: N/Лимит».\n\n"
        "Что это значит:\n"
        "• N — текущее число уникальных подключений за короткое временное окно\n"
        "• Лимит — допустимое число подключений для вашей подписки\n"
        "• Лимит устанавливается для каждого тарифа индивидуально\n\n"
        "Если лимит временно превышен, бот может предупредить вас или временно ограничить доступ.\n\n"
        "Что делать:\n"
        "• отключите лишние устройства\n"
        "• подождите несколько минут\n"
        "• если это ошибка — напишите в поддержку"
    )


def faq_restricted_text() -> str:
    return (
        "⛔ Почему доступ ограничен\n\n"
        "Доступ может быть временно приостановлен, если по подписке зафиксировано превышение лимита подключений.\n\n"
        "Обычно это происходит, если:\n"
        "• подписка используется на слишком большом числе устройств одновременно\n"
        "• подписка была передана другим людям\n"
        "• сеть часто меняется и лимит временно выглядит превышенным\n\n"
        "Что делать:\n"
        "• уменьшите количество одновременных подключений\n"
        "• дождитесь окончания ограничения\n"
        "• если это ошибка — напишите в поддержку"
    )


def faq_device_text(platform: str) -> str:
    common = (
        "Кнопки ниже ведут прямо на скачивание приложений.\n"
        "Если одно приложение недоступно, попробуйте резервный вариант.\n\n"
        "После установки приложения, скопируйте ссылку на подписку и вставьте её в приложение через буфер обмена, затем подключитесь."
    )
    mapping = {
        "ios": f"iPhone / iPad\n\n{common}",
        "android": f"Android\n\n{common}",
        "windows": f"Windows\n\n{common}",
        "macos": f"macOS\n\n{common}",
        "androidtv": (
            "Android TV\n\n"
            "Кнопки ниже ведут прямо на скачивание приложений.\n"
            "Для Android TV рекомендуется использовать VPN4TV.\n"
            "Ознакомьтесь с инструкцией на сайте VPN4TV, vless:// ссылка для подключения на TV передается через TG бота VPN4TV.\n\n"
            "Использование Happ — скопируйте ссылку на подписку и вставьте её в приложение через буфер обмена, затем подключитесь."
        ),
    }
    return mapping.get(platform, "Инструкция для этой платформы скоро появится.")


def faq_router_text() -> str:
    return (
        "📡 OpenWRT / Роутеры\n\n"
        "Если у вас роутер с OpenWRT, можно подключить VPN на уровне всей сети.\n\n"
        "Можете ознакомиться с русскоязычными руководствами:"
    )


def faq_proxy_text() -> str:
    return (
        "🚇 Описание протоколов и сервера\n\n"
        "🔑 Протоколы:\n"
        "• VLESS — универсальный вариант для большинства случаев.\n"
        "• TUIC — часто лучше там, где важна задержка и скорость (просмотр видео, игры, звонки).\n"
        "• Hysteria2 — хорошо подходит для нестабильных сетей и мобильного интернета.\n\n"
        "🌏 Сервер:\n"
        "У нас отсутствует список серверов в привычном для рынка VPN виде. \n"
        "Используется группа серверов-нод с разными настройками в виде каскада. \n"
        "Это позволяет разделять трафик и настройки, например:\n"
        "• отключать рекламу на ютубе.\n"
        "• проксировать в WARP.\n"
        "• обеспечивать чистый «потребительский» IP-адрес при работе с VPN."
    )
	

def faq_problems_text() -> str:
    return (
        "❗ Частые проблемы\n\n"
        "1. Не подключается\n"
        "• Проверьте, что ссылка подписки полностью скопирована, в приложении появился список протоколов \n"
        "• Обновите подписку в приложении\n"
        "• Попробуйте другой клиент\n\n"
        "2. Интернета нет после включения\n"
        "• Проверьте режим клиента (например режимы proxy / vpn / direct)\n"
        "• Перезапустите подключение\n"
        "• Импортируйте подписку заново\n\n"
        "3. Медленно работает\n"
        "• Попробуйте другой клиент\n"
        "• Обновите подписку\n"
        "• Проверьте свою сеть\n\n"
        "Если не помогло — откройте поддержку."
    )


def faq_about_text() -> str:
    return (
        "🔐 О VPN\n\n"
        "Сервис работает на sing-box.\n\n"
        "Что важно:\n"
        "• вы получаете ссылку-подписку содержащую в себе доступы для использования в VPN приложениях\n"
        "• для обычных клиентов используйте ссылку «подписка»\n"
        "• для Clash клиентов используйте Clash-ссылку\n"
        "• доступность протоколов в подписке определяет используемое вами приложение, например Happ не покажет TUIC-протокол\n"
        "• открыв подписку в браузере можно извлечь один нужный ключ и использовать его отдельно (например в AmneziaVPN или VPN4TV)\n"
        "• OpenWRT подключается в зависимости от установленных вами пакетов\n\n"
        "Если нужен быстрый старт — начните с раздела «Подключить устройство»."
    )





def telegram_proxy_menu_text() -> str:
    return (
        "Telegram proxy\n\n"
        "Это приватные Telegram proxy для активных пользователей сервиса. "
        "Выберите прокси ниже для подключения."
    )


def telegram_proxy_denied_text() -> str:
    return (
        "Telegram proxy\n\n"
        "Этот раздел доступен только активным клиентам с обычной подпиской.\n\n"
        "Тестовый доступ не даёт доступ к Telegram proxy."
    )


def telegram_proxy_empty_text() -> str:
    return (
        "Telegram proxy\n\n"
        "Это приватные Telegram proxy для активных пользователей сервиса.\n\n"
        "Сейчас доступных Telegram proxy нет."
    )


def telegram_proxy_admin_list_text() -> str:
    return "📡 Telegram proxy\n\nВыберите прокси ниже или добавьте новый."


def telegram_proxy_admin_detail_text(row) -> str:
    status = "🟢 включён" if int(row["enabled"] or 0) else "⚫ выключен"
    line = "━━━━━━━━━━━━━━"
    return (
        "📡 Telegram proxy\n"
        f"{line}\n"
        f"ID: #{row['id']}\n"
        f"Название: {row['title']}\n"
        f"Ссылка: {row['url']}\n"
        f"Статус: {status}\n"
        f"Порядок: {int(row['sort_order'] or 0)}\n"
        f"{line}"
    )


def telegram_proxy_create_prompt_text(step: str) -> str:
    labels = {
        "title": "название кнопки",
        "url": "полную ссылку tg://...",
    }
    return "➕ Telegram proxy\n\nОтправьте " + labels.get(step, step) + "."


def telegram_proxy_edit_prompt_text(proxy_id: int, field_name: str) -> str:
    labels = {
        "title": "новое название кнопки",
        "url": "новую полную ссылку tg://...",
    }
    return (
        "✏️ Telegram proxy\n\n"
        f"Proxy ID: #{proxy_id}\n"
        f"Отправьте {labels.get(field_name, field_name)}."
    )


def telegram_proxy_prompt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отменить", callback_data="admin:tgproxy")]])


def telegram_proxy_edit_prompt_keyboard(proxy_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отменить", callback_data=f"admin:tgproxy:view:{int(proxy_id)}")]])


def telegram_proxy_user_keyboard(rows) -> InlineKeyboardMarkup:
    buttons = []
    for row in rows:
        buttons.append([InlineKeyboardButton(text=str(row["title"]), url=str(row["url"]))])
    buttons.append([InlineKeyboardButton(text="✖️ Закрыть", callback_data="menu:back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def telegram_proxy_admin_list_keyboard(rows) -> InlineKeyboardMarkup:
    buttons = []
    for row in rows:
        status = "🟢" if int(row["enabled"] or 0) else "⚫"
        buttons.append([InlineKeyboardButton(text=f"{status} {row['title']}", callback_data=f"admin:tgproxy:view:{int(row['id'])}")])
    buttons.append([InlineKeyboardButton(text="➕ Добавить proxy", callback_data="admin:tgproxy:create")])
    buttons.append([InlineKeyboardButton(text="← К 🛠 Админ", callback_data="admin:panel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def telegram_proxy_admin_detail_keyboard(row) -> InlineKeyboardMarkup:
    proxy_id = int(row["id"])
    enabled = int(row["enabled"] or 0) == 1
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Название", callback_data=f"admin:tgproxy:edit:{proxy_id}:title")],
            [InlineKeyboardButton(text="🔗 Ссылка tg://", callback_data=f"admin:tgproxy:edit:{proxy_id}:url")],
            [InlineKeyboardButton(text="⚫ Выключить" if enabled else "🟢 Включить", callback_data=f"admin:tgproxy:toggle:{proxy_id}")],
            [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"admin:tgproxy:delete:{proxy_id}")],
            [InlineKeyboardButton(text="← К списку proxy", callback_data="admin:tgproxy")],
        ]
    )

def admin_stats_text(
    *,
    users_count: int,
    payments_total: int,
    payments_success: int,
    payments_pending: int,
    payments_failed: int,
    revenue_total: int,
    revenue_today: int,
    revenue_7d: int,
    revenue_30d: int,
    buys_total: int,
    renews_total: int,
    buys_today: int,
    renews_today: int,
    buys_7d: int,
    renews_7d: int,
    buys_30d: int,
    renews_30d: int,
    tickets_open: int,
    tickets_closed: int,
    subscriptions_total: int,
    active_subscriptions: int,
    expiring_3d: int,
    expiring_1d: int,
    suspicious_tgids: int,
    top_tariffs: list[tuple[str, int]],
) -> str:
    top_lines = ["🏆 Топ тарифов:"]
    if top_tariffs:
        for idx, (tariff_key, count) in enumerate(top_tariffs[:5], start=1):
            top_lines.append(f"{idx}. {tariff_key} — {count}")
    else:
        top_lines.append("Пока нет успешных продаж")

    parts = [
        "📊 Статистика",
        "",
        "👥 Пользователи и платежи:",
        f"• Пользователей с платежами: {users_count}",
        f"• Платежей всего: {payments_total}",
        f"• Success: {payments_success}",
        f"• Pending: {payments_pending}",
        f"• Failed: {payments_failed}",
        "",
        "💰 Выручка:",
        f"• За всё время: {revenue_total} ⭐",
        f"• Сегодня: {revenue_today} ⭐",
        f"• 7 дней: {revenue_7d} ⭐",
        f"• 30 дней: {revenue_30d} ⭐",
        "",
        "🛒 Покупки / продления:",
        f"• Всего покупок: {buys_total}",
        f"• Всего продлений: {renews_total}",
        f"• Сегодня: покупки {buys_today} / продления {renews_today}",
        f"• 7 дней: покупки {buys_7d} / продления {renews_7d}",
        f"• 30 дней: покупки {buys_30d} / продления {renews_30d}",
        "",
        "📦 Подписки:",
        f"• Всего в S-UI: {subscriptions_total}",
        f"• Активных сейчас: {active_subscriptions}",
        f"• Истекают ≤ 3 дней: {expiring_3d}",
        f"• Истекают сегодня/завтра: {expiring_1d}",
        "",
        "🎫 Поддержка:",
        f"• Активных тикетов: {tickets_open}",
        f"• Закрытых тикетов: {tickets_closed}",
        "",
        "⚠️ Быстрые сигналы:",
        f"• Pending платежей: {payments_pending}",
        f"• Истекают ≤ 1 дня: {expiring_1d}",
        f"• Истекают ≤ 3 дней: {expiring_3d}",
        f"• Подозрительных TGID: {suspicious_tgids}",
        "",
        *top_lines,
    ]
    return "\n".join(parts)


def support_menu_text() -> str:
    return "🆘 Поддержка\n\nЗдесь можно открыть тикет, посмотреть свои обращения и продолжить диалог внутри тикета."


def support_no_tickets_text() -> str:
    return "🆘 У вас пока нет обращений."


SUPPORT_NEW_TICKET_COOLDOWN_SECONDS = 3600


def support_new_ticket_cooldown_text(remaining_seconds: int) -> str:
    minutes = max(1, int((remaining_seconds + 59) // 60))
    return (
        "🕒 Новое обращение можно создать не чаще одного раза в час.\n\n"
        f"Попробуйте снова примерно через {minutes} мин.\n\n"
        "Если у вас уже есть активный тикет — продолжайте диалог в нём."
    )


def support_new_ticket_prompt_text() -> str:
    return (
        "🆕 Новое обращение\n\n"
        "Отправьте одним сообщением текст проблемы.\n\n"
        "Первое предложение будет использовано как заголовок тикета."
    )


def support_ticket_created_text(ticket_id: int) -> str:
    return (
        "✅ Ваше обращение создано\n\n"
        f"Тикет #{ticket_id}\n"
        "Ожидайте ответа поддержки.\n"
        "Новые сообщения придут отдельным уведомлением."
    )


def support_ticket_card_text(ticket, messages) -> str:
    line = "━━━━━━━━━━━━━━"
    subject = str(ticket["subject"] or "—")
    parts = [
        f"🆘 Тикет #{ticket['id']}",
        line,
        f"Статус: {ticket_status_label(str(ticket['status']))}",
        f"Этап: {ticket_waiting_label(str((ticket['waiting_for'] if 'waiting_for' in ticket.keys() else 'admin') or 'admin'), str(ticket['status']))}",
        f"Тема: {subject}",
        line,
        "",
    ]
    if not messages:
        parts.append("Сообщений пока нет.")
        return "\n".join(parts)
    for msg in messages:
        role = str(msg["sender_role"])
        body = support_message_body_from_row(msg)
        if role == "user":
            prefix = "👤 Вы"
        elif role == "admin":
            prefix = "🛠 Поддержка"
        else:
            prefix = "📝 Заметка админа"
        parts.append(f"{prefix}:")
        parts.append(body)
        parts.append("")
    return "\n".join(parts).strip()


def support_ticket_preview_text(ticket, messages) -> str:
    line = "━━━━━━━━━━━━━━"
    subject = str(ticket["subject"] or "—")
    visible_messages = list(messages)[-12:]
    parts = [
        f"🆘 Тикет #{ticket['id']}",
        line,
        f"Статус: {ticket_status_label(str(ticket['status']))}",
        f"Этап: {ticket_waiting_label(str((ticket['waiting_for'] if 'waiting_for' in ticket.keys() else 'admin') or 'admin'), str(ticket['status']))}",
        f"Тема: {subject}",
        line,
        "",
    ]
    if not visible_messages:
        parts.append("Сообщений пока нет.")
        return "\n".join(parts)
    for msg in visible_messages:
        role = str(msg["sender_role"])
        body = support_message_body_from_row(msg)
        if role == "user":
            prefix = "👤 Вы"
        elif role == "admin":
            prefix = "🛠 Поддержка"
        else:
            prefix = "📝 Заметка админа"
        parts.append(f"{prefix}:")
        parts.append(body)
        parts.append("")
    if len(messages) > len(visible_messages):
        parts.append("… показаны последние 12 сообщений")
    return "\n".join(parts).strip()


def support_message_placeholder(content_type: str, file_name: str = "") -> str:
    mapping = {
        "photo": "📷 Фото",
        "video": "🎬 Видео",
        "document": "📄 Документ",
        "audio": "🎧 Аудио",
        "voice": "🎤 Голосовое сообщение",
        "text": "",
    }
    base = mapping.get(str(content_type or "text"), "📎 Медиа")
    if file_name and str(content_type) == "document":
        return f"{base}: {file_name}"
    return base or "—"


def support_message_body_from_row(row) -> str:
    body = str(row["message_text"] or "").strip()
    content_type = str(row["content_type"] or "text")
    if content_type == "text":
        return body or "—"
    placeholder = support_message_placeholder(content_type, str(row["file_name"] or ""))
    return f"{placeholder}\n{body}" if body else placeholder


def support_extract_message_payload(message: types.Message) -> dict | None:
    text_value = (message.text or message.caption or "").strip()

    if message.text and not (message.text or "").startswith("/"):
        return {
            "message_text": text_value,
            "content_type": "text",
            "file_id": None,
            "file_name": None,
            "mime_type": None,
            "preview_text": text_value or "—",
        }

    if message.photo:
        photo = message.photo[-1]
        preview = f"📷 Фото\n{text_value}" if text_value else "📷 Фото"
        return {
            "message_text": text_value,
            "content_type": "photo",
            "file_id": getattr(photo, "file_id", None),
            "file_name": "photo.jpg",
            "mime_type": "image/jpeg",
            "preview_text": preview,
        }

    if message.document:
        doc = message.document
        file_name = str(getattr(doc, "file_name", "") or "")
        placeholder = support_message_placeholder("document", file_name)
        preview = f"{placeholder}\n{text_value}" if text_value else placeholder
        return {
            "message_text": text_value,
            "content_type": "document",
            "file_id": getattr(doc, "file_id", None),
            "file_name": file_name,
            "mime_type": getattr(doc, "mime_type", None),
            "preview_text": preview,
        }

    if message.video:
        video = message.video
        preview = f"🎬 Видео\n{text_value}" if text_value else "🎬 Видео"
        return {
            "message_text": text_value,
            "content_type": "video",
            "file_id": getattr(video, "file_id", None),
            "file_name": getattr(video, "file_name", None),
            "mime_type": getattr(video, "mime_type", None),
            "preview_text": preview,
        }

    if message.audio:
        audio = message.audio
        file_name = str(getattr(audio, "file_name", "") or "")
        placeholder = support_message_placeholder("audio", file_name)
        preview = f"{placeholder}\n{text_value}" if text_value else placeholder
        return {
            "message_text": text_value,
            "content_type": "audio",
            "file_id": getattr(audio, "file_id", None),
            "file_name": file_name,
            "mime_type": getattr(audio, "mime_type", None),
            "preview_text": preview,
        }

    if message.voice:
        voice = message.voice
        preview = f"🎤 Голосовое сообщение\n{text_value}" if text_value else "🎤 Голосовое сообщение"
        return {
            "message_text": text_value,
            "content_type": "voice",
            "file_id": getattr(voice, "file_id", None),
            "file_name": "voice.ogg",
            "mime_type": getattr(voice, "mime_type", None),
            "preview_text": preview,
        }

    return None


async def support_send_payload_to_chat(bot: Bot, chat_id: int, payload: dict) -> None:
    content_type = str(payload.get("content_type") or "text")
    message_text = str(payload.get("message_text") or "").strip()
    file_id = payload.get("file_id")

    # Текст уже приходит в карточке тикета / уведомлении.
    # Повторно отдельным сообщением отправляем только медиа.
    if content_type == "text":
        return

    if content_type == "photo" and file_id:
        await bot.send_photo(chat_id, file_id, caption=message_text or None)
        return
    if content_type == "video" and file_id:
        await bot.send_video(chat_id, file_id, caption=message_text or None)
        return
    if content_type == "document" and file_id:
        await bot.send_document(chat_id, file_id, caption=message_text or None)
        return
    if content_type == "audio" and file_id:
        await bot.send_audio(chat_id, file_id, caption=message_text or None)
        return
    if content_type == "voice" and file_id:
        await bot.send_voice(chat_id, file_id, caption=message_text or None)
        return


async def support_get_reopen_cooldown_left(ticket_id: int) -> int:
    async with aiosqlite.connect(SUPPORT_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT last_user_reopen_at FROM support_tickets WHERE id=?", (int(ticket_id),)) as cur:
            row = await cur.fetchone()
            if not row or not row["last_user_reopen_at"]:
                return 0
            left = int((float(row["last_user_reopen_at"]) + 3600) - datetime.now().timestamp())
            return max(0, left)


async def support_mark_user_reopen(ticket_id: int) -> None:
    async with aiosqlite.connect(SUPPORT_DB) as conn:
        await conn.execute("UPDATE support_tickets SET last_user_reopen_at=? WHERE id=?", (datetime.now().timestamp(), int(ticket_id)))
        await conn.commit()


def support_admin_list_text(status_filter: str = "all", query_label: str = "") -> str:
    labels = {
        "all": "Все",
        "open": "Открытые",
        "answered": "Отвечённые",
        "closed": "Закрытые",
    }
    line = "━━━━━━━━━━━━━━"
    text = (
        "🛠 Тикеты поддержки\n"
        f"{line}\n"
        f"Фильтр: {labels.get(status_filter, status_filter)}\n"
    )
    if query_label:
        text += f"Поиск: {query_label}\n"
    text += f"{line}\n\nВыберите тикет ниже."
    return text


def support_reply_prompt_text(ticket_id: int) -> str:
    return f"✍️ Ответ в тикет #{ticket_id}\n\nОтправьте следующим сообщением текст ответа."


def support_note_prompt_text(ticket_id: int) -> str:
    return f"📝 Заметка к тикету #{ticket_id}\n\nОтправьте следующим сообщением внутреннюю заметку. Пользователь её не увидит."


def quick_reply_text(key: str) -> str:
    return {
        "restart": "Пожалуйста, полностью закройте и заново откройте приложение, затем обновите подписку и попробуйте подключиться ещё раз.",
        "internet": "Пожалуйста, проверьте, что интернет работает без VPN, затем попробуйте другую сеть: Wi‑Fi или мобильный интернет.",
        "reinstall": "Рекомендуем удалить приложение, установить его заново, затем снова импортировать подписку и проверить подключение.",
        "server": "Попробуйте заново обновить подписку в приложении. Если проблема сохранится, напишите, на каком устройстве и в каком приложении это происходит.",
    }.get(key, "")



def support_admin_search_prompt_text() -> str:
    return (
        "🔎 Поиск тикета\n\n"
        "Отправьте следующим сообщением:\n"
        "• номер тикета, например 15 или #15\n"
        "• либо TGID пользователя\n\n"
        "Будет открыт список найденных тикетов."
    )

def support_ticket_search_prompt_text() -> str:
    return support_admin_search_prompt_text()


async def get_user_profile_stats(user_id: int) -> dict:
    username = "—"
    async with aiosqlite.connect(PAYMENTS_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success_count,
                MAX(COALESCE(NULLIF(username, ''), '')) AS username
            FROM payments
            WHERE user_id=?
            """,
            (int(user_id),),
        ) as cur:
            pay_row = await cur.fetchone()
            if pay_row and pay_row["username"]:
                username = str(pay_row["username"])

    async with aiosqlite.connect(SUPPORT_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """
            SELECT COUNT(*) AS total, MAX(COALESCE(NULLIF(username, ''), '')) AS username
            FROM support_tickets
            WHERE user_id=?
            """,
            (int(user_id),),
        ) as cur:
            ticket_row = await cur.fetchone()
            if ticket_row and ticket_row["username"]:
                username = str(ticket_row["username"])

    if username and username != "—" and not username.startswith("@"):
        username = "@" + username

    return {
        "username": username or "—",
        "payments_total": int(pay_row["total"] or 0),
        "payments_success": int(pay_row["success_count"] or 0),
        "tickets_total": int(ticket_row["total"] or 0),
    }



def admin_user_profile_text(*, user_id: int, username: str, payments_total: int, payments_success: int, tickets_total: int, active_subscriptions: int, latest_plans: list[str]) -> str:
    plans_block = "\n".join(f"• {p}" for p in latest_plans) if latest_plans else "• Нет данных"
    return (
        "👤 Профиль пользователя\n\n"
        f"TGID: {user_id}\n"
        f"Username: {username}\n\n"
        f"Платежей всего: {payments_total}\n"
        f"Успешных платежей: {payments_success}\n"
        f"Тикетов всего: {tickets_total}\n"
        f"Активных подписок: {active_subscriptions}\n\n"
        "Последние планы:\n"
        f"{plans_block}"
    )


def tariffs_admin_list_text() -> str:
    return "💼 Управление тарифами\n\nВыберите тариф ниже."


def tariff_admin_detail_text(tariff: Tariff) -> str:
    line = "━━━━━━━━━━━━━━"
    enabled = "🟢 включён" if tariff_enabled(tariff.key) else "⚫ выключен"
    visibility = "только админы" if tariff.is_admin_only else "публичный"
    return (
        "💼 Тариф\n"
        f"{line}\n"
        f"Ключ: {tariff.key}\n"
        f"Название: {tariff.title}\n"
        f"Срок: {tariff.days} дн.\n"
        f"Трафик: {human_traffic(tariff.traffic_gb)}\n"
        f"Цена: {tariff.stars_price} ⭐\n"
        f"Лимит подключений: {tariff.connection_limit}\n"
        f"Stars URL: {tariff.stars_purchase_url or 'по умолчанию'}\n"
        f"Статус: {enabled}\n"
        f"Доступ: {visibility}\n"
        f"{line}"
    )


def tariff_edit_prompt_text(tariff_key: str, field_name: str) -> str:
    labels = {
        "title": "новое название",
        "days": "новый срок в днях",
        "traffic_gb": "новый трафик в ГБ или ∞",
        "stars_price": "новую цену в Stars",
        "connection_limit": "новый лимит подключений",
        "stars_purchase_url": "новую ссылку Stars Bot или - для сброса",
    }
    return (
        "✏️ Редактирование тарифа\n\n"
        f"Тариф: {tariff_key}\n"
        f"Отправьте {labels.get(field_name, field_name)} следующим сообщением."
    )



async def build_admin_user_profile_text(user_id: int) -> str:
    async with aiosqlite.connect(PAYMENTS_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success_count,
                MAX(COALESCE(NULLIF(username, ''), '')) AS username
            FROM payments
            WHERE user_id=?
            """,
            (int(user_id),),
        ) as cur:
            pay_row = await cur.fetchone()

    async with aiosqlite.connect(SUPPORT_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                MAX(COALESCE(NULLIF(username, ''), '')) AS username
            FROM support_tickets
            WHERE user_id=?
            """,
            (int(user_id),),
        ) as cur:
            ticket_row = await cur.fetchone()

    username = pay_row["username"] or ticket_row["username"] or "—"

    latest_plans = []
    active_subscriptions = 0
    try:
        items = await get_user_subscriptions(user_id)
        active_subscriptions = len([x for x in items if int(x["days_left"]) > 0])
        for item in items[:5]:
            plan = str(item.get("plan_key") or "без плана")
            if plan not in latest_plans:
                latest_plans.append(plan)
    except Exception:
        pass

    stats = await get_user_profile_stats(user_id)

    return admin_user_profile_text(
        user_id=user_id,
        username=username if str(username).strip() else "—",
        payments_total=int(stats["payments_total"]),
        payments_success=int(stats["payments_success"]),
        tickets_total=int(stats["tickets_total"]),
        active_subscriptions=active_subscriptions,
        latest_plans=latest_plans,
    )


def reminder_expiring_text(item: dict, days_left: int) -> str:
    day_word = "день" if days_left == 1 else "дня" if days_left in (2,3,4) else "дней"
    return (
        "⏰ Напоминание о подписке\n\n"
        f"Подписка: {item['name']}\n"
        f"Истекает через {days_left} {day_word}\n"
        f"Тариф: {item.get('plan_key') or '—'}\n\n"
        "Вы можете заранее продлить подписку в разделе «🔄 Продлить»."
    )


def reminder_expired_text(item: dict) -> str:
    return (
        "⌛ Срок подписки закончился\n\n"
        f"Подписка: {item['name']}\n"
        "Чтобы снова пользоваться VPN, откройте раздел «🔄 Продлить»."
    )


def reminders_panel_text(*, expiring_3d: int, expiring_1d: int, expired_now: int, marks_3d: int, marks_1d: int, marks_expired: int, last_run_text: str, last_run_stats: str) -> str:
    line = "━━━━━━━━━━━━━━"
    parts = [
        "⏰ Напоминания",
        line,
        "Очередь по подпискам:",
        f"• Истекают в ближайшие 3 дня: {expiring_3d}",
        f"• Истекают сегодня/завтра: {expiring_1d}",
        f"• Уже истекли: {expired_now}",
        line,
        "Отправлено напоминаний:",
        f"• За 3 дня: {marks_3d}",
        f"• За 1 день: {marks_1d}",
        f"• После истечения: {marks_expired}",
        line,
        f"Последний запуск: {last_run_text}",
        last_run_stats,
    ]
    return "\n".join(parts)


def reminders_templates_text() -> str:
    line = "━━━━━━━━━━━━━━"
    return (
        "✉️ Тексты напоминаний\n"
        f"{line}\n"
        "За 3 дня:\n"
        "⏰ Напоминание о подписке — скоро потребуется продление.\n\n"
        "За 1 день:\n"
        "⏰ Напоминание о подписке — истекает сегодня или завтра.\n\n"
        "После истечения:\n"
        "⌛ Срок подписки закончился — можно продлить в разделе «🔄 Продлить»."
    )


def admin_panel_text() -> str:
    return "🛠 Админ-панель\n\nВыберите раздел ниже."




def antiabuse_status_label(status: str) -> str:
    raw = str(status or "new")
    if raw == "review":
        raw = "check"
    return {
        "new": "🆕 новый",
        "check": "🟡 на проверке",
        "ok": "✅ норма",
        "ignore": "🙈 игнор",
    }.get(raw, raw)


def antiabuse_case_list_text(cases: list[dict], status: str = "suspicious", page: int = 1, per_page: int = 10) -> str:
    title_map = {
        "suspicious": "🛡 Antiabuse отчёт",
        "all": "📋 Все кейсы",
        "check": "🟡 На проверке",
        "ignore": "🙈 Игнор",
        "ok": "✅ Норма",
    }
    title = title_map.get(status, "🛡 Antiabuse отчёт")
    if not cases:
        return f"{title}\n\nНичего не найдено."

    suspicious = [x for x in cases if x.get("unique_window", 0) >= x.get("limit_value", 10)]
    total = len(cases)
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(int(page or 1), pages))

    parts = [title, ""]
    parts.append(f"Всего кейсов: {total}")
    parts.append(f"Подозрительных по лимиту: {len(suspicious)}")
    parts.append(f"Страница: {page}/{pages}")

    if status == "all":
        if suspicious:
            parts.append("")
            parts.append("Сейчас выше или на лимите:")
            for item in suspicious[:5]:
                owner = item.get("username") or (f"TGID {item.get('tgid')}" if item.get("tgid") else "владелец неизвестен")
                parts.append(f"• {item['name']} — {owner} — IP {item['unique_window']}/{item['limit_value']}")
            if len(suspicious) > 5:
                parts.append(f"… ещё {len(suspicious) - 5} кейсов")
        return "\n".join(parts).strip()

    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    page_items = cases[start_idx:end_idx]
    line = "━━━━━━━━━━━━━━"
    parts.append("")
    for item in page_items:
        owner = item.get("username") or (f"TGID {item['tgid']}" if item.get("tgid") else "владелец неизвестен")
        parts.append(f"{item['name']}")
        parts.append(f"{owner}")
        parts.append(f"IP: {item['unique_window']}/{item['limit_value']} за {ANTIABUSE_WINDOW_MINUTES} мин · статус: {antiabuse_status_label(item['admin_status'])}")
        if item.get("admin_note"):
            parts.append(f"Заметка: {str(item['admin_note'])[:100]}")
        parts.append(line)
    return "\n".join(parts).strip()


def antiabuse_case_detail_text(item: dict) -> str:
    owner = item.get("username") or "—"
    plan = item.get("plan_key") or "—"
    ips = item.get("recent_ips") or []
    limit_text = f"{item['limit_value']} (override)" if item.get("has_override") else str(item["limit_value"])
    parts = [
        "🛡 Antiabuse кейс",
        "━━━━━━━━━━━━━━",
        f"Name: {item['name']}",
        f"Статус: {antiabuse_status_label(item['admin_status'])}",
        f"Лимит IP: {limit_text}",
        f"Сервер: {item.get('server_name') or '—'}",
        f"Тариф: {plan}",
        f"TGID: {item.get('tgid') or '—'}",
        f"User: {owner}",
        "━━━━━━━━━━━━━━",
        f"IP за {ANTIABUSE_WINDOW_MINUTES} мин: {item['unique_window']}",
        f"IP за 1 час: {item['unique_hour']}",
        f"IP за 24 часа: {item['unique_day']}",
        f"Уникальных за {ANTIABUSE_RETENTION_HOURS} ч: {item['unique_total']}",
        f"Последняя активность: {item['last_seen_text']}",
        f"Risk score: {item['risk_score']}",
        f"Предупреждений: первое {item.get('warn1_count', 0)} / второе {item.get('warn2_count', 0)}",
        f"Ограничение: {'⛔ активно до ' + item.get('disabled_until_text', '—') if item.get('disabled_active') else 'нет'}",
        "",
        "Причины:" if item["reasons"] else "Причины: не выявлены",
    ]
    for reason in item["reasons"]:
        parts.append(f"• {reason}")
    parts.append("")
    parts.append("Последние IP:")
    if ips:
        for ip in ips[:10]:
            parts.append(f"• {ip}")
    else:
        parts.append("• нет данных")
    parts.append("")
    parts.append("Заметка администратора:")
    parts.append(item["admin_note"] if item["admin_note"] else "—")
    return "\n".join(parts).strip()


def antiabuse_keyboard(cases: list[dict], mode: str = "suspicious", page: int = 1, per_page: int = 10) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="⚠️ Только подозрительные", callback_data="antiabuse:report"),
            InlineKeyboardButton(text="📋 Все", callback_data="antiabuse:all"),
        ],
        [
            InlineKeyboardButton(text="🟡 На проверке", callback_data="antiabuse:check"),
            InlineKeyboardButton(text="🙈 Игнор", callback_data="antiabuse:ignore"),
        ],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"antiabuse:refresh:{mode}:{page}")],
    ]

    total = len(cases)
    page = max(1, int(page or 1))
    pages = max(1, (total + per_page - 1) // per_page)
    if page > pages:
        page = pages

    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page

    for item in cases[start_idx:end_idx]:
        badge = antiabuse_status_label(item["admin_status"]).split(" ", 1)[0]
        rows.append([
            InlineKeyboardButton(
                text=f"{badge} {item['unique_window']}/{item['limit_value']} · {item['name'][:28]}",
                callback_data=f"antiabuse:view:{item['case_id']}:{mode}",
            )
        ])

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"antiabuse:page:{mode}:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page}/{pages}", callback_data="noop"))
    if page < pages:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"antiabuse:page:{mode}:{page+1}"))
    rows.append(nav)

    rows.append([InlineKeyboardButton(text="← К 🛠 Админ", callback_data="admin:panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def antiabuse_detail_keyboard(case_id: int, mode: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🟡 На проверке", callback_data=f"antiabuse:mark:check:{case_id}:{mode}")],
            [InlineKeyboardButton(text="✅ Норма", callback_data=f"antiabuse:mark:ok:{case_id}:{mode}")],
            [InlineKeyboardButton(text="🙈 Игнор", callback_data=f"antiabuse:mark:ignore:{case_id}:{mode}")],
            [InlineKeyboardButton(text="📝 Заметка", callback_data=f"antiabuse:note:{case_id}:{mode}")],
            [InlineKeyboardButton(text="✏️ Лимит IP", callback_data=f"antiabuse:limit:{case_id}:{mode}")],
            [InlineKeyboardButton(text="♻️ Сбросить лимит", callback_data=f"antiabuse:limit_reset:{case_id}:{mode}")],
            [InlineKeyboardButton(text="⛔ Отключить на 10 мин", callback_data=f"antiabuse:disable:{case_id}:{mode}:{ANTIABUSE_DISABLE_DEFAULT_MINUTES}")],
            [InlineKeyboardButton(text="⏱ Свой срок", callback_data=f"antiabuse:disable_custom:{case_id}:{mode}")],
            [InlineKeyboardButton(text="✅ Включить сейчас", callback_data=f"antiabuse:enable:{case_id}:{mode}")],
            [InlineKeyboardButton(text="← К списку", callback_data=f"antiabuse:refresh:{mode}:1")],
        ]
    )


async def render_antiabuse_list(callback: types.CallbackQuery, status: str = "suspicious", page: int = 1) -> None:
    try:
        cases = await antiabuse_build_cases(force_refresh=True)
        if status == "all":
            filtered = list(cases)
        elif status in {"check", "ignore", "ok"}:
            filtered = [x for x in cases if x["admin_status"] == status]
        else:
            filtered = [x for x in cases if x["suspicious"]]
            status = "suspicious"
        await safe_edit_text(
            callback.message,
            antiabuse_case_list_text(filtered, status=status, page=page),
            reply_markup=antiabuse_keyboard(filtered, status, page=page),
        )
    except Exception as exc:
        await safe_edit_text(
            callback.message,
            "❌ Не удалось собрать antiabuse отчёт.\n\n" + str(exc)[:700],
            reply_markup=admin_subpage_keyboard(),
        )


async def render_antiabuse_case(callback: types.CallbackQuery, case_id: int, mode: str = "suspicious") -> None:
    try:
        cases = await antiabuse_build_cases(force_refresh=True)
        item = next((x for x in cases if int(x["case_id"]) == int(case_id)), None)
        if not item:
            await safe_edit_text(callback.message, "Кейс antiabuse не найден.", reply_markup=admin_subpage_keyboard())
            return
        await safe_edit_text(
            callback.message,
            antiabuse_case_detail_text(item),
            reply_markup=antiabuse_detail_keyboard(case_id, mode if mode in ("all", "suspicious", "check", "ignore", "ok") else "suspicious"),
        )
    except Exception as exc:
        await safe_edit_text(
            callback.message,
            "❌ Не удалось открыть кейс antiabuse.\n\n" + str(exc)[:700],
            reply_markup=admin_subpage_keyboard(),
        )

def tariff_admin_list_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for tariff in list_all_tariffs_for_admin():
        status = "🟢" if tariff_enabled(tariff.key) else "⚫"
        vis = "🔒" if tariff.is_admin_only else "🌍"
        tag = "🆕 " if tariff.is_custom else ""
        rows.append([InlineKeyboardButton(text=f"{tag}{status} {vis} {tariff.title}", callback_data=f"admin:tariff:view:{tariff.key}")])
    rows.append([InlineKeyboardButton(text="➕ Создать тариф", callback_data="admin:tariff:create")])
    rows.append([InlineKeyboardButton(text="← К 🛠 Админ", callback_data="admin:panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def tariff_admin_detail_keyboard(tariff: Tariff) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="✏️ Название", callback_data=f"admin:tariff:edit:{tariff.key}:title")],
        [InlineKeyboardButton(text="⏱ Срок", callback_data=f"admin:tariff:edit:{tariff.key}:days")],
        [InlineKeyboardButton(text="📦 Трафик", callback_data=f"admin:tariff:edit:{tariff.key}:traffic_gb")],
        [InlineKeyboardButton(text="⭐ Цена", callback_data=f"admin:tariff:edit:{tariff.key}:stars_price")],
        [InlineKeyboardButton(text="🔌 Лимит подключений", callback_data=f"admin:tariff:edit:{tariff.key}:connection_limit")],
        [InlineKeyboardButton(text="🔗 Stars Bot URL", callback_data=f"admin:tariff:edit:{tariff.key}:stars_purchase_url")],
        [InlineKeyboardButton(text="🟢/⚫ Вкл/Выкл", callback_data=f"admin:tariff:toggle_enabled:{tariff.key}")],
        [InlineKeyboardButton(text="🔒/🌍 Admin/Public", callback_data=f"admin:tariff:toggle_admin:{tariff.key}")],
    ]
    if tariff.is_custom:
        rows.append([InlineKeyboardButton(text="🗑 Удалить тариф", callback_data=f"admin:tariff:delete:{tariff.key}")])
    rows.append([InlineKeyboardButton(text="← К тарифам", callback_data="admin:tariffs")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_search_prompt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отменить", callback_data="support:admin:list")]])



def tariff_create_prompt_text(step: str) -> str:
    labels = {
        "key": "ключ тарифа латиницей/цифрами/подчёркиванием",
        "title": "название тарифа",
        "days": "срок в днях",
        "traffic_gb": "трафик в ГБ или ∞",
        "stars_price": "цену в Stars",
        "is_admin_only": "доступ: public или admin",
        "connection_limit": "лимит подключений",
        "stars_purchase_url": "ссылку для покупки Stars Bot или - для пропуска",
    }
    return "➕ Создание тарифа\n\nОтправьте " + labels.get(step, step) + "."


def tariff_create_prompt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отменить", callback_data="admin:tariffs")]])
def tariff_edit_prompt_keyboard(tariff_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отменить", callback_data=f"admin:tariff:view:{tariff_key}")]])


def close_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Закрыть", callback_data="menu:back")]])


def support_prompt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отменить", callback_data="support:menu")]])


def broadcast_prompt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Отменить", callback_data="admin:panel")]]
    )


def broadcast_preview_keyboard(has_button: bool) -> InlineKeyboardMarkup:
    rows = []
    if has_button:
        rows.append([InlineKeyboardButton(text="🗑 Убрать кнопку", callback_data="admin:broadcast:clear_button")])
    else:
        rows.append([InlineKeyboardButton(text="🔗 Добавить кнопку", callback_data="admin:broadcast:add_button")])
    rows.append([InlineKeyboardButton(text="✅ Отправить", callback_data="admin:broadcast:send")])
    rows.append([InlineKeyboardButton(text="Отменить", callback_data="admin:panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def faq_subpage_keyboard(back_callback: str = "faq:main", back_text: str = "← К FAQ") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=back_text, callback_data=back_callback)]]
    )


def faq_router_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📘 Гайды Mihomo / Clash", url="https://ssclash.notion.site/Mihomo-Clash-15989188f6b48051a97fc887adea736a")],
            [InlineKeyboardButton(text="📚 Mihomo Docs", url="https://wiki.metacubex.one/ru/startup/client/client/")],
            [InlineKeyboardButton(text="← К FAQ", callback_data="faq:main")],
        ]
    )


def stars_faq_keyboard_from_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛒 Купить Telegram Stars", url="https://t.me/PremiumBot")],
            [InlineKeyboardButton(text="🛒 Купить Telegram Stars (Bot)", url="https://t.me/stars_mint_bot?start=ref_v4u8c1v47pfs")],
            [InlineKeyboardButton(text="← К FAQ", callback_data="faq:main")],
        ]
    )


def stars_faq_keyboard_from_buy(tariff_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛒 Купить Telegram Stars", url="https://t.me/PremiumBot")],
            [InlineKeyboardButton(text="🛒 Купить Telegram Stars (Bot)", url="https://t.me/stars_mint_bot?start=ref_v4u8c1v47pfs")],
            [InlineKeyboardButton(text="← Назад", callback_data=f"faq:stars:back:{tariff_key}")],
        ]
    )


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return close_keyboard()



def direct_message_prompt_target_text() -> str:
    return (
        "📩 Сообщение пользователю\n\n"
        "Отправьте следующим сообщением TGID или @username пользователя."
    )


def direct_message_prompt_text(user_id: int, username: str) -> str:
    uname = username if username else "—"
    return (
        "📩 Сообщение пользователю\n\n"
        f"Получатель: {user_id}\n"
        f"Username: {uname}\n\n"
        "Теперь отправьте текст сообщения."
    )


def direct_message_preview_text(user_id: int, username: str, text: str) -> str:
    uname = username if username else "—"
    return (
        "📩 Предпросмотр сообщения\n\n"
        f"Получатель: {user_id}\n"
        f"Username: {uname}\n\n"
        "Текст:\n"
        f"{text}"
    )


def direct_message_prompt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Отменить", callback_data="admin:panel")]]
    )


def direct_message_preview_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Отправить", callback_data="admin:direct_message:send")],
            [InlineKeyboardButton(text="✏️ Изменить текст", callback_data="admin:direct_message:edit_text")],
            [InlineKeyboardButton(text="Отменить", callback_data="admin:panel")],
        ]
    )


async def find_user_for_direct_message(raw_query: str):
    q = (raw_query or "").strip()
    if not q:
        return None
    q_lower = q.lower()
    q_no_at = q_lower[1:] if q_lower.startswith("@") else q_lower

    async def by_user_id(uid: int):
        username = ""
        async with aiosqlite.connect(PAYMENTS_DB) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT MAX(COALESCE(NULLIF(username, ''), '')) AS username FROM payments WHERE user_id=?",
                (uid,),
            ) as cur:
                row = await cur.fetchone()
                if row and row["username"]:
                    username = str(row["username"])
        if not username:
            async with aiosqlite.connect(SUPPORT_DB) as conn:
                conn.row_factory = aiosqlite.Row
                async with conn.execute(
                    "SELECT MAX(COALESCE(NULLIF(username, ''), '')) AS username FROM support_tickets WHERE user_id=?",
                    (uid,),
                ) as cur:
                    row = await cur.fetchone()
                    if row and row["username"]:
                        username = str(row["username"])
        return {"user_id": uid, "username": username}

    if q.isdigit():
        return await by_user_id(int(q))

    q_norm = q[1:] if q.startswith("@") else q
    q_norm = q_norm.strip().lstrip("@")
    if not q_norm:
        return None

    async with aiosqlite.connect(PAYMENTS_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """
            SELECT user_id, MAX(COALESCE(NULLIF(username, ''), '')) AS username
            FROM payments
            WHERE LOWER(COALESCE(username, ''))=LOWER(?)
            GROUP BY user_id
            ORDER BY MAX(created_at) DESC
            LIMIT 1
            """,
            (q_norm,),
        ) as cur:
            row = await cur.fetchone()
            if row:
                return {"user_id": int(row["user_id"]), "username": ("@" + str(row["username"]).lstrip("@")) if row["username"] else ""}

    async with aiosqlite.connect(SUPPORT_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """
            SELECT user_id, MAX(COALESCE(NULLIF(username, ''), '')) AS username
            FROM support_tickets
            WHERE LOWER(COALESCE(username, ''))=LOWER(?)
            GROUP BY user_id
            ORDER BY MAX(updated_at) DESC
            LIMIT 1
            """,
            (q_norm,),
        ) as cur:
            row = await cur.fetchone()
            if row:
                return {"user_id": int(row["user_id"]), "username": ("@" + str(row["username"]).lstrip("@")) if row["username"] else ""}

    try:
        clients = await get_all_clients()
        for client in clients:
            desc = str(client.get("desc") or "")
            username = parse_username_from_desc(desc).lstrip("@")
            if username and username.lower() == q_norm.lower():
                tgid = parse_tgid_from_desc(desc)
                if tgid:
                    return {"user_id": int(tgid), "username": "@" + username}
    except Exception:
        pass

    return None



def analytics_panel_text() -> str:
    return "📊 Аналитика\n\nВыберите раздел ниже."


def analytics_overview_text(stats: dict) -> str:
    return (
        "📊 Общая статистика\n\n"
        f"👥 Всего пользователей: {stats['users_total']}\n"
        f"🟢 Пользователей с подписками: {stats['users_with_subscriptions']}\n"
        f"🆕 Новых за 24 часа: {stats['new_users_24h']}\n"
        f"📅 Новых за 7 дней: {stats['new_users_7d']}"
    )


def analytics_payments_text(stats: dict) -> str:
    return (
        "💳 Аналитика платежей\n\n"
        f"✅ Успешных за 24ч: {stats['success_24h']}\n"
        f"✅ Успешных за 7д: {stats['success_7d']}\n"
        f"✅ Успешных за 30д: {stats['success_30d']}\n\n"
        f"⭐ Stars за 24ч: {stats['stars_24h']}\n"
        f"⭐ Stars за 7д: {stats['stars_7d']}\n"
        f"⭐ Stars за 30д: {stats['stars_30d']}\n\n"
        f"🧾 Неоплаченных счетов: {stats['pending_total']}\n"
        f"❌ Отменённых/ошибок: {stats['failed_total']}"
    )


def analytics_support_text(stats: dict) -> str:
    return (
        "🎫 Аналитика поддержки\n\n"
        f"📬 Открытых тикетов: {stats['open_total']}\n"
        f"⏳ Ждут администратора: {stats['waiting_admin']}\n"
        f"👤 Ждут пользователя: {stats['waiting_user']}\n"
        f"✅ Закрытых: {stats['closed_total']}\n\n"
        f"🆕 Создано за 24ч: {stats['created_24h']}\n"
        f"🆕 Создано за 7д: {stats['created_7d']}"
    )


def analytics_antiabuse_text(stats: dict) -> str:
    lines = [
        "🛡 Аналитика antiabuse",
        "",
        f"⚠️ warn1: {stats['warn1_total']}",
        f"⚠️ warn2: {stats['warn2_total']}",
        f"⛔ Ограничений: {stats['disabled_total']}",
        f"🔄 Восстановлений: {stats['enabled_total']}",
    ]
    top = stats.get("top_cases") or []
    if top:
        lines += ["", "🔝 Топ кейсов по сигналам:"]
        for item in top:
            lines.append(f"• {item['name']} — {item['cnt']}")
    return "\n".join(lines)


def analytics_tariffs_text(stats: dict) -> str:
    lines = [
        "💼 Аналитика тарифов",
        "",
        f"📦 Активных подписок: {stats['active_subscriptions_total']}",
        f"🛒 Успешных покупок: {stats['successful_payments_total']}",
        "",
        "🔝 Тарифы:",
    ]
    rows = stats.get("tariff_rows") or []
    if not rows:
        lines.append("• Пока нет данных")
    else:
        for row in rows[:15]:
            lines.append(
                f"• {row['plan_key']}: активных {row['active_subscriptions']}, покупок {row['payments_success']}, ⭐ {row['stars_total']}"
            )
    return "\n".join(lines)


def analytics_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Общая", callback_data="analytics:overview")],
            [InlineKeyboardButton(text="💳 Платежи", callback_data="analytics:payments")],
            [InlineKeyboardButton(text="🎫 Поддержка", callback_data="analytics:support")],
            [InlineKeyboardButton(text="🛡 Antiabuse", callback_data="analytics:antiabuse")],
            [InlineKeyboardButton(text="💼 Тарифы", callback_data="analytics:tariffs")],
            [InlineKeyboardButton(text="← К 🛠 Админ", callback_data="admin:panel")],
        ]
    )


def analytics_subpage_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="← К 📊 Аналитике", callback_data="analytics:panel")],
            [InlineKeyboardButton(text="← К 🛠 Админ", callback_data="admin:panel")],
        ]
    )


async def analytics_overview_stats() -> dict:
    now_ts = datetime.now().timestamp()
    day_ago = now_ts - 86400
    week_ago = now_ts - 86400 * 7

    users = set()
    users_with_subs = set()
    async with aiosqlite.connect(PAYMENTS_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT DISTINCT user_id FROM payments") as cur:
            rows = await cur.fetchall()
            users.update(int(r["user_id"]) for r in rows if r["user_id"] is not None)
        async with conn.execute("SELECT COUNT(DISTINCT user_id) AS c FROM payments WHERE created_at>=?", (day_ago,)) as cur:
            row24 = await cur.fetchone()
        async with conn.execute("SELECT COUNT(DISTINCT user_id) AS c FROM payments WHERE created_at>=?", (week_ago,)) as cur:
            row7 = await cur.fetchone()

    try:
        clients = await get_all_clients()
        for client in clients:
            desc = str(client.get("desc") or "")
            tgid = parse_tgid_from_desc(desc)
            if tgid:
                users_with_subs.add(int(tgid))
                users.add(int(tgid))
    except Exception:
        pass

    async with aiosqlite.connect(SUPPORT_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT DISTINCT user_id FROM support_tickets") as cur:
            rows = await cur.fetchall()
            users.update(int(r["user_id"]) for r in rows if r["user_id"] is not None)

    return {
        "users_total": len(users),
        "users_with_subscriptions": len(users_with_subs),
        "new_users_24h": int(row24["c"] or 0) if row24 else 0,
        "new_users_7d": int(row7["c"] or 0) if row7 else 0,
    }


async def analytics_payments_stats() -> dict:
    now_ts = datetime.now().timestamp()
    day_ago = now_ts - 86400
    week_ago = now_ts - 86400 * 7
    month_ago = now_ts - 86400 * 30
    async with aiosqlite.connect(PAYMENTS_DB) as conn:
        conn.row_factory = aiosqlite.Row

        async def one(query: str, params=()):
            async with conn.execute(query, params) as cur:
                return await cur.fetchone()

        row1 = await one("SELECT COUNT(*) AS c, COALESCE(SUM(amount),0) AS s FROM payments WHERE status='success' AND created_at>=?", (day_ago,))
        row7 = await one("SELECT COUNT(*) AS c, COALESCE(SUM(amount),0) AS s FROM payments WHERE status='success' AND created_at>=?", (week_ago,))
        row30 = await one("SELECT COUNT(*) AS c, COALESCE(SUM(amount),0) AS s FROM payments WHERE status='success' AND created_at>=?", (month_ago,))
        row_pending = await one("SELECT COUNT(*) AS c FROM payments WHERE status='pending'")
        row_failed = await one("SELECT COUNT(*) AS c FROM payments WHERE status='failed'")
    return {
        "success_24h": int(row1["c"] or 0),
        "success_7d": int(row7["c"] or 0),
        "success_30d": int(row30["c"] or 0),
        "stars_24h": int(row1["s"] or 0),
        "stars_7d": int(row7["s"] or 0),
        "stars_30d": int(row30["s"] or 0),
        "pending_total": int(row_pending["c"] or 0),
        "failed_total": int(row_failed["c"] or 0),
    }


async def analytics_support_stats() -> dict:
    now_ts = datetime.now().timestamp()
    day_ago = now_ts - 86400
    week_ago = now_ts - 86400 * 7
    async with aiosqlite.connect(SUPPORT_DB) as conn:
        conn.row_factory = aiosqlite.Row

        async def c(query: str, params=()):
            async with conn.execute(query, params) as cur:
                row = await cur.fetchone()
                return int(row["c"] or 0) if row else 0

        open_total = await c("SELECT COUNT(*) AS c FROM support_tickets WHERE status!='closed'")
        waiting_admin = await c("SELECT COUNT(*) AS c FROM support_tickets WHERE status!='closed' AND COALESCE(waiting_for,'admin')='admin'")
        waiting_user = await c("SELECT COUNT(*) AS c FROM support_tickets WHERE status!='closed' AND COALESCE(waiting_for,'admin')='user'")
        closed_total = await c("SELECT COUNT(*) AS c FROM support_tickets WHERE status='closed'")
        created_24h = await c("SELECT COUNT(*) AS c FROM support_tickets WHERE created_at>=?", (day_ago,))
        created_7d = await c("SELECT COUNT(*) AS c FROM support_tickets WHERE created_at>=?", (week_ago,))
    return {
        "open_total": open_total,
        "waiting_admin": waiting_admin,
        "waiting_user": waiting_user,
        "closed_total": closed_total,
        "created_24h": created_24h,
        "created_7d": created_7d,
    }


async def analytics_antiabuse_stats() -> dict:
    async with aiosqlite.connect(ANTIABUSE_DB) as conn:
        conn.row_factory = aiosqlite.Row

        async def c(query: str, params=()):
            async with conn.execute(query, params) as cur:
                row = await cur.fetchone()
                return int(row["c"] or 0) if row else 0

        warn1 = await c("SELECT COUNT(*) AS c FROM antiabuse_notifications WHERE kind='warn1'")
        warn2 = await c("SELECT COUNT(*) AS c FROM antiabuse_notifications WHERE kind='warn2'")
        disabled = await c("SELECT COUNT(*) AS c FROM antiabuse_notifications WHERE kind='disabled'")
        enabled = await c("SELECT COUNT(*) AS c FROM antiabuse_notifications WHERE kind='enabled'")
        top = []
        try:
            async with conn.execute("SELECT name, COUNT(*) AS cnt FROM antiabuse_notifications GROUP BY name ORDER BY cnt DESC, name ASC LIMIT 10") as cur:
                rows = await cur.fetchall()
                top = [{"name": str(r["name"]), "cnt": int(r["cnt"] or 0)} for r in rows]
        except Exception:
            top = []

    return {
        "warn1_total": warn1,
        "warn2_total": warn2,
        "disabled_total": disabled,
        "enabled_total": enabled,
        "top_cases": top,
    }


async def analytics_tariffs_stats() -> dict:
    clients = []
    try:
        clients = await get_all_clients()
    except Exception:
        clients = []

    active_by_plan = {}
    for client in clients:
        plan_key = parse_plan_key_from_desc(str(client.get("desc") or "")) or "unknown"
        if int(client.get("expiry") or 0) > datetime.now().timestamp():
            active_by_plan[plan_key] = active_by_plan.get(plan_key, 0) + 1

    payments_by_plan = {}
    async with aiosqlite.connect(PAYMENTS_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """
            SELECT COALESCE(tarif_key,'unknown') AS plan_key,
                   COUNT(*) AS c,
                   COALESCE(SUM(amount),0) AS stars
            FROM payments
            WHERE status='success'
            GROUP BY COALESCE(tarif_key,'unknown')
            ORDER BY c DESC
            """
        ) as cur:
            rows = await cur.fetchall()
            for row in rows:
                payments_by_plan[str(row["plan_key"])] = {
                    "payments_success": int(row["c"] or 0),
                    "stars_total": int(row["stars"] or 0),
                }

    keys = set(active_by_plan) | set(payments_by_plan)
    rows = []
    for key in keys:
        rows.append({
            "plan_key": key,
            "active_subscriptions": int(active_by_plan.get(key, 0)),
            "payments_success": int(payments_by_plan.get(key, {}).get("payments_success", 0)),
            "stars_total": int(payments_by_plan.get(key, {}).get("stars_total", 0)),
        })
    rows.sort(key=lambda x: (x["payments_success"], x["active_subscriptions"], x["stars_total"]), reverse=True)

    return {
        "active_subscriptions_total": sum(active_by_plan.values()),
        "successful_payments_total": sum(v["payments_success"] for v in payments_by_plan.values()),
        "tariff_rows": rows,
    }


# =========================
# FSM + HELPERS
# =========================

class SupportStates(StatesGroup):
    waiting_new_ticket = State()
    waiting_user_reply = State()
    waiting_admin_reply = State()
    waiting_admin_note = State()


class AdminStates(StatesGroup):
    waiting_broadcast_text = State()
    waiting_broadcast_button = State()
    waiting_ticket_search = State()
    waiting_payment_search = State()
    waiting_tariff_edit = State()
    waiting_antiabuse_note = State()
    waiting_antiabuse_limit = State()
    waiting_antiabuse_disable = State()
    waiting_direct_message_target = State()
    waiting_direct_message_text = State()
    waiting_tariff_create = State()
    waiting_tgproxy_create = State()
    waiting_tgproxy_edit = State()


router = Router()


async def send_long_message(message: types.Message, text: str, reply_markup=None) -> None:
    chunks = [text[i : i + 3500] for i in range(0, len(text), 3500)]
    if not chunks:
        chunks = [""]
    for i, chunk in enumerate(chunks):
        if i == len(chunks) - 1 and reply_markup is not None:
            await message.answer(chunk, reply_markup=reply_markup)
        else:
            await message.answer(chunk)


async def safe_edit_text(message: types.Message, text: str, reply_markup=None) -> None:
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        raise


def tariff_title_or_key(tarif_key: str) -> str:
    tariff = get_tariff(tarif_key)
    return tariff.title if tariff else tarif_key


async def send_stars_invoice_for_payment(bot, chat_id: int, payment) -> None:
    tariff = get_tariff(str(payment["tarif_key"]))
    if not tariff:
        raise RuntimeError("Тариф не найден для оплаты.")

    label = tariff.title if str(payment["kind"]) == "buy" else f"Продление {tariff.title}"
    title = f"Подписка · {tariff.title}" if str(payment["kind"]) == "buy" else f"Продление · {tariff.title}"
    payload = f"stars:{payment['payment_uid']}"

    await bot.send_invoice(
        chat_id=chat_id,
        title=title,
        description="Оплата VPN-подписки через Telegram Stars",
        payload=payload,
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=label, amount=int(payment["amount"] or 0))],
    )


async def apply_payment_without_payment(bot: Bot, payment, admin_id: int) -> str:
    tariff = get_tariff(str(payment["tarif_key"]))
    if not tariff:
        raise RuntimeError("Тариф не найден для применения.")

    kind = str(payment["kind"] or "buy")
    user_id = int(payment["user_id"] or 0)
    username = str(payment["username"] or "") or None

    if kind == "buy":
        sub_link, clash_link, client_name = await create_client(user_id=user_id, username=username, tariff=tariff)
        result_payload = {
            "kind": "buy",
            "client_name": client_name,
            "sub_link": sub_link,
            "clash_link": clash_link,
        }
        await mark_payment_success_manual(int(payment["id"]), int(admin_id), note="without_payment buy", result_payload=result_payload)
        try:
            await bot.send_message(user_id, payment_success_text_from_payload(kind, tariff, result_payload))
            await mark_payment_notified(int(payment["id"]), note="manual_buy_notified")
        except Exception as exc:
            logging.warning("Failed to notify user about manual buy payment apply: %s", exc)
        return "✅ Подписка выдана без оплаты."

    if kind == "renew":
        client_id = int(payment["client_id"] or 0)
        if client_id <= 0:
            raise RuntimeError("У платежа продления нет client_id")
        client = await get_client_by_id(client_id)
        if not client:
            raise RuntimeError("Подписка для продления не найдена в S-UI")

        owner_items = await get_user_subscriptions(user_id)
        owner_item = next((x for x in owner_items if int(x["id"]) == client_id), None)
        if not owner_item:
            raise RuntimeError("Эта подписка не принадлежит пользователю платежа")

        await renew_client_in_sui(client, tariff)
        result_payload = {
            "kind": "renew",
            "client_id": client_id,
            "name": owner_item["name"],
            "sub_link": owner_item["sub_link"],
            "clash_link": owner_item["clash_link"],
        }
        await mark_payment_success_manual(int(payment["id"]), int(admin_id), note="without_payment renew", result_payload=result_payload)
        try:
            await bot.send_message(user_id, payment_success_text_from_payload(kind, tariff, result_payload))
            await mark_payment_notified(int(payment["id"]), note="manual_renew_notified")
        except Exception as exc:
            logging.warning("Failed to notify user about manual renew payment apply: %s", exc)
        return "✅ Продление применено без оплаты."

    raise RuntimeError(f"Неизвестный тип платежа: {kind}")


async def notify_admins_about_ticket(bot, ticket_id: int, title: str) -> None:
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"Открыть тикет #{ticket_id}", callback_data=f"support:admin:view:{ticket_id}")]
        ]
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(int(admin_id), title, reply_markup=kb)
        except Exception:
            pass


async def notify_user_about_admin_reply(bot, user_id: int, ticket_id: int) -> None:
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"Открыть тикет #{ticket_id}", callback_data=f"support:view:{ticket_id}")]
        ]
    )
    try:
        await bot.send_message(
            int(user_id),
            f"📩 Получен ответ поддержки по тикету #{ticket_id}",
            reply_markup=kb,
        )
    except Exception:
        pass



def build_broadcast_reply_markup(button_text: str | None, button_url: str | None):
    if button_text and button_url:
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=button_text, url=button_url)]]
        )
    return None


async def send_broadcast_preview(message: types.Message, data: dict) -> None:
    reply_markup = build_broadcast_reply_markup(data.get("button_text"), data.get("button_url"))
    kind = data.get("kind")
    if kind == "photo":
        await message.answer_photo(
            photo=data["photo_file_id"],
            caption=data.get("caption") or " ",
            reply_markup=reply_markup,
        )
    else:
        await message.answer(
            data.get("text") or "—",
            reply_markup=reply_markup,
        )

    await message.answer(
        broadcast_preview_text(
            has_media=(kind == "photo"),
            has_button=bool(data.get("button_text") and data.get("button_url")),
        ),
        reply_markup=broadcast_preview_keyboard(
            has_button=bool(data.get("button_text") and data.get("button_url"))
        ),
    )


async def execute_broadcast(bot: Bot, data: dict) -> tuple[int, int, int]:
    user_ids = await collect_broadcast_user_ids()
    sent = failed = skipped = 0
    reply_markup = build_broadcast_reply_markup(data.get("button_text"), data.get("button_url"))

    for uid in user_ids:
        if uid in ADMIN_IDS:
            skipped += 1
            continue
        try:
            if data.get("kind") == "photo":
                await bot.send_photo(
                    uid,
                    photo=data["photo_file_id"],
                    caption=data.get("caption") or " ",
                    reply_markup=reply_markup,
                )
            else:
                await bot.send_message(
                    uid,
                    data.get("text") or "—",
                    reply_markup=reply_markup,
                )
            sent += 1
            await asyncio.sleep(0.05)
        except TelegramRetryAfter as exc:
            await asyncio.sleep(float(getattr(exc, "retry_after", 1)) + 0.5)
            failed += 1
        except Exception:
            failed += 1
            await asyncio.sleep(0.05)

    return sent, failed, skipped


async def run_reminder_check(bot: Bot) -> dict:
    stats = {
        "checked_count": 0,
        "sent_3d": 0,
        "sent_1d": 0,
        "sent_expired": 0,
        "failed_count": 0,
    }

    try:
        clients = await get_all_clients()
    except Exception:
        return stats

    now_ts = int(datetime.now().timestamp())
    grouped: dict[int, list[dict]] = {}
    for client in clients:
        desc = str(client.get("desc") or "")
        tgid = parse_tgid_from_desc(desc)
        tg_id_raw = str(client.get("tgId") or "")
        if tgid is None and tg_id_raw.isdigit():
            tgid = int(tg_id_raw)
        if tgid is None:
            continue
        grouped.setdefault(int(tgid), []).append(client)

    for user_id, items in grouped.items():
        for client in items:
            stats["checked_count"] += 1
            expiry = int(client.get("expiry") or 0)
            client_id = int(client.get("id") or 0)
            if client_id <= 0:
                continue
            days_left = max(0, int((expiry - now_ts) / 86400)) if expiry > 0 else 0

            name = str(client.get("name") or "unknown")
            desc = str(client.get("desc") or "")
            item = {
                "id": client_id,
                "name": name,
                "plan_key": parse_plan_key_from_desc(desc),
            }

            if days_left in (3, 1):
                reminder_type = f"expiring_{days_left}"
                if not await reminder_already_sent(user_id, client_id, reminder_type):
                    try:
                        await bot.send_message(int(user_id), reminder_expiring_text(item, days_left))
                        await mark_reminder_sent(user_id, client_id, reminder_type)
                        if days_left == 3:
                            stats["sent_3d"] += 1
                        else:
                            stats["sent_1d"] += 1
                    except Exception:
                        stats["failed_count"] += 1

            if expiry > 0 and expiry <= now_ts:
                reminder_type = "expired"
                if not await reminder_already_sent(user_id, client_id, reminder_type):
                    try:
                        await bot.send_message(int(user_id), reminder_expired_text(item))
                        await mark_reminder_sent(user_id, client_id, reminder_type)
                        stats["sent_expired"] += 1
                    except Exception:
                        stats["failed_count"] += 1

    await log_reminder_run(**stats)
    return stats


async def build_reminders_panel_text() -> str:
    expiring_3d = 0
    expiring_1d = 0
    expired_now = 0

    try:
        clients = await get_all_clients()
        now_ts = int(datetime.now().timestamp())
        for client in clients:
            expiry = int(client.get("expiry") or 0)
            if expiry <= 0:
                continue
            if expiry <= now_ts:
                expired_now += 1
                continue
            days_left = max(0, int((expiry - now_ts) / 86400))
            if days_left <= 3:
                expiring_3d += 1
            if days_left <= 1:
                expiring_1d += 1
    except Exception:
        pass

    marks = await count_reminder_marks()
    last_run = await get_last_reminder_run()
    if last_run:
        last_run_text = payment_dt_text(last_run["run_at"])
        last_run_stats = (
            f"Проверено: {int(last_run['checked_count'] or 0)} | "
            f"3d: {int(last_run['sent_3d'] or 0)} | "
            f"1d: {int(last_run['sent_1d'] or 0)} | "
            f"expired: {int(last_run['sent_expired'] or 0)} | "
            f"errors: {int(last_run['failed_count'] or 0)}"
        )
    else:
        last_run_text = "—"
        last_run_stats = "Статистика запуска пока отсутствует."

    return reminders_panel_text(
        expiring_3d=expiring_3d,
        expiring_1d=expiring_1d,
        expired_now=expired_now,
        marks_3d=marks["expiring_3"],
        marks_1d=marks["expiring_1"],
        marks_expired=marks["expired"],
        last_run_text=last_run_text,
        last_run_stats=last_run_stats,
    )


async def reminder_loop(bot: Bot) -> None:
    while True:
        try:
            await run_reminder_check(bot)
        except Exception as exc:
            logging.exception("reminder_loop failed: %s", exc)
        await asyncio.sleep(3600)


def main_menu(user_id: int) -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(text="🛒 Купить"), KeyboardButton(text="🔑 Мои подписки")],
        [KeyboardButton(text="🔄 Продлить"), KeyboardButton(text="💳 Мои платежи")],
        [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="🎁 Тестовый доступ")],
        [KeyboardButton(text="💎 Telegram proxy"), KeyboardButton(text="🆘 Поддержка")],
        [KeyboardButton(text="❓ FAQ")],
    ]
    if is_admin(user_id):
        keyboard.append([KeyboardButton(text="🛠 Админ")])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True, input_field_placeholder="Выберите действие")

def admin_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")],
            [InlineKeyboardButton(text="💳 Платежи", callback_data="admin:payments")],
            [InlineKeyboardButton(text="📊 Аналитика", callback_data="analytics:panel")],
            [InlineKeyboardButton(text="⏰ Напоминания", callback_data="reminders:panel")],
            [InlineKeyboardButton(text="🎫 Тикеты поддержки", callback_data="support:admin:list")],
            [InlineKeyboardButton(text="📣 Рассылка", callback_data="admin:broadcast")],
            [InlineKeyboardButton(text="📩 Сообщение пользователю", callback_data="admin:direct_message")],
            [InlineKeyboardButton(text="🛡 Antiabuse отчёт", callback_data="antiabuse:report")],
            [InlineKeyboardButton(text="💼 Тарифы", callback_data="admin:tariffs")],
            [InlineKeyboardButton(text="💎 Telegram proxy", callback_data="admin:tgproxy")],
            [InlineKeyboardButton(text="✖️ Закрыть", callback_data="menu:back")],
        ]
    )

def admin_subpage_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← К 🛠 Админ", callback_data="admin:panel")]])

def broadcast_prompt_text() -> str:
    return (
        "📣 Рассылка\n\n"
        "Отправьте следующим сообщением:\n"
        "• обычный текст\n"
        "или\n"
        "• фото с подписью\n\n"
        "После этого будет предпросмотр, и вы сможете:\n"
        "• добавить кнопку-ссылку\n"
        "• отправить\n"
        "• отменить"
    )

def broadcast_preview_text(has_media: bool, has_button: bool) -> str:
    media_text = "фото + подпись" if has_media else "текст"
    button_text = "есть" if has_button else "нет"
    return (
        "📣 Предпросмотр рассылки\n\n"
        f"Тип: {media_text}\n"
        f"Кнопка: {button_text}\n\n"
        "Проверьте сообщение выше и выберите действие ниже."
    )

def broadcast_result_text(sent: int, failed: int, skipped: int) -> str:
    return (
        "📣 Рассылка завершена\n\n"
        f"Отправлено: {sent}\n"
        f"Ошибок: {failed}\n"
        f"Пропущено: {skipped}"
    )

async def collect_broadcast_user_ids() -> list[int]:
    ids = set()
    async with aiosqlite.connect(PAYMENTS_DB) as conn:
        async with conn.execute("SELECT DISTINCT user_id FROM payments") as cur:
            for row in await cur.fetchall():
                uid = int(row[0] or 0)
                if uid > 0:
                    ids.add(uid)
    async with aiosqlite.connect(SUPPORT_DB) as conn:
        async with conn.execute("SELECT DISTINCT user_id FROM support_tickets") as cur:
            for row in await cur.fetchall():
                uid = int(row[0] or 0)
                if uid > 0:
                    ids.add(uid)
    return sorted(ids)

def tariffs_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for tariff in list_tariffs(user_id):
        rows.append([InlineKeyboardButton(text=f"{tariff.title} · {human_traffic(tariff.traffic_gb)} · {tariff.stars_price} ⭐", callback_data=f"buy:preview:{tariff.key}")])
    rows.append([InlineKeyboardButton(text="✖️ Закрыть", callback_data="menu:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def tariff_preview_keyboard(tariff: Tariff) -> InlineKeyboardMarkup:
    stars_bot_url = normalize_stars_purchase_url(tariff.stars_purchase_url) or "https://t.me/stars_mint_bot?start=ref_v4u8c1v47pfs"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⭐ Оплатить Telegram Stars", callback_data=f"buy:invoice:{tariff.key}")],
            [InlineKeyboardButton(text="🛒 Купить Telegram Stars", url="https://t.me/PremiumBot")],
            [InlineKeyboardButton(text="🛒 Купить Telegram Stars (Bot)", url=stars_bot_url)],
            [InlineKeyboardButton(text="⭐ Как купить Telegram Stars", callback_data=f"faq:stars:{tariff.key}")],
            [InlineKeyboardButton(text="← К тарифам", callback_data="buy:menu")],
        ]
    )

def faq_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📱 Подключить устройство", callback_data="faq:connect_menu")],
            [InlineKeyboardButton(text="⭐ Как купить Stars", callback_data="faq:stars")],
            [InlineKeyboardButton(text="🛡 Подключения и лимиты", callback_data="faq:connections")],
            [InlineKeyboardButton(text="⛔ Почему доступ ограничен", callback_data="faq:restricted")],
            [InlineKeyboardButton(text="📡 OpenWRT / Роутеры", callback_data="faq:router")],
            [InlineKeyboardButton(text="❗ Частые проблемы", callback_data="faq:problems")],
            [InlineKeyboardButton(text="🚇 Описание протоколов и сервера", callback_data="faq:proxy")],
            [InlineKeyboardButton(text="🔐 О VPN", callback_data="faq:about")],
            [InlineKeyboardButton(text="✖️ Закрыть", callback_data="menu:back")],
        ]
    )

def faq_connect_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📱 iPhone / iPad", callback_data="faq:device:ios"),
                InlineKeyboardButton(text="🤖 Android", callback_data="faq:device:android"),
            ],
            [
                InlineKeyboardButton(text="📺 Android TV", callback_data="faq:device:androidtv"),
                InlineKeyboardButton(text="🍎 macOS", callback_data="faq:device:macos"),
            ],
            [InlineKeyboardButton(text="💻 Windows", callback_data="faq:device:windows")],
            [InlineKeyboardButton(text="← К FAQ", callback_data="faq:main")],
        ]
    )

def faq_device_links_keyboard(platform: str) -> InlineKeyboardMarkup:
    if platform == "ios":
        rows = [
            [
                InlineKeyboardButton(text="OnlyNet Rus", url="https://apps.apple.com/ru/app/onlynet-vpn-client/id6502987522"),
                InlineKeyboardButton(text="Happ Rus", url="https://apps.apple.com/ru/app/happ-proxy-utility-plus/id6746188973"),
            ],
            [
                InlineKeyboardButton(text="Clash Mi", url="https://apps.apple.com/us/app/clash-mi/id6744321968"),
                InlineKeyboardButton(text="Hiddify Global", url="https://apps.apple.com/us/app/hiddify-proxy-vpn/id6596777532"),
            ],
            [InlineKeyboardButton(text="Streisand Global", url="https://apps.apple.com/us/app/streisand/id6450534064")],
        ]
    elif platform == "android":
        rows = [
            [
                InlineKeyboardButton(text="Happ APK", url="https://github.com/Happ-proxy/happ-android/releases/latest/download/Happ.apk"),
                InlineKeyboardButton(text="Hiddify APK", url="https://github.com/hiddify/hiddify-app/releases/download/v4.1.1/Hiddify-Android-universal.apk"),
            ],
            [InlineKeyboardButton(text="Clash Mi APK", url="https://github.com/KaringX/clashmi/releases/download/v1.0.20.607/clashmi_1.0.20.607_android_arm.apk")],
        ]
    elif platform == "windows":
        rows = [
            [
                InlineKeyboardButton(text="Happ", url="https://github.com/Happ-proxy/happ-desktop/releases/latest/download/setup-Happ.x64.exe"),
                InlineKeyboardButton(text="Hiddify", url="https://github.com/hiddify/hiddify-app/releases/download/v4.1.1/Hiddify-Windows-Setup-x64.exe"),
            ],
            [InlineKeyboardButton(text="Clash Mi", url="https://github.com/KaringX/clashmi/releases/download/v1.0.20.607/clashmi_1.0.20.607_windows_x64.exe")],
        ]
    elif platform == "macos":
        rows = [
            [
                InlineKeyboardButton(text="Happ dmg", url="https://github.com/Happ-proxy/happ-desktop/releases/latest/download/Happ.macOS.universal.dmg"),
                InlineKeyboardButton(text="Hiddify dmg", url="https://github.com/hiddify/hiddify-app/releases/download/v4.1.1/Hiddify-MacOS.dmg"),
            ],
            [
                InlineKeyboardButton(text="Clash Mi dmg", url="https://github.com/KaringX/clashmi/releases/download/v1.0.20.607/clashmi_1.0.20.607_macos_universal.dmg"),
                InlineKeyboardButton(text="OnlyNet Rus", url="https://apps.apple.com/ru/app/onlynet-vpn-client/id6502987522"),
            ],
            [InlineKeyboardButton(text="Streisand Global", url="https://apps.apple.com/us/app/streisand/id6450534064")],
        ]
    elif platform == "androidtv":
        rows = [
            [
                InlineKeyboardButton(text="Happ APK", url="https://github.com/Happ-proxy/happ-android/releases/latest/download/Happ.apk"),
                InlineKeyboardButton(text="VPN4TV", url="https://vpn4tv.com/"),
            ],
        ]
    else:
        rows = []

    rows.append([InlineKeyboardButton(text="← К устройствам", callback_data="faq:connect_menu")])
    rows.append([InlineKeyboardButton(text="← К FAQ", callback_data="faq:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def user_payments_keyboard(rows) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for row in rows[:15]:
        label = f"#{row['id']} · {payment_kind_label(str(row['kind']))} · {payment_amount_text(int(row['amount'] or 0))} · {payment_status_label(str(row['status']))}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"pay:view:{row['id']}")])
    buttons.append([InlineKeyboardButton(text="✖️ Закрыть", callback_data="menu:back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def user_payment_detail_keyboard(row) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if str(row["status"]) == "pending":
        rows.append([InlineKeyboardButton(text="⭐ Продолжить оплату", callback_data=f"pay:open:{row['id']}")])
        rows.append([InlineKeyboardButton(text="❌ Отменить pending-счёт", callback_data=f"pay:cancel:{row['id']}")])
    if str(row["status"]) == "failed":
        rows.append([InlineKeyboardButton(text="🔄 Повторить оплату", callback_data=f"pay:retry:{row['id']}")])
    rows.append([InlineKeyboardButton(text="← К платежам", callback_data="pay:list")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def reminders_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Проверить сейчас", callback_data="reminders:run_now")],
            [InlineKeyboardButton(text="✉️ Шаблоны", callback_data="reminders:templates")],
            [InlineKeyboardButton(text="← К 🛠 Админ", callback_data="admin:panel")],
        ]
    )

def reminders_templates_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="← К ⏰ Напоминания", callback_data="reminders:panel")],
            [InlineKeyboardButton(text="← К 🛠 Админ", callback_data="admin:panel")],
        ]
    )

def renew_list_keyboard(items: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for item in items:
        rows.append([InlineKeyboardButton(text=f"{item['name']} · {item['days_left']} дн.", callback_data=f"renew:pick:{item['id']}")])
    rows.append([InlineKeyboardButton(text="✖️ Закрыть", callback_data="menu:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def renew_preview_keyboard(client_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⭐ Оплатить продление", callback_data=f"renew:invoice:{client_id}")],
            [InlineKeyboardButton(text="← К списку", callback_data="renew:menu")],
        ]
    )

def support_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🆕 Открыть обращение", callback_data="support:new")],
            [InlineKeyboardButton(text="📂 Мои обращения", callback_data="support:list")],
            [InlineKeyboardButton(text="✖️ Закрыть", callback_data="menu:back")],
        ]
    )

def support_user_tickets_keyboard(rows) -> InlineKeyboardMarkup:
    buttons = []
    for row in rows[:15]:
        buttons.append([InlineKeyboardButton(text=f"#{row['id']} · {ticket_status_label(str(row['status']))} · {row['subject'] or 'Без темы'}", callback_data=f"support:view:{row['id']}")])
    buttons.append([InlineKeyboardButton(text="← К поддержке", callback_data="support:menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def support_ticket_keyboard(ticket, back_callback: str = "support:list") -> InlineKeyboardMarkup:
    rows = []
    if str(ticket["status"]) != "closed":
        rows.append([InlineKeyboardButton(text="✍️ Ответить", callback_data=f"support:reply:{ticket['id']}")])
        rows.append([InlineKeyboardButton(text="✅ Закрыть", callback_data=f"support:close:{ticket['id']}")])
    else:
        rows.append([InlineKeyboardButton(text="♻️ Переоткрыть", callback_data=f"support:reopen:{ticket['id']}")])
    rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data=f"support:refresh:{ticket['id']}")])
    back_text = "← К 🛠 Админ" if back_callback == "support:admin:list" else "← К обращениям"
    rows.append([InlineKeyboardButton(text=back_text, callback_data=back_callback)])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def support_admin_tickets_keyboard(rows, status_filter: str = "all", page: int = 1, per_page: int = 10) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(text="Все", callback_data="support:admin:list:all:1"),
            InlineKeyboardButton(text="Открытые", callback_data="support:admin:list:open:1"),
            InlineKeyboardButton(text="Отвечённые", callback_data="support:admin:list:answered:1"),
            InlineKeyboardButton(text="Закрытые", callback_data="support:admin:list:closed:1"),
        ],
        [InlineKeyboardButton(text="🔎 Поиск", callback_data="support:admin:search")],
    ]

    total = len(rows)
    page = max(1, int(page or 1))
    pages = max(1, (total + per_page - 1) // per_page)
    if page > pages:
        page = pages

    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    for row in rows[start_idx:end_idx]:
        title = row["username"] or row["user_id"]
        waiting = str((row.get("waiting_for") or ("closed" if str(row["status"]) == "closed" else "user" if str(row["status"]) == "answered" else "admin")))
        label = ticket_waiting_label(waiting, str(row["status"]))
        buttons.append([InlineKeyboardButton(text=f"#{row['id']} · {label} · {title}", callback_data=f"support:admin:view:{row['id']}:{status_filter}:{page}")])

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"support:admin:list:{status_filter}:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page}/{pages}", callback_data="noop"))
    if page < pages:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"support:admin:list:{status_filter}:{page+1}"))
    buttons.append(nav)

    buttons.append([InlineKeyboardButton(text="← К 🛠 Админ", callback_data="admin:panel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def admin_ticket_keyboard(ticket, status_filter: str = "all", page: int = 1) -> InlineKeyboardMarkup:
    tid = ticket["id"]
    suffix = f":{status_filter}:{page}"
    rows = []
    if str(ticket["status"]) != "closed":
        rows.append([InlineKeyboardButton(text="✍️ Ответить", callback_data=f"support:reply:{tid}{suffix}")])
        rows.append([
            InlineKeyboardButton(text="💬 Перезапуск", callback_data=f"support:quick:restart:{tid}{suffix}"),
            InlineKeyboardButton(text="💬 Интернет", callback_data=f"support:quick:internet:{tid}{suffix}"),
        ])
        rows.append([
            InlineKeyboardButton(text="💬 Переустановить", callback_data=f"support:quick:reinstall:{tid}{suffix}"),
            InlineKeyboardButton(text="💬 Сменить сервер", callback_data=f"support:quick:server:{tid}{suffix}"),
        ])
        rows.append([InlineKeyboardButton(text="📝 Добавить заметку", callback_data=f"support:note:{tid}{suffix}")])
        rows.append([InlineKeyboardButton(text="✅ Закрыть", callback_data=f"support:close:{tid}{suffix}")])
    else:
        rows.append([InlineKeyboardButton(text="♻️ Переоткрыть", callback_data=f"support:reopen:{tid}{suffix}")])
        rows.append([InlineKeyboardButton(text="📝 Добавить заметку", callback_data=f"support:note:{tid}{suffix}")])
    rows.append([InlineKeyboardButton(text="👤 Профиль пользователя", callback_data=f"support:admin:user:{ticket['user_id']}:{tid}:{status_filter}:{page}")])
    rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data=f"support:refresh:{tid}:{status_filter}:{page}")])
    rows.append([InlineKeyboardButton(text="← К тикетам", callback_data=f"support:admin:list:{status_filter}:{page}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def search_admin_payments(query: str, limit: int = 50):
    q = (query or "").strip()
    if not q:
        return []

    normalized = q.replace("№", "#").replace(" ", "")
    async with aiosqlite.connect(PAYMENTS_DB) as conn:
        conn.row_factory = aiosqlite.Row
        if normalized.startswith("#"):
            raw_id = normalized[1:]
            if raw_id.isdigit():
                payment_id = int(raw_id)
                async with conn.execute("SELECT * FROM payments WHERE id=?", (payment_id,)) as cur:
                    row = await cur.fetchone()
                    return [row] if row else []

        if q.isdigit():
            user_id = int(q)
            async with conn.execute(
                """
                SELECT *
                FROM payments
                WHERE user_id=?
                ORDER BY id DESC
                LIMIT ?
                """,
                (user_id, int(limit)),
            ) as cur:
                return await cur.fetchall()

    return []

async def list_admin_payments_filtered(status_filter: str = "all", limit: int = 50):
    async with aiosqlite.connect(PAYMENTS_DB) as conn:
        conn.row_factory = aiosqlite.Row
        base_sql = """
            SELECT *
            FROM payments
            {where_clause}
            ORDER BY id DESC
            LIMIT ?
        """
        params = [int(limit)]
        where_clause = ""
        if status_filter in ("success", "pending", "failed"):
            where_clause = "WHERE status=?"
            params = [status_filter, int(limit)]
        sql = base_sql.format(where_clause=where_clause)
        async with conn.execute(sql, params) as cur:
            return await cur.fetchall()

def admin_payments_text(status_filter: str = "all") -> str:
    title = "💳 Платежи"
    if status_filter != "all":
        title += f"\n\nФильтр: {status_filter}"
    else:
        title += "\n\nПоследние платежи:"
    return title

def admin_payment_detail_text(row, tariff_title: str) -> str:
    line = "━━━━━━━━━━━━━━"
    username = f"@{row['username']}" if str(row['username'] or "") else "—"
    return (
        "💳 Платёж\n"
        f"{line}\n"
        f"ID: #{row['id']}\n"
        f"TGID: {row['user_id']}\n"
        f"Username: {username}\n"
        f"Тип: {payment_kind_label(str(row['kind']))}\n"
        f"Тариф: {tariff_title}\n"
        f"Сумма: {payment_amount_text(int(row['amount'] or 0))}\n"
        f"Статус: {payment_status_label(str(row['status']))}\n"
        f"Применение: {str(row['application_status'] or '—')}\n"
        f"Создан: {payment_dt_text(row['created_at'])}\n"
        f"Оплачен: {payment_dt_text(row['paid_at'])}\n"
        f"Применён: {payment_dt_text(row['applied_at'])}\n"
        f"Уведомлён: {payment_dt_text(row['notified_at'])}\n"
        f"Ошибка/закрыт: {payment_dt_text(row['failed_at'])}\n"
        f"Provider charge: {str(row['provider_charge_id'] or '—')}\n"
        f"Заметки: {str(row['notes'] or '—')}\n"
        f"{line}"
    )

def admin_payment_detail_keyboard(row=None) -> InlineKeyboardMarkup:
    rows = []
    if row is not None and str(row["status"] or "") == "pending":
        action_text = "✅ Выдать без оплаты" if str(row["kind"] or "") == "buy" else "✅ Продлить без оплаты"
        rows.append([InlineKeyboardButton(text=action_text, callback_data=f"admin:payments:approve:{int(row['id'])}")])
    rows.append([InlineKeyboardButton(text="← К платежам", callback_data="admin:payments")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_payment_search_prompt_text() -> str:
    return (
        "🔎 Поиск платежа\n\n"
        "Отправьте:\n"
        "• #123 — для поиска по payment ID\n"
        "• 123456789 — для поиска по TGID"
    )

def admin_payment_search_prompt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Отменить", callback_data="admin:payments")]]
    )

async def build_admin_stats_text() -> str:
    stats = await get_admin_stats()
    subscriptions_total = 0
    active_subscriptions = 0
    expiring_3d = 0
    expiring_1d = 0
    suspicious_tgids = 0

    try:
        clients = await get_all_clients()
        subscriptions_total = len(clients)
        now_ts = int(datetime.now().timestamp())
        groups = await antiabuse_group_by_tgid(clients)
        suspicious_tgids = len(groups)

        for client in clients:
            expiry = int(client.get("expiry") or 0)
            if expiry > now_ts:
                active_subscriptions += 1
                days_left = max(0, int((expiry - now_ts) / 86400))
                if days_left <= 3:
                    expiring_3d += 1
                if days_left <= 1:
                    expiring_1d += 1
    except Exception:
        pass

    return admin_stats_text(
        users_count=stats["users_count"],
        payments_total=stats["payments_total"],
        payments_success=stats["payments_success"],
        payments_pending=stats["payments_pending"],
        payments_failed=stats["payments_failed"],
        revenue_total=stats["revenue_total"],
        revenue_today=stats["revenue_today"],
        revenue_7d=stats["revenue_7d"],
        revenue_30d=stats["revenue_30d"],
        buys_total=stats["buys_total"],
        renews_total=stats["renews_total"],
        buys_today=stats["buys_today"],
        renews_today=stats["renews_today"],
        buys_7d=stats["buys_7d"],
        renews_7d=stats["renews_7d"],
        buys_30d=stats["buys_30d"],
        renews_30d=stats["renews_30d"],
        tickets_open=stats["tickets_open"],
        tickets_closed=stats["tickets_closed"],
        subscriptions_total=subscriptions_total,
        active_subscriptions=active_subscriptions,
        expiring_3d=expiring_3d,
        expiring_1d=expiring_1d,
        suspicious_tgids=suspicious_tgids,
        top_tariffs=stats["top_tariffs"],
    )

def broadcast_button_prompt_text() -> str:
    return (
        "🔗 Кнопка для рассылки\n\n"
        "Отправьте строку в формате:\n"
        "Текст кнопки | Ссылка"
        "Например:\n"
        "Открыть сайт | https://example.com"
    )

def admin_payments_keyboard(rows, status_filter: str = "all") -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(text="Все", callback_data="admin:payments"),
            InlineKeyboardButton(text="Успешные", callback_data="admin:payments:success"),
            InlineKeyboardButton(text="Ожидают", callback_data="admin:payments:pending"),
            InlineKeyboardButton(text="Ошибки", callback_data="admin:payments:failed"),
        ],
        [InlineKeyboardButton(text="🔎 Поиск", callback_data="admin:payments:search")],
    ]
    for row in rows[:20]:
        label = (
            f"#{row['id']} · {row['user_id']} · "
            f"{payment_kind_label(str(row['kind']))} · "
            f"{payment_amount_text(int(row['amount'] or 0))} · "
            f"{payment_status_label(str(row['status']))}"
        )
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"admin:payments:view:{row['id']}")])
    buttons.append([InlineKeyboardButton(text="← К 🛠 Админ", callback_data="admin:panel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def get_admin_stats() -> dict:
    now = datetime.now()
    start_today = datetime(now.year, now.month, now.day).timestamp()
    start_7d = (now - timedelta(days=7)).timestamp()
    start_30d = (now - timedelta(days=30)).timestamp()

    async with aiosqlite.connect(PAYMENTS_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success_count,
                SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending_count,
                SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed_count,
                COUNT(DISTINCT user_id) AS users_count,
                SUM(CASE WHEN status='success' THEN amount ELSE 0 END) AS revenue_total,
                SUM(CASE WHEN status='success' AND paid_at >= ? THEN amount ELSE 0 END) AS revenue_today,
                SUM(CASE WHEN status='success' AND paid_at >= ? THEN amount ELSE 0 END) AS revenue_7d,
                SUM(CASE WHEN status='success' AND paid_at >= ? THEN amount ELSE 0 END) AS revenue_30d,
                SUM(CASE WHEN kind='buy' THEN 1 ELSE 0 END) AS buys_total,
                SUM(CASE WHEN kind='renew' THEN 1 ELSE 0 END) AS renews_total,
                SUM(CASE WHEN kind='buy' AND created_at >= ? THEN 1 ELSE 0 END) AS buys_today,
                SUM(CASE WHEN kind='renew' AND created_at >= ? THEN 1 ELSE 0 END) AS renews_today,
                SUM(CASE WHEN kind='buy' AND created_at >= ? THEN 1 ELSE 0 END) AS buys_7d,
                SUM(CASE WHEN kind='renew' AND created_at >= ? THEN 1 ELSE 0 END) AS renews_7d,
                SUM(CASE WHEN kind='buy' AND created_at >= ? THEN 1 ELSE 0 END) AS buys_30d,
                SUM(CASE WHEN kind='renew' AND created_at >= ? THEN 1 ELSE 0 END) AS renews_30d
            FROM payments
            """,
            (start_today, start_7d, start_30d, start_today, start_today, start_7d, start_7d, start_30d, start_30d),
        ) as cur:
            summary = await cur.fetchone()
        async with conn.execute(
            """
            SELECT tarif_key, COUNT(*) AS cnt
            FROM payments
            WHERE status='success'
            GROUP BY tarif_key
            ORDER BY cnt DESC, tarif_key ASC
            LIMIT 5
            """
        ) as cur:
            top_tariffs_rows = await cur.fetchall()

    async with aiosqlite.connect(SUPPORT_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """
            SELECT
                SUM(CASE WHEN status IN ('open', 'answered') THEN 1 ELSE 0 END) AS open_count,
                SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) AS closed_count
            FROM support_tickets
            """
        ) as cur:
            ticket_summary = await cur.fetchone()

    return {
        "users_count": int(summary["users_count"] or 0),
        "payments_total": int(summary["total"] or 0),
        "payments_success": int(summary["success_count"] or 0),
        "payments_pending": int(summary["pending_count"] or 0),
        "payments_failed": int(summary["failed_count"] or 0),
        "revenue_total": int(summary["revenue_total"] or 0),
        "revenue_today": int(summary["revenue_today"] or 0),
        "revenue_7d": int(summary["revenue_7d"] or 0),
        "revenue_30d": int(summary["revenue_30d"] or 0),
        "buys_total": int(summary["buys_total"] or 0),
        "renews_total": int(summary["renews_total"] or 0),
        "buys_today": int(summary["buys_today"] or 0),
        "renews_today": int(summary["renews_today"] or 0),
        "buys_7d": int(summary["buys_7d"] or 0),
        "renews_7d": int(summary["renews_7d"] or 0),
        "buys_30d": int(summary["buys_30d"] or 0),
        "renews_30d": int(summary["renews_30d"] or 0),
        "tickets_open": int(ticket_summary["open_count"] or 0),
        "tickets_closed": int(ticket_summary["closed_count"] or 0),
        "top_tariffs": [(str(row["tarif_key"] or "—"), int(row["cnt"] or 0)) for row in top_tariffs_rows],
    }

async def antiabuse_group_by_tgid(clients: list[dict]) -> list[dict]:
    now_ts = int(datetime.now().timestamp())
    groups: dict[int, list[dict]] = {}

    for client in clients:
        desc = str(client.get("desc") or "")
        tgid = parse_tgid_from_desc(desc)
        tg_id_raw = str(client.get("tgId") or "")
        if tgid is None and tg_id_raw.isdigit():
            tgid = int(tg_id_raw)
        if tgid is None:
            continue
        groups.setdefault(int(tgid), []).append(client)

    async with aiosqlite.connect(PAYMENTS_DB) as conn:
        conn.row_factory = aiosqlite.Row
        result = []
        for tgid, items in groups.items():
            active_count = 0
            expired_count = 0
            names = []
            plan_keys = set()
            expiring_3d = 0

            for c in items:
                expiry = int(c.get("expiry") or 0)
                if expiry > now_ts:
                    active_count += 1
                    days_left = max(0, int((expiry - now_ts) / 86400))
                    if days_left <= 3:
                        expiring_3d += 1
                else:
                    expired_count += 1

                names.append(str(c.get("name") or "unknown"))
                desc = str(c.get("desc") or "")
                plan_key = parse_plan_key_from_desc(desc)
                if plan_key:
                    plan_keys.add(plan_key)

            suspicious_reasons = []
            if len(items) >= 3:
                suspicious_reasons.append("3+ клиентов")
            if active_count >= 2:
                suspicious_reasons.append("2+ активных")
            if expiring_3d >= 2:
                suspicious_reasons.append("несколько скоро истекают")

            async with conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success_count,
                    SUM(CASE WHEN kind='renew' THEN 1 ELSE 0 END) AS renew_count,
                    SUM(CASE WHEN kind='buy' THEN 1 ELSE 0 END) AS buy_count,
                    MAX(paid_at) AS last_paid_at
                FROM payments
                WHERE user_id=?
                """,
                (int(tgid),),
            ) as cur:
                row = await cur.fetchone()

            case = await get_antiabuse_case(tgid)
            admin_status = str(case["admin_status"]) if case else "new"
            if admin_status == "review":
                admin_status = "check"
            admin_note = str(case["admin_note"] or "") if case else ""

            payments_total = int(row["total"] or 0)
            success_count = int(row["success_count"] or 0)
            risk_score = len(items) * 2 + active_count * 3 + expiring_3d + success_count

            result.append(
                {
                    "tgid": tgid,
                    "count": len(items),
                    "active_count": active_count,
                    "expired_count": expired_count,
                    "expiring_3d": expiring_3d,
                    "names": names[:10],
                    "plans": sorted(plan_keys),
                    "plan_keys": sorted(plan_keys),
                    "success_count": success_count,
                    "payments_total": payments_total,
                    "payment_count": payments_total,
                    "buy_count": int(row["buy_count"] or 0),
                    "renew_count": int(row["renew_count"] or 0),
                    "last_paid_at": payment_dt_text(row["last_paid_at"]),
                    "suspicious_reasons": suspicious_reasons,
                    "reasons": suspicious_reasons,
                    "suspicious": bool(suspicious_reasons),
                    "risk_score": risk_score,
                    "admin_status": admin_status,
                    "admin_note": admin_note,
                }
            )

    result.sort(
        key=lambda x: (
            0 if x["admin_status"] == "check" else 1,
            0 if x["suspicious_reasons"] else 1,
            -x["risk_score"],
            -x["active_count"],
            -x["count"],
        )
    )
    return result

# =========================
# S-UI
# =========================

async def get_antiabuse_case(tgid: int):
    async with aiosqlite.connect(ANTIABUSE_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM antiabuse_cases WHERE tgid=? LIMIT 1",
            (int(tgid),),
        ) as cur:
            return await cur.fetchone()

def cancel_inline_keyboard(callback_data: str = "menu:back") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Отменить", callback_data=callback_data)]]
    )

def antiabuse_note_prompt_text() -> str:
    return "📝 Введите заметку для кейса antiabuse одним сообщением."


def antiabuse_disable_prompt_text(name: str) -> str:
    return (
        "⏱ Время отключения\n\n"
        f"Name: {name}\n\n"
        "Отправьте числом количество минут для временного отключения подписки."
    )



@router.callback_query(F.data == "noop")
async def noop_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()

# =========================
# HANDLERS
# =========================

@router.message(Command("start"))
async def start_handler(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(welcome_text(), reply_markup=main_menu(message.from_user.id))


@router.message(Command("menu"))
async def menu_handler(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(menu_text(), reply_markup=main_menu(message.from_user.id))


async def safe_delete_message(message: types.Message) -> bool:
    try:
        await message.delete()
        return True
    except TelegramBadRequest:
        return False
    except Exception:
        return False


async def safe_delete_user_command_message(message: types.Message) -> None:
    with contextlib.suppress(TelegramBadRequest, Exception):
        await message.delete()


@router.callback_query(F.data == "menu:back")
async def menu_back_handler(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    deleted = await safe_delete_message(callback.message)
    if not deleted:
        await safe_edit_text(callback.message, menu_text())



@router.message(F.text == "🎁 Тестовый доступ")
async def trial_access_handler(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    await safe_delete_user_command_message(message)
    await message.answer(trial_menu_text(), reply_markup=trial_menu_keyboard())


@router.callback_query(F.data == "trial:get")
async def trial_get_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()

    pending = await get_latest_pending_payment_for_user(callback.from_user.id)
    if pending:
        title = tariff_title_or_key(str(pending["tarif_key"]))
        await safe_edit_text(
            callback.message,
            "⚠️ У вас уже есть pending-счёт.\n\n"
            f"Payment ID: #{pending['id']}\n"
            f"Тип: {payment_kind_label(str(pending['kind']))}\n"
            f"Тариф: {title}\n"
            f"Статус: {payment_status_label(str(pending['status']))}\n\n"
            "Сначала завершите текущий платёж.",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    if await user_has_trial_access(callback.from_user.id):
        await safe_edit_text(callback.message, trial_denied_text(), reply_markup=back_to_menu_keyboard())
        return

    try:
        sub_link, clash_link, client_name = await create_trial_client(
            user_id=callback.from_user.id,
            username=callback.from_user.username,
        )
        await save_trial_record(
            user_id=callback.from_user.id,
            username=callback.from_user.username,
            client_name=client_name,
            notes="trial issued",
        )
    except Exception as exc:
        await safe_edit_text(callback.message, "❌ Не удалось выдать тестовый доступ.\n\n" + str(exc)[:700], reply_markup=back_to_menu_keyboard())
        return

    await safe_edit_text(callback.message, trial_granted_text(sub_link, clash_link), reply_markup=close_keyboard())

@router.message(F.text == "🛒 Купить")
async def buy_menu_handler(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    await safe_delete_user_command_message(message)
    pending = await get_latest_pending_payment_for_user(message.from_user.id)
    if pending:
        title = tariff_title_or_key(str(pending["tarif_key"]))
        await message.answer(
            "⚠️ У вас уже есть pending-счёт.\n\n"
            f"Payment ID: #{pending['id']}\n"
            f"Тип: {payment_kind_label(str(pending['kind']))}\n"
            f"Тариф: {title}\n"
            f"Статус: {payment_status_label(str(pending['status']))}\n\n"
            "Можно открыть новый только после завершения текущего платежа.",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    await message.answer(tariffs_text(), reply_markup=tariffs_keyboard(message.from_user.id))


@router.callback_query(F.data == "buy:menu")
async def buy_menu_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    await safe_edit_text(callback.message, tariffs_text(), reply_markup=tariffs_keyboard(callback.from_user.id))


@router.callback_query(F.data.startswith("buy:preview:"))
async def buy_preview_handler(callback: types.CallbackQuery) -> None:
    await callback.answer()
    tariff_key = callback.data.split(":")[-1]
    tariff = get_tariff(tariff_key)
    if not tariff:
        await safe_edit_text(callback.message, "Тариф не найден.", reply_markup=back_to_menu_keyboard())
        return
    if tariff.is_admin_only and not is_admin(callback.from_user.id):
        await safe_edit_text(callback.message, "Этот тариф доступен только админу.", reply_markup=back_to_menu_keyboard())
        return
    await safe_edit_text(callback.message, tariff_preview_text(tariff), reply_markup=tariff_preview_keyboard(tariff))


@router.callback_query(F.data.startswith("buy:invoice:"))
async def buy_invoice_handler(callback: types.CallbackQuery, bot: Bot) -> None:
    await callback.answer()
    tariff_key = callback.data.split(":")[-1]
    tariff = get_tariff(tariff_key)
    if not tariff:
        await safe_edit_text(callback.message, "Тариф не найден.", reply_markup=back_to_menu_keyboard())
        return
    if tariff.is_admin_only and not is_admin(callback.from_user.id):
        await safe_edit_text(callback.message, "Этот тариф доступен только админу.", reply_markup=back_to_menu_keyboard())
        return

    existing = await get_pending_payment_for_target(user_id=callback.from_user.id, kind="buy", tarif_key=tariff.key, client_id=None)
    if existing:
        await callback.message.answer(f"⚠️ Уже есть pending-счёт на этот тариф: #{existing['id']}. Откройте его в разделе «Мои платежи».")
        return

    payment_id = await create_payment(
        user_id=callback.from_user.id,
        username=callback.from_user.username,
        tarif_key=tariff.key,
        amount=tariff.stars_price,
        kind="buy",
        notes="single_file_buy",
    )
    payment = await get_payment(payment_id)
    await callback.message.answer(payment_created_text(payment_id, tariff, "buy"))
    await send_stars_invoice_for_payment(bot, callback.from_user.id, payment)


@router.message(F.text == "🔑 Мои подписки")
async def my_subscriptions_handler(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    await safe_delete_user_command_message(message)
    try:
        items = await get_user_subscriptions(message.from_user.id)
    except Exception as exc:
        await message.answer("❌ Не удалось получить подписки из S-UI.\n\n" + str(exc)[:700])
        return

    if not items:
        await message.answer(no_subscriptions_text(), reply_markup=close_keyboard())
        return

    text = subscriptions_header_text(len(items)) + "\n\n" + "\n\n".join(format_subscription_card(item) for item in items)
    await send_long_message(message, text, reply_markup=close_keyboard())


@router.message(F.text == "🔄 Продлить")
async def renew_handler(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    await safe_delete_user_command_message(message)
    pending = await get_latest_pending_payment_for_user(message.from_user.id)
    if pending:
        title = tariff_title_or_key(str(pending["tarif_key"]))
        await message.answer(
            "⚠️ У вас уже есть pending-счёт.\n\n"
            f"Payment ID: #{pending['id']}\n"
            f"Тип: {payment_kind_label(str(pending['kind']))}\n"
            f"Тариф: {title}\n"
            f"Статус: {payment_status_label(str(pending['status']))}\n\n"
            "Сначала завершите текущий платёж.",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    try:
        items = await get_user_subscriptions(message.from_user.id)
    except Exception as exc:
        await message.answer("❌ Не удалось получить подписки из S-UI.\n\n" + str(exc)[:700])
        return

    renewable = [item for item in items if str(item["plan_key"]) != "test" and get_tariff(str(item["plan_key"])) is not None]
    if not renewable:
        await message.answer(no_renewable_subscriptions_text())
        return

    await message.answer(renew_menu_text(len(renewable)), reply_markup=renew_list_keyboard(renewable))


@router.callback_query(F.data == "renew:menu")
async def renew_menu_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    try:
        items = await get_user_subscriptions(callback.from_user.id)
    except Exception as exc:
        await safe_edit_text(callback.message, "❌ Не удалось получить подписки из S-UI.\n\n" + str(exc)[:700])
        return

    renewable = [item for item in items if get_tariff(str(item["plan_key"])) is not None]
    if not renewable:
        await safe_edit_text(callback.message, no_renewable_subscriptions_text())
        return

    await safe_edit_text(callback.message, renew_menu_text(len(renewable)), reply_markup=renew_list_keyboard(renewable))


@router.callback_query(F.data.startswith("renew:pick:"))
async def renew_pick_handler(callback: types.CallbackQuery) -> None:
    await callback.answer()
    client_id = int(callback.data.split(":")[-1])

    try:
        items = await get_user_subscriptions(callback.from_user.id)
    except Exception as exc:
        await safe_edit_text(callback.message, "❌ Не удалось получить подписки из S-UI.\n\n" + str(exc)[:700])
        return

    item = next((x for x in items if int(x["id"]) == client_id), None)
    if not item:
        await safe_edit_text(callback.message, "Подписка не найдена.", reply_markup=back_to_menu_keyboard())
        return

    tariff = get_tariff(str(item["plan_key"]))
    if not tariff:
        await safe_edit_text(callback.message, "Для этой подписки продление недоступно.", reply_markup=back_to_menu_keyboard())
        return

    await safe_edit_text(callback.message, renew_preview_text(item, tariff), reply_markup=renew_preview_keyboard(client_id))


@router.callback_query(F.data.startswith("renew:invoice:"))
async def renew_invoice_handler(callback: types.CallbackQuery, bot: Bot) -> None:
    await callback.answer()
    client_id = int(callback.data.split(":")[-1])

    try:
        items = await get_user_subscriptions(callback.from_user.id)
    except Exception as exc:
        await callback.message.answer("❌ Не удалось получить подписки из S-UI.\n\n" + str(exc)[:700])
        return

    item = next((x for x in items if int(x["id"]) == client_id), None)
    if not item:
        await callback.message.answer("Подписка не найдена.")
        return

    tariff = get_tariff(str(item["plan_key"]))
    if not tariff:
        await callback.message.answer("Для этой подписки продление недоступно.")
        return

    existing = await get_pending_payment_for_target(user_id=callback.from_user.id, kind="renew", tarif_key=tariff.key, client_id=client_id)
    if existing:
        await callback.message.answer(f"⚠️ Уже есть pending-счёт на это продление: #{existing['id']}. Откройте его в разделе «Мои платежи».")
        return

    payment_id = await create_payment(
        user_id=callback.from_user.id,
        username=callback.from_user.username,
        tarif_key=tariff.key,
        amount=tariff.stars_price,
        kind="renew",
        client_id=client_id,
        notes="single_file_renew",
    )
    payment = await get_payment(payment_id)
    await callback.message.answer(payment_created_text(payment_id, tariff, "renew"))
    await send_stars_invoice_for_payment(bot, callback.from_user.id, payment)


@router.pre_checkout_query()
async def pre_checkout_handler(pre_checkout_query: types.PreCheckoutQuery) -> None:
    payload = str(pre_checkout_query.invoice_payload or "")
    if not payload.startswith("stars:"):
        await pre_checkout_query.answer(ok=False, error_message="Некорректный payload.")
        return

    payment_uid = payload.split(":", 1)[1]
    payment = await get_payment_by_uid(payment_uid)
    if not payment:
        await pre_checkout_query.answer(ok=False, error_message="Счёт не найден.")
        return

    if str(payment["status"]) != "pending":
        await pre_checkout_query.answer(ok=False, error_message="Счёт уже обработан.")
        return

    await pre_checkout_query.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment_handler(message: types.Message) -> None:
    payload = str(message.successful_payment.invoice_payload or "")
    if not payload.startswith("stars:"):
        return

    payment_uid = payload.split(":", 1)[1]
    charge_id = str(message.successful_payment.telegram_payment_charge_id or "")
    payment = await get_payment_by_uid(payment_uid)
    if not payment:
        await message.answer("Платёж получен, но счёт не найден.")
        return

    tariff = get_tariff(str(payment["tarif_key"]))
    if not tariff:
        await message.answer("Тариф не найден после оплаты.")
        return

    kind = str(payment["kind"] or "buy")
    current_status = str(payment["status"] or "")
    current_app_status = str(payment["application_status"] or "not_applied")
    result_payload = payment_result_payload_loads(payment["result_payload"] if "result_payload" in payment.keys() else None)

    if current_status == "success" and current_app_status == "applied" and result_payload:
        await message.answer(payment_success_text_from_payload(kind, tariff, result_payload))
        if not payment["notified_at"]:
            await mark_payment_notified(int(payment["id"]), note="duplicate_event_after_success")
        return

    if current_status == "processing":
        if current_app_status == "applied" and result_payload:
            await message.answer(payment_success_text_from_payload(kind, tariff, result_payload))
            if not payment["notified_at"]:
                await mark_payment_notified(int(payment["id"]), note="duplicate_event_after_apply")
        else:
            await message.answer("Платёж уже получен и сейчас обрабатывается. Если сообщение с подпиской не придёт, обратитесь в поддержку.")
        return

    if current_status != "pending":
        await message.answer("Этот платёж уже был обработан.")
        return

    claimed = await mark_payment_processing(int(payment["id"]), provider_charge_id=charge_id)
    if not claimed:
        payment = await get_payment_by_uid(payment_uid)
        if payment:
            result_payload = payment_result_payload_loads(payment["result_payload"] if "result_payload" in payment.keys() else None)
            if str(payment["application_status"] or "") == "applied" and result_payload:
                await message.answer(payment_success_text_from_payload(kind, tariff, result_payload))
                if not payment["notified_at"]:
                    await mark_payment_notified(int(payment["id"]), note="duplicate_event_after_claim")
                return
        await message.answer("Этот платёж уже был обработан.")
        return

    try:
        if kind == "buy":
            sub_link, clash_link, client_name = await create_client(
                user_id=message.from_user.id,
                username=message.from_user.username,
                tariff=tariff,
            )
            result_payload = {
                "kind": "buy",
                "client_name": client_name,
                "sub_link": sub_link,
                "clash_link": clash_link,
            }
            await mark_payment_applied(
                int(payment["id"]),
                provider_charge_id=charge_id,
                result_payload=result_payload,
                note="buy_applied",
            )
            await message.answer(payment_success_text_from_payload(kind, tariff, result_payload))
            await mark_payment_notified(int(payment["id"]), note="buy_notified")
            return

        if kind == "renew":
            client_id = int(payment["client_id"] or 0)
            if client_id <= 0:
                raise RuntimeError("У платежа продления нет client_id")
            client = await get_client_by_id(client_id)
            if not client:
                raise RuntimeError("Подписка для продления не найдена в S-UI")
            owner_items = await get_user_subscriptions(message.from_user.id)
            owner_item = next((x for x in owner_items if int(x["id"]) == client_id), None)
            if not owner_item:
                raise RuntimeError("Эта подписка не принадлежит текущему пользователю")

            await renew_client_in_sui(client, tariff)
            result_payload = {
                "kind": "renew",
                "client_id": client_id,
                "name": owner_item["name"],
                "sub_link": owner_item["sub_link"],
                "clash_link": owner_item["clash_link"],
            }
            await mark_payment_applied(
                int(payment["id"]),
                provider_charge_id=charge_id,
                result_payload=result_payload,
                note="renew_applied",
            )
            await message.answer(payment_success_text_from_payload(kind, tariff, result_payload))
            await mark_payment_notified(int(payment["id"]), note="renew_notified")
            return

        raise RuntimeError(f"Неизвестный тип платежа: {kind}")
    except Exception as exc:
        await mark_payment_apply_failed(int(payment["id"]), note=str(exc)[:300])
        await message.answer("❌ Оплата прошла, но при применении действия возникла ошибка.\n\n" + str(exc)[:700])


@router.message(F.text == "💳 Мои платежи")
async def my_payments_handler(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    await safe_delete_user_command_message(message)
    rows = await list_user_payments(message.from_user.id, limit=20)
    if not rows:
        await message.answer(my_payments_empty_text(), reply_markup=close_keyboard())
        return
    await message.answer("💳 Мои платежи\n\nВыберите платёж ниже:", reply_markup=user_payments_keyboard(rows))


@router.callback_query(F.data == "pay:list")
async def pay_list_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    rows = await list_user_payments(callback.from_user.id, limit=20)
    if not rows:
        await safe_edit_text(callback.message, my_payments_empty_text(), reply_markup=close_keyboard())
        return
    await safe_edit_text(callback.message, "💳 Мои платежи\n\nВыберите платёж ниже:", reply_markup=user_payments_keyboard(rows))


@router.callback_query(F.data.startswith("pay:view:"))
async def pay_view_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    payment_id = int(callback.data.split(":")[-1])
    row = await get_payment(payment_id)
    if not row or int(row["user_id"]) != int(callback.from_user.id):
        await safe_edit_text(callback.message, "Платёж не найден.")
        return
    await safe_edit_text(callback.message, payment_detail_text(row, tariff_title_or_key(str(row["tarif_key"]))), reply_markup=user_payment_detail_keyboard(row))


@router.callback_query(F.data.startswith("pay:open:"))
async def pay_open_callback(callback: types.CallbackQuery, bot: Bot) -> None:
    await callback.answer()
    payment_id = int(callback.data.split(":")[-1])
    row = await get_payment(payment_id)
    if not row or int(row["user_id"]) != int(callback.from_user.id):
        await callback.message.answer("Платёж не найден.")
        return
    if str(row["status"]) != "pending":
        await callback.message.answer("Этот платёж уже не pending.")
        return
    await send_stars_invoice_for_payment(bot, callback.from_user.id, row)


@router.callback_query(F.data.startswith("pay:cancel:"))
async def pay_cancel_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    payment_id = int(callback.data.split(":")[-1])
    ok = await cancel_pending_payment(payment_id, callback.from_user.id)
    if not ok:
        await callback.message.answer("Не удалось отменить pending-счёт.")
        return
    row = await get_payment(payment_id)
    await safe_edit_text(callback.message, payment_detail_text(row, tariff_title_or_key(str(row["tarif_key"]))), reply_markup=user_payment_detail_keyboard(row))


@router.callback_query(F.data.startswith("pay:retry:"))
async def pay_retry_callback(callback: types.CallbackQuery, bot: Bot) -> None:
    await callback.answer()
    payment_id = int(callback.data.split(":")[-1])
    row = await get_payment(payment_id)
    if not row or int(row["user_id"]) != int(callback.from_user.id):
        await callback.message.answer("Платёж не найден.")
        return
    if str(row["status"]) != "failed":
        await callback.message.answer("Повтор доступен только для failed-платежей.")
        return

    existing = await get_pending_payment_for_target(
        user_id=callback.from_user.id,
        kind=str(row["kind"]),
        tarif_key=str(row["tarif_key"]),
        client_id=int(row["client_id"] or 0) if row["client_id"] is not None else None,
    )
    if existing:
        await callback.message.answer(f"⚠️ Уже есть pending-счёт: #{existing['id']}. Откройте его в разделе «Мои платежи».")
        return

    new_payment_id = await create_payment(
        user_id=callback.from_user.id,
        username=callback.from_user.username,
        tarif_key=str(row["tarif_key"]),
        amount=int(row["amount"] or 0),
        kind=str(row["kind"]),
        client_id=int(row["client_id"] or 0) if row["client_id"] is not None else None,
        notes=f"retry_of:{row['id']}",
    )
    new_row = await get_payment(new_payment_id)
    await callback.message.answer(f"🔄 Создан новый счёт #{new_payment_id} для повторной оплаты.")
    await send_stars_invoice_for_payment(bot, callback.from_user.id, new_row)


@router.message(F.text == "👤 Профиль")
async def profile_handler(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    await safe_delete_user_command_message(message)
    try:
        items = await get_user_subscriptions(message.from_user.id)
    except Exception as exc:
        await message.answer("❌ Не удалось получить профиль из S-UI.\n\n" + str(exc)[:700])
        return

    subscriptions_count = len(items)
    active_count = len([x for x in items if int(x["days_left"]) > 0])
    expiring = [x for x in items if int(x["days_left"]) > 0]
    expiring_next_text = f"{min(x['days_left'] for x in expiring)} дн." if expiring else "нет активных"

    latest_plans = []
    for item in items[:5]:
        plan = str(item.get("plan_key") or "без плана")
        if plan not in latest_plans:
            latest_plans.append(plan)

    username = f"@{message.from_user.username}" if message.from_user.username else "—"
    pending = await get_latest_pending_payment_for_user(message.from_user.id)

    await message.answer(
        profile_text(
            user_id=message.from_user.id,
            username=username,
            subscriptions_count=subscriptions_count,
            active_count=active_count,
            expiring_next_text=expiring_next_text,
            has_pending_payment=bool(pending),
            latest_plans=latest_plans,
        ),
        reply_markup=close_keyboard(),
    )




@router.message(F.text == "💎 Telegram proxy")
async def telegram_proxy_menu_handler(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    await safe_delete_user_command_message(message)
    has_access = await user_has_paid_active_subscription(message.from_user.id)
    if not has_access:
        await message.answer(telegram_proxy_denied_text(), reply_markup=back_to_menu_keyboard())
        return
    rows = await list_telegram_proxies(include_disabled=False)
    if not rows:
        await message.answer(telegram_proxy_empty_text(), reply_markup=back_to_menu_keyboard())
        return
    await message.answer(telegram_proxy_menu_text(), reply_markup=telegram_proxy_user_keyboard(rows))


@router.callback_query(F.data == "admin:tgproxy")
async def admin_tgproxy_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    if not is_admin(callback.from_user.id):
        return
    rows = await list_telegram_proxies(include_disabled=True)
    await safe_edit_text(
        callback.message,
        telegram_proxy_admin_list_text(),
        reply_markup=telegram_proxy_admin_list_keyboard(rows),
    )


@router.callback_query(F.data.startswith("admin:tgproxy:view:"))
async def admin_tgproxy_view_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    proxy_id = int(callback.data.split(":")[-1])
    row = await get_telegram_proxy(proxy_id)
    if not row:
        await safe_edit_text(callback.message, "Telegram proxy не найден.", reply_markup=admin_subpage_keyboard())
        return
    await safe_edit_text(callback.message, telegram_proxy_admin_detail_text(row), reply_markup=telegram_proxy_admin_detail_keyboard(row))


@router.callback_query(F.data == "admin:tgproxy:create")
async def admin_tgproxy_create_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    await state.clear()
    await state.set_state(AdminStates.waiting_tgproxy_create)
    await state.update_data(tgproxy_create_step="title", tgproxy_create_data={})
    await safe_edit_text(
        callback.message,
        telegram_proxy_create_prompt_text("title"),
        reply_markup=telegram_proxy_prompt_keyboard(),
    )


@router.callback_query(F.data.startswith("admin:tgproxy:edit:"))
async def admin_tgproxy_edit_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    parts = callback.data.split(":")
    proxy_id = int(parts[3])
    field_name = parts[4]
    if field_name not in ("title", "url"):
        await safe_edit_text(callback.message, "Поле не поддерживается.", reply_markup=admin_subpage_keyboard())
        return
    row = await get_telegram_proxy(proxy_id)
    if not row:
        await safe_edit_text(callback.message, "Telegram proxy не найден.", reply_markup=admin_subpage_keyboard())
        return
    await state.clear()
    await state.set_state(AdminStates.waiting_tgproxy_edit)
    await state.update_data(tgproxy_id=proxy_id, field_name=field_name)
    await safe_edit_text(
        callback.message,
        telegram_proxy_edit_prompt_text(proxy_id, field_name),
        reply_markup=telegram_proxy_edit_prompt_keyboard(proxy_id),
    )


@router.callback_query(F.data.startswith("admin:tgproxy:toggle:"))
async def admin_tgproxy_toggle_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    proxy_id = int(callback.data.split(":")[-1])
    row = await get_telegram_proxy(proxy_id)
    if not row:
        await safe_edit_text(callback.message, "Telegram proxy не найден.", reply_markup=admin_subpage_keyboard())
        return
    await set_telegram_proxy_enabled(proxy_id, not bool(int(row["enabled"] or 0)))
    row = await get_telegram_proxy(proxy_id)
    await safe_edit_text(callback.message, telegram_proxy_admin_detail_text(row), reply_markup=telegram_proxy_admin_detail_keyboard(row))


@router.callback_query(F.data.startswith("admin:tgproxy:delete:"))
async def admin_tgproxy_delete_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    proxy_id = int(callback.data.split(":")[-1])
    await delete_telegram_proxy(proxy_id)
    rows = await list_telegram_proxies(include_disabled=True)
    await safe_edit_text(
        callback.message,
        telegram_proxy_admin_list_text(),
        reply_markup=telegram_proxy_admin_list_keyboard(rows),
    )


@router.message(AdminStates.waiting_tgproxy_create)
async def admin_tgproxy_create_message(message: types.Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    step = str(data.get("tgproxy_create_step") or "title")
    payload = dict(data.get("tgproxy_create_data") or {})
    raw = (message.text or "").strip()

    try:
        if step == "title":
            if not raw:
                raise ValueError("Название не может быть пустым.")
            payload["title"] = raw[:128]
            next_step = "url"
        elif step == "url":
            normalized_url = normalize_tgproxy_url(raw)
            if not normalized_url:
                raise ValueError("Ссылка должна начинаться с tg://")
            proxy_id = await create_telegram_proxy(payload["title"], normalized_url)
            await state.clear()
            row = await get_telegram_proxy(proxy_id)
            await message.answer(telegram_proxy_admin_detail_text(row), reply_markup=telegram_proxy_admin_detail_keyboard(row))
            return
        else:
            raise ValueError("Неизвестный шаг создания Telegram proxy.")
    except Exception as exc:
        await message.answer(f"Ошибка: {exc}", reply_markup=telegram_proxy_prompt_keyboard())
        return

    await state.update_data(tgproxy_create_step=next_step, tgproxy_create_data=payload)
    await message.answer(telegram_proxy_create_prompt_text(next_step), reply_markup=telegram_proxy_prompt_keyboard())


@router.message(AdminStates.waiting_tgproxy_edit)
async def admin_tgproxy_edit_message(message: types.Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    proxy_id = data.get("tgproxy_id")
    field_name = data.get("field_name")
    if not proxy_id or not field_name:
        await state.clear()
        await message.answer("Сессия редактирования потеряна.", reply_markup=admin_subpage_keyboard())
        return

    raw = (message.text or "").strip()
    try:
        if field_name == "title":
            await update_telegram_proxy(int(proxy_id), title=raw)
        elif field_name == "url":
            await update_telegram_proxy(int(proxy_id), url=raw)
        else:
            raise ValueError("Поле не поддерживается.")
    except Exception as exc:
        await message.answer(f"Ошибка: {exc}", reply_markup=telegram_proxy_edit_prompt_keyboard(int(proxy_id)))
        return

    await state.clear()
    row = await get_telegram_proxy(int(proxy_id))
    await message.answer(telegram_proxy_admin_detail_text(row), reply_markup=telegram_proxy_admin_detail_keyboard(row))

@router.message(F.text == "❓ FAQ")
async def faq_handler(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    await safe_delete_user_command_message(message)
    await message.answer(faq_main_text(), reply_markup=faq_main_keyboard())


@router.callback_query(F.data == "faq:main")
async def faq_main_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    await safe_edit_text(callback.message, faq_main_text(), reply_markup=faq_main_keyboard())


@router.callback_query(F.data == "faq:connect_menu")
async def faq_connect_menu_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    await safe_edit_text(callback.message, faq_connect_menu_text(), reply_markup=faq_connect_keyboard())


@router.callback_query(F.data.startswith("faq:device:"))
async def faq_device_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    platform = callback.data.split(":")[-1]
    await safe_edit_text(callback.message, faq_device_text(platform), reply_markup=faq_device_links_keyboard(platform))


@router.callback_query(F.data == "faq:router")
async def faq_router_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    await safe_edit_text(callback.message, faq_router_text(), reply_markup=faq_router_keyboard())


@router.callback_query(F.data == "faq:proxy")
async def faq_router_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    await safe_edit_text(callback.message, faq_proxy_text(), reply_markup=faq_subpage_keyboard())


@router.callback_query(F.data == "faq:problems")
async def faq_problems_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    await safe_edit_text(callback.message, faq_problems_text(), reply_markup=faq_subpage_keyboard())


@router.callback_query(F.data == "faq:about")
async def faq_about_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    await safe_edit_text(callback.message, faq_about_text(), reply_markup=faq_subpage_keyboard())



@router.callback_query(F.data == "faq:stars")
async def faq_stars_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    await safe_edit_text(callback.message, faq_stars_text(), reply_markup=stars_faq_keyboard_from_menu())


@router.callback_query(F.data.startswith("faq:stars:back:"))
async def faq_stars_back_to_preview_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    parts = callback.data.split(":", 3)
    tariff_key = parts[3] if len(parts) == 4 else ""
    tariff = get_tariff(tariff_key)
    if not tariff:
        await safe_edit_text(callback.message, tariffs_text(), reply_markup=tariffs_keyboard(callback.from_user.id))
        return
    await safe_edit_text(callback.message, tariff_preview_text(tariff), reply_markup=tariff_preview_keyboard(tariff))


@router.callback_query(F.data.startswith("faq:stars:"))
async def faq_stars_from_buy_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    parts = callback.data.split(":", 2)
    tariff_key = parts[2] if len(parts) == 3 else ""
    await safe_edit_text(callback.message, faq_stars_text(), reply_markup=stars_faq_keyboard_from_buy(tariff_key))


@router.callback_query(F.data == "faq:connections")
async def faq_connections_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    await safe_edit_text(callback.message, faq_connections_text(), reply_markup=faq_subpage_keyboard())


@router.callback_query(F.data == "faq:restricted")
async def faq_restricted_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    await safe_edit_text(callback.message, faq_restricted_text(), reply_markup=faq_subpage_keyboard())


@router.message(F.text == "🆘 Поддержка")
async def support_handler(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    await safe_delete_user_command_message(message)
    await message.answer(support_menu_text(), reply_markup=support_menu_keyboard())


@router.callback_query(F.data == "support:menu")
async def support_menu_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await safe_edit_text(callback.message, support_menu_text(), reply_markup=support_menu_keyboard())


@router.callback_query(F.data == "support:new")
async def support_new_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    open_ticket = await get_open_ticket_for_user(callback.from_user.id)
    if open_ticket:
        await safe_edit_text(
            callback.message,
            "⚠️ У вас уже есть открытое обращение. Продолжите его в списке обращений.",
            reply_markup=support_menu_keyboard(),
        )
        return

    remaining = await get_new_ticket_cooldown_remaining(callback.from_user.id)
    if remaining > 0:
        await safe_edit_text(
            callback.message,
            support_new_ticket_cooldown_text(remaining),
            reply_markup=support_menu_keyboard(),
        )
        return

    await state.set_state(SupportStates.waiting_new_ticket)
    await safe_edit_text(callback.message, support_new_ticket_prompt_text(), reply_markup=support_prompt_keyboard())



@router.message(Command("start"), SupportStates.waiting_new_ticket)
@router.message(Command("menu"), SupportStates.waiting_new_ticket)
@router.message(Command("start"), SupportStates.waiting_user_reply)
@router.message(Command("menu"), SupportStates.waiting_user_reply)
@router.message(Command("start"), SupportStates.waiting_admin_reply)
@router.message(Command("menu"), SupportStates.waiting_admin_reply)
@router.message(Command("start"), SupportStates.waiting_admin_note)
@router.message(Command("menu"), SupportStates.waiting_admin_note)
async def support_command_cancel(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(menu_text(), reply_markup=main_menu(message.from_user.id))


@router.message(Command("start"), AdminStates.waiting_payment_search)
@router.message(Command("menu"), AdminStates.waiting_payment_search)
@router.message(Command("start"), AdminStates.waiting_broadcast_text)
@router.message(Command("start"), AdminStates.waiting_tariff_create)
@router.message(Command("menu"), AdminStates.waiting_tariff_create)
async def tariff_create_command_cancel(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(menu_text(), reply_markup=main_menu(message.from_user.id))


@router.message(Command("start"), AdminStates.waiting_direct_message_target)
@router.message(Command("menu"), AdminStates.waiting_direct_message_target)
@router.message(Command("start"), AdminStates.waiting_direct_message_text)
@router.message(Command("menu"), AdminStates.waiting_direct_message_text)
async def direct_message_command_cancel(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(menu_text(), reply_markup=main_menu(message.from_user.id))


@router.message(Command("menu"), AdminStates.waiting_broadcast_text)
async def broadcast_command_cancel(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(menu_text(), reply_markup=main_menu(message.from_user.id))


@router.message(SupportStates.waiting_new_ticket)
async def support_new_ticket_message(message: types.Message, state: FSMContext, bot: Bot) -> None:
    payload = support_extract_message_payload(message)
    if not payload:
        await message.answer("Поддерживаются текст, фото, видео, документ, аудио и голосовое сообщение.")
        return

    subject = str(payload["preview_text"]).splitlines()[0][:80]
    ticket_id = await create_ticket(
        user_id=message.from_user.id,
        username=message.from_user.username,
        subject=subject,
        message_text=str(payload["message_text"] or ""),
        content_type=str(payload["content_type"] or "text"),
        file_id=payload.get("file_id"),
        file_name=payload.get("file_name"),
        mime_type=payload.get("mime_type"),
    )
    await state.clear()
    ticket = await get_ticket(ticket_id)

    await message.answer(
        support_ticket_created_text(ticket_id),
        reply_markup=support_ticket_keyboard(ticket),
    )

    username = f"@{message.from_user.username}" if message.from_user.username else "—"
    await notify_admins_about_ticket(
        bot,
        ticket_id,
        (
            f"🆘 Новый тикет #{ticket_id}\n\n"
            f"От: {username}\n"
            f"User ID: {message.from_user.id}\n"
            f"Тема: {subject}"
        ),
    )
    for admin_id in ADMIN_IDS:
        try:
            await support_send_payload_to_chat(bot, int(admin_id), payload)
        except Exception:
            pass


@router.callback_query(F.data == "support:list")
async def support_list_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    rows = await list_user_tickets(callback.from_user.id)
    if not rows:
        await safe_edit_text(callback.message, support_no_tickets_text(), reply_markup=support_menu_keyboard())
        return
    await safe_edit_text(
        callback.message,
        "📂 Ваши обращения\n\nВыберите тикет ниже.",
        reply_markup=support_user_tickets_keyboard(rows),
    )


@router.callback_query(F.data.startswith("support:view:"))
async def support_view_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    ticket_id = int(callback.data.split(":")[-1])
    ticket = await get_ticket(ticket_id)
    if not ticket or int(ticket["user_id"]) != int(callback.from_user.id):
        await safe_edit_text(callback.message, "Тикет не найден.")
        return
    msgs = await list_ticket_messages(ticket_id, limit=200, include_notes=False)
    await safe_edit_text(
        callback.message,
        support_ticket_card_text(ticket, msgs),
        reply_markup=support_ticket_keyboard(ticket, back_callback="support:list"),
    )


@router.callback_query(F.data.startswith("support:refresh:"))
async def support_refresh_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    parts = callback.data.split(":")
    ticket_id = int(parts[2]) if len(parts) >= 3 else int(parts[-1])
    status_filter = parts[3] if len(parts) >= 4 else "all"
    try:
        page = int(parts[4]) if len(parts) >= 5 else 1
    except Exception:
        page = 1

    ticket = await get_ticket(ticket_id)
    if not ticket:
        await safe_edit_text(callback.message, "Тикет не найден.")
        return

    msgs_user = await list_ticket_messages(ticket_id, limit=200, include_notes=False)
    msgs_admin = await list_ticket_messages(ticket_id, limit=200, include_notes=True)
    if int(ticket["user_id"]) == int(callback.from_user.id):
        await safe_edit_text(
            callback.message,
            support_ticket_card_text(ticket, msgs_user),
            reply_markup=support_ticket_keyboard(ticket, back_callback="support:list"),
        )
        return

    if is_admin(callback.from_user.id):
        await safe_edit_text(
            callback.message,
            support_ticket_card_text(ticket, msgs_admin),
            reply_markup=admin_ticket_keyboard(ticket, status_filter=status_filter, page=page),
        )


@router.callback_query(F.data.startswith("support:reply:"))
async def support_reply_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    parts = callback.data.split(":")
    ticket_id = int(parts[2]) if len(parts) >= 3 else int(parts[-1])
    status_filter = parts[3] if len(parts) >= 4 else "all"
    try:
        page = int(parts[4]) if len(parts) >= 5 else 1
    except Exception:
        page = 1

    ticket = await get_ticket(ticket_id)
    if not ticket:
        await safe_edit_text(callback.message, "Тикет не найден.")
        return

    if int(ticket["user_id"]) == int(callback.from_user.id):
        await state.set_state(SupportStates.waiting_user_reply)
        await state.update_data(ticket_id=ticket_id)
        await safe_edit_text(callback.message, support_reply_prompt_text(ticket_id))
        return

    if is_admin(callback.from_user.id):
        await state.set_state(SupportStates.waiting_admin_reply)
        await state.update_data(ticket_id=ticket_id, support_admin_filter=status_filter, support_admin_page=page)
        await safe_edit_text(callback.message, support_reply_prompt_text(ticket_id))
        return


@router.message(SupportStates.waiting_user_reply)
async def support_user_reply_message(message: types.Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    ticket_id = int(data["ticket_id"])
    ticket = await get_ticket(ticket_id)
    if not ticket or int(ticket["user_id"]) != int(message.from_user.id):
        await state.clear()
        await message.answer("Тикет не найден.")
        return

    payload = support_extract_message_payload(message)
    if not payload:
        await message.answer("Поддерживаются текст, фото, видео, документ, аудио и голосовое сообщение.")
        return

    await add_support_message(
        ticket_id,
        message.from_user.id,
        "user",
        str(payload["message_text"] or ""),
        content_type=str(payload["content_type"] or "text"),
        file_id=payload.get("file_id"),
        file_name=payload.get("file_name"),
        mime_type=payload.get("mime_type"),
    )
    await state.clear()
    updated = await get_ticket(ticket_id)
    msgs = await list_ticket_messages(ticket_id, limit=200, include_notes=False)

    await message.answer(
        support_ticket_card_text(updated, msgs),
        reply_markup=support_ticket_keyboard(updated, back_callback="support:list"),
    )

    username = f"@{message.from_user.username}" if message.from_user.username else "—"
    await notify_admins_about_ticket(
        bot,
        ticket_id,
        (
            f"📩 Новый ответ пользователя в тикет #{ticket_id}\n\n"
            f"От: {username}\n"
            f"User ID: {message.from_user.id}\n\n"
            f"{str(payload['preview_text'])[:1000]}"
        ),
    )
    for admin_id in ADMIN_IDS:
        try:
            await support_send_payload_to_chat(bot, int(admin_id), payload)
        except Exception:
            pass


@router.message(SupportStates.waiting_admin_reply)
async def support_admin_reply_message(message: types.Message, state: FSMContext, bot: Bot) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    data = await state.get_data()
    ticket_id = int(data["ticket_id"])
    ticket = await get_ticket(ticket_id)
    if not ticket:
        await state.clear()
        await message.answer("Тикет не найден.")
        return

    payload = support_extract_message_payload(message)
    if not payload:
        await message.answer("Поддерживаются текст, фото, видео, документ, аудио и голосовое сообщение.")
        return

    await add_support_message(
        ticket_id,
        message.from_user.id,
        "admin",
        str(payload["message_text"] or ""),
        content_type=str(payload["content_type"] or "text"),
        file_id=payload.get("file_id"),
        file_name=payload.get("file_name"),
        mime_type=payload.get("mime_type"),
    )
    status_filter = str(data.get("support_admin_filter") or "all")
    page = int(data.get("support_admin_page") or 1)
    await state.clear()
    updated = await get_ticket(ticket_id)
    msgs = await list_ticket_messages(ticket_id, limit=200)

    await message.answer(
        support_ticket_preview_text(updated, msgs),
        reply_markup=admin_ticket_keyboard(updated, status_filter=status_filter, page=page),
    )

    await notify_user_about_admin_reply(bot, int(ticket["user_id"]), ticket_id)
    try:
        await support_send_payload_to_chat(bot, int(ticket["user_id"]), payload)
    except Exception:
        pass


@router.callback_query(F.data.startswith("support:close:"))
async def support_close_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    parts = callback.data.split(":")
    ticket_id = int(parts[2]) if len(parts) >= 3 else int(parts[-1])
    status_filter = parts[3] if len(parts) >= 4 else "all"
    try:
        page = int(parts[4]) if len(parts) >= 5 else 1
    except Exception:
        page = 1
    ticket = await get_ticket(ticket_id)
    if not ticket:
        await safe_edit_text(callback.message, "Тикет не найден.")
        return
    if int(ticket["user_id"]) != int(callback.from_user.id) and not is_admin(callback.from_user.id):
        await safe_edit_text(callback.message, "Нет доступа к тикету.")
        return

    await close_ticket(ticket_id)
    updated = await get_ticket(ticket_id)
    msgs = await list_ticket_messages(ticket_id, limit=200, include_notes=not int(updated["user_id"]) == int(callback.from_user.id))
    if int(updated["user_id"]) == int(callback.from_user.id):
        await safe_edit_text(
            callback.message,
            support_ticket_card_text(updated, msgs),
            reply_markup=support_ticket_keyboard(updated, back_callback="support:list"),
        )
    else:
        await safe_edit_text(
            callback.message,
            support_ticket_card_text(updated, msgs),
            reply_markup=admin_ticket_keyboard(updated, status_filter=status_filter, page=page),
        )


@router.callback_query(F.data.startswith("support:reopen:"))
async def support_reopen_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    parts = callback.data.split(":")
    ticket_id = int(parts[2]) if len(parts) >= 3 else int(parts[-1])
    status_filter = parts[3] if len(parts) >= 4 else "all"
    try:
        page = int(parts[4]) if len(parts) >= 5 else 1
    except Exception:
        page = 1
    ticket = await get_ticket(ticket_id)
    if not ticket:
        await safe_edit_text(callback.message, "Тикет не найден.")
        return
    if int(ticket["user_id"]) != int(callback.from_user.id) and not is_admin(callback.from_user.id):
        await safe_edit_text(callback.message, "Нет доступа к тикету.")
        return

    if not is_admin(callback.from_user.id):
        cooldown_left = await support_get_reopen_cooldown_left(ticket_id)
        if cooldown_left > 0:
            mins = max(1, (cooldown_left + 59) // 60)
            await safe_edit_text(callback.message, f"⏳ Переоткрывать тикет можно не чаще 1 раза в час.\n\nПопробуйте снова примерно через {mins} мин.", reply_markup=support_ticket_keyboard(ticket, back_callback="support:list"))
            return

    await reopen_ticket(ticket_id)
    if not is_admin(callback.from_user.id):
        await support_mark_user_reopen(ticket_id)
    updated = await get_ticket(ticket_id)
    msgs = await list_ticket_messages(ticket_id, limit=200, include_notes=not int(updated["user_id"]) == int(callback.from_user.id))
    if int(updated["user_id"]) == int(callback.from_user.id):
        await safe_edit_text(
            callback.message,
            support_ticket_card_text(updated, msgs),
            reply_markup=support_ticket_keyboard(updated, back_callback="support:list"),
        )
    else:
        await safe_edit_text(
            callback.message,
            support_ticket_card_text(updated, msgs),
            reply_markup=admin_ticket_keyboard(updated, status_filter=status_filter, page=page),
        )


@router.message(F.text == "🛠 Админ")
async def admin_handler(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    await safe_delete_user_command_message(message)
    if not is_admin(message.from_user.id):
        return
    await message.answer(admin_panel_text(), reply_markup=admin_panel_keyboard())


@router.callback_query(F.data == "admin:panel")
async def admin_panel_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    if not is_admin(callback.from_user.id):
        return
    await safe_edit_text(callback.message, admin_panel_text(), reply_markup=admin_panel_keyboard())



@router.callback_query(F.data == "admin:payments")
async def admin_payments_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    await state.clear()
    rows = await list_admin_payments_filtered("all")
    await safe_edit_text(
        callback.message,
        admin_payments_text("all"),
        reply_markup=admin_payments_keyboard(rows, "all"),
    )


@router.callback_query(F.data.startswith("admin:payments:"))
async def admin_payments_sub_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return

    data = callback.data

    if data == "admin:payments:search":
        await state.clear()
        await state.set_state(AdminStates.waiting_payment_search)
        await safe_edit_text(
            callback.message,
            admin_payment_search_prompt_text(),
            reply_markup=admin_payment_search_prompt_keyboard(),
        )
        return

    if data in ("admin:payments:success", "admin:payments:pending", "admin:payments:failed"):
        status_filter = data.split(":")[-1]
        rows = await list_admin_payments_filtered(status_filter)
        await safe_edit_text(
            callback.message,
            admin_payments_text(status_filter),
            reply_markup=admin_payments_keyboard(rows, status_filter),
        )
        return

    if data.startswith("admin:payments:view:"):
        payment_id = int(data.split(":")[-1])
        row = await get_payment(payment_id)
        if not row:
            await safe_edit_text(callback.message, "Платёж не найден.", reply_markup=admin_subpage_keyboard())
            return
        await safe_edit_text(
            callback.message,
            admin_payment_detail_text(row, tariff_title_or_key(str(row["tarif_key"]))),
            reply_markup=admin_payment_detail_keyboard(row),
        )
        return

    if data.startswith("admin:payments:approve:"):
        payment_id = int(data.split(":")[-1])
        row = await get_payment(payment_id)
        if not row:
            await safe_edit_text(callback.message, "Платёж не найден.", reply_markup=admin_subpage_keyboard())
            return
        if str(row["status"] or "") != "pending":
            await safe_edit_text(
                callback.message,
                admin_payment_detail_text(row, tariff_title_or_key(str(row["tarif_key"]))),
                reply_markup=admin_payment_detail_keyboard(row),
            )
            return
        try:
            result_text = await apply_payment_without_payment(callback.bot, row, int(callback.from_user.id))
        except Exception as exc:
            await safe_edit_text(
                callback.message,
                "❌ Не удалось применить платёж без оплаты.\n\n" + str(exc)[:700],
                reply_markup=admin_payment_detail_keyboard(row),
            )
            return

        updated = await get_payment(payment_id)
        await safe_edit_text(
            callback.message,
            result_text + "\n\n" + admin_payment_detail_text(updated, tariff_title_or_key(str(updated["tarif_key"]))),
            reply_markup=admin_payment_detail_keyboard(updated),
        )
        return


@router.message(F.text.regexp(r"^(#|№)?\d+$"), AdminStates.waiting_payment_search)
@router.message(AdminStates.waiting_payment_search)
async def admin_payment_search_message(message: types.Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    query = (message.text or "").strip()
    if not query:
        await message.answer("Введите #payment_id или TGID.", reply_markup=admin_payment_search_prompt_keyboard())
        return

    rows = await search_admin_payments(query)
    await state.clear()
    if not rows:
        await message.answer("Ничего не найдено.", reply_markup=admin_subpage_keyboard())
        return

    await message.answer(
        f"🔎 Результаты поиска платежей\n\nЗапрос: {query}",
        reply_markup=admin_payments_keyboard(rows, "all"),
    )


@router.callback_query(F.data == "admin:stats")
async def admin_stats_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    stats_text = await build_admin_stats_text()
    await safe_edit_text(callback.message, stats_text, reply_markup=admin_subpage_keyboard())



@router.callback_query(F.data == "admin:direct_message")
async def admin_direct_message_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    await state.clear()
    await state.set_state(AdminStates.waiting_direct_message_target)
    await safe_edit_text(callback.message, direct_message_prompt_target_text(), reply_markup=direct_message_prompt_keyboard())


@router.message(AdminStates.waiting_direct_message_target)
async def admin_direct_message_target_message(message: types.Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    query = (message.text or "").strip()
    if not query:
        await message.answer("Нужно отправить TGID или @username.")
        return

    found = await find_user_for_direct_message(query)
    if not found:
        await message.answer("Пользователь не найден. Попробуйте TGID или @username.")
        return

    await state.set_state(AdminStates.waiting_direct_message_text)
    await state.update_data(
        direct_message_user_id=int(found["user_id"]),
        direct_message_username=str(found.get("username") or ""),
    )
    await message.answer(
        direct_message_prompt_text(int(found["user_id"]), str(found.get("username") or "")),
        reply_markup=direct_message_prompt_keyboard(),
    )


@router.message(AdminStates.waiting_direct_message_text)
async def admin_direct_message_text_message(message: types.Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    text_msg = (message.text or "").strip()
    if not text_msg:
        await message.answer("Нужно отправить текст сообщения.")
        return

    data = await state.get_data()
    user_id = int(data.get("direct_message_user_id") or 0)
    username = str(data.get("direct_message_username") or "")
    if not user_id:
        await state.clear()
        await message.answer("Получатель потерян. Начните заново.", reply_markup=main_menu(message.from_user.id))
        return

    await state.update_data(direct_message_text=text_msg)
    await message.answer(
        direct_message_preview_text(user_id, username, text_msg),
        reply_markup=direct_message_preview_keyboard(),
    )


@router.callback_query(F.data == "admin:direct_message:edit_text")
async def admin_direct_message_edit_text_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    data = await state.get_data()
    user_id = int(data.get("direct_message_user_id") or 0)
    username = str(data.get("direct_message_username") or "")
    if not user_id:
        await state.clear()
        await safe_edit_text(callback.message, admin_panel_text(), reply_markup=admin_panel_keyboard())
        return
    await state.set_state(AdminStates.waiting_direct_message_text)
    await safe_edit_text(
        callback.message,
        direct_message_prompt_text(user_id, username),
        reply_markup=direct_message_prompt_keyboard(),
    )


@router.callback_query(F.data == "admin:direct_message:send")
async def admin_direct_message_send_callback(callback: types.CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    data = await state.get_data()
    user_id = int(data.get("direct_message_user_id") or 0)
    username = str(data.get("direct_message_username") or "")
    text_msg = str(data.get("direct_message_text") or "").strip()
    if not user_id or not text_msg:
        await state.clear()
        await safe_edit_text(callback.message, admin_panel_text(), reply_markup=admin_panel_keyboard())
        return

    try:
        await bot.send_message(user_id, text_msg)
        await state.clear()
        await safe_edit_text(
            callback.message,
            f"📩 Сообщение отправлено.\n\nПолучатель: {user_id}\nUsername: {username or '—'}",
            reply_markup=admin_subpage_keyboard(),
        )
    except Exception as exc:
        await safe_edit_text(
            callback.message,
            "❌ Не удалось отправить сообщение.\n\n" + str(exc)[:500],
            reply_markup=admin_subpage_keyboard(),
        )


@router.callback_query(F.data == "admin:broadcast")
async def admin_broadcast_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    await state.clear()
    await state.set_state(AdminStates.waiting_broadcast_text)
    await safe_edit_text(callback.message, broadcast_prompt_text(), reply_markup=broadcast_prompt_keyboard())


@router.message(AdminStates.waiting_broadcast_text)
async def admin_broadcast_message(message: types.Message, state: FSMContext, bot: Bot) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    if message.photo:
        data = {
            "kind": "photo",
            "photo_file_id": message.photo[-1].file_id,
            "caption": (message.caption or "").strip(),
            "button_text": None,
            "button_url": None,
        }
    else:
        text_msg = (message.text or "").strip()
        if not text_msg:
            await message.answer("Нужно отправить текст или фото с подписью.")
            return
        data = {
            "kind": "text",
            "text": text_msg,
            "button_text": None,
            "button_url": None,
        }

    await state.update_data(broadcast=data)
    await send_broadcast_preview(message, data)


@router.callback_query(F.data == "admin:broadcast:add_button")
async def admin_broadcast_add_button_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    data = await state.get_data()
    if "broadcast" not in data:
        await safe_edit_text(callback.message, "Нет черновика рассылки.", reply_markup=admin_subpage_keyboard())
        return
    await state.set_state(AdminStates.waiting_broadcast_button)
    await safe_edit_text(callback.message, broadcast_button_prompt_text(), reply_markup=broadcast_prompt_keyboard())


@router.message(AdminStates.waiting_broadcast_button)
async def admin_broadcast_button_message(message: types.Message, state: FSMContext) -> None:
    data = await state.get_data()
    if "broadcast" not in data:
        await state.clear()
        await message.answer("Черновик рассылки не найден.", reply_markup=admin_subpage_keyboard())
        return

    raw = (message.text or "").strip()
    if "|" not in raw:
        await message.answer("Неверный формат. Используйте: Текст кнопки | https://example.com")
        return
    button_text, button_url = [x.strip() for x in raw.split("|", 1)]
    if not button_text or not button_url or not (button_url.startswith("http://") or button_url.startswith("https://")):
        await message.answer("Неверный формат ссылки. Нужен полный URL, начинающийся с http:// или https://")
        return

    broadcast = dict(data["broadcast"])
    broadcast["button_text"] = button_text[:64]
    broadcast["button_url"] = button_url
    await state.update_data(broadcast=broadcast)
    await state.set_state(AdminStates.waiting_broadcast_text)
    await send_broadcast_preview(message, broadcast)


@router.callback_query(F.data == "admin:broadcast:clear_button")
async def admin_broadcast_clear_button_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    data = await state.get_data()
    if "broadcast" not in data:
        await safe_edit_text(callback.message, "Нет черновика рассылки.", reply_markup=admin_subpage_keyboard())
        return
    broadcast = dict(data["broadcast"])
    broadcast["button_text"] = None
    broadcast["button_url"] = None
    await state.update_data(broadcast=broadcast)
    await safe_edit_text(
        callback.message,
        broadcast_preview_text(has_media=(broadcast.get("kind") == "photo"), has_button=False),
        reply_markup=broadcast_preview_keyboard(has_button=False),
    )


@router.callback_query(F.data == "admin:broadcast:send")
async def admin_broadcast_send_callback(callback: types.CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await callback.answer("Запускаю рассылку…")
    if not is_admin(callback.from_user.id):
        return
    data = await state.get_data()
    if "broadcast" not in data:
        await safe_edit_text(callback.message, "Нет черновика рассылки.", reply_markup=admin_subpage_keyboard())
        return

    sent, failed, skipped = await execute_broadcast(bot, data["broadcast"])
    await state.clear()
    await safe_edit_text(callback.message, broadcast_result_text(sent, failed, skipped), reply_markup=admin_subpage_keyboard())



async def render_support_admin_list(callback: types.CallbackQuery, status_filter: str = "all", page: int = 1) -> None:
    rows = await list_admin_tickets_filtered(status_filter)
    title = support_admin_list_text()
    if status_filter != "all":
        labels = {"open": "Открытые", "answered": "Отвечённые", "closed": "Закрытые"}
        title += f"\n\nФильтр: {labels.get(status_filter, status_filter)}"
    await safe_edit_text(callback.message, title, reply_markup=support_admin_tickets_keyboard(rows, status_filter, page=page))

@router.callback_query(F.data == "support:admin:list")
async def support_admin_list_root_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    if not is_admin(callback.from_user.id):
        return
    await render_support_admin_list(callback, "all", 1)


@router.callback_query(F.data.startswith("support:admin:list:"))
async def support_admin_list_filtered_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    if not is_admin(callback.from_user.id):
        return
    parts = callback.data.split(":")
    status_filter = parts[3] if len(parts) >= 4 else "all"
    try:
        page = int(parts[4]) if len(parts) >= 5 else 1
    except Exception:
        page = 1
    if status_filter not in ("all", "open", "answered", "closed"):
        status_filter = "all"
    await render_support_admin_list(callback, status_filter, page)


@router.callback_query(F.data.startswith("support:quick:"))
async def support_quick_reply_callback(callback: types.CallbackQuery, bot: Bot) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    parts = callback.data.split(":")
    key = parts[2]
    ticket_id = int(parts[3])
    status_filter = parts[4] if len(parts) >= 5 else "all"
    try:
        page = int(parts[5]) if len(parts) >= 6 else 1
    except Exception:
        page = 1
    ticket = await get_ticket(ticket_id)
    if not ticket:
        await safe_edit_text(callback.message, "Тикет не найден.")
        return
    if str(ticket["status"]) == "closed":
        await callback.message.answer("Тикет закрыт. Сначала переоткройте его.")
        return
    reply_text = quick_reply_text(key)
    if not reply_text:
        return
    await add_support_message(ticket_id, callback.from_user.id, "admin", reply_text)
    updated = await get_ticket(ticket_id)
    msgs = await list_ticket_messages(ticket_id, limit=200)
    await safe_edit_text(callback.message, support_ticket_card_text(updated, msgs), reply_markup=admin_ticket_keyboard(updated, status_filter=status_filter, page=page))
    await notify_user_about_admin_reply(bot, int(ticket["user_id"]), ticket_id)

@router.callback_query(F.data.startswith("support:note:"))
async def support_note_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    parts = callback.data.split(":")
    ticket_id = int(parts[2]) if len(parts) >= 3 else int(parts[-1])
    status_filter = parts[3] if len(parts) >= 4 else "all"
    try:
        page = int(parts[4]) if len(parts) >= 5 else 1
    except Exception:
        page = 1
    ticket = await get_ticket(ticket_id)
    if not ticket:
        await safe_edit_text(callback.message, "Тикет не найден.")
        return
    await state.set_state(SupportStates.waiting_admin_note)
    await state.update_data(ticket_id=ticket_id, support_admin_filter=status_filter, support_admin_page=page)
    await safe_edit_text(callback.message, support_note_prompt_text(ticket_id), reply_markup=cancel_inline_keyboard(f"support:admin:view:{ticket_id}:{status_filter}:{page}"))

@router.message(SupportStates.waiting_admin_note)
async def support_admin_note_message(message: types.Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    ticket_id = int(data["ticket_id"])
    ticket = await get_ticket(ticket_id)
    if not ticket:
        await state.clear()
        await message.answer("Тикет не найден.")
        return
    note_text = (message.text or "").strip()
    if not note_text:
        await message.answer("Нужно отправить текст заметки.")
        return
    await add_support_message(ticket_id, message.from_user.id, "note", note_text, is_note=True)
    status_filter = str(data.get("support_admin_filter") or "all")
    page = int(data.get("support_admin_page") or 1)
    await state.clear()
    updated = await get_ticket(ticket_id)
    msgs = await list_ticket_messages(ticket_id, limit=200)
    await message.answer(support_ticket_preview_text(updated, msgs), reply_markup=admin_ticket_keyboard(updated, status_filter=status_filter, page=page))


@router.callback_query(F.data == "support:admin:search")
async def support_admin_search_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    await state.clear()
    await state.set_state(AdminStates.waiting_ticket_search)
    await safe_edit_text(callback.message, support_ticket_search_prompt_text(), reply_markup=admin_search_prompt_keyboard())


@router.message(F.text.regexp(r"^#\d+$"), AdminStates.waiting_ticket_search)
@router.message(AdminStates.waiting_ticket_search)
async def admin_ticket_search_message(message: types.Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    query = (message.text or "").strip()
    if not query:
        await message.answer("Введите #ticket_id или TGID.", reply_markup=admin_search_prompt_keyboard())
        return
    rows = await search_admin_tickets(query)
    await state.clear()
    if not rows:
        await message.answer("Ничего не найдено.", reply_markup=admin_subpage_keyboard())
        return
    await message.answer(
        f"🔎 Результаты поиска\n\nЗапрос: {query}",
        reply_markup=support_admin_tickets_keyboard(rows, "all", page=1),
    )


@router.callback_query(F.data.startswith("support:admin:user:"))
async def support_admin_user_profile_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    parts = callback.data.split(":")
    user_id = int(parts[3]) if len(parts) >= 4 else int(parts[-1])
    ticket_id = int(parts[4]) if len(parts) >= 5 else 0
    status_filter = parts[5] if len(parts) >= 6 else "all"
    try:
        page = int(parts[6]) if len(parts) >= 7 else 1
    except Exception:
        page = 1

    stats = await get_user_profile_stats(user_id)
    try:
        subs = await get_user_subscriptions(user_id)
    except Exception:
        subs = []
    active_subscriptions = len([x for x in subs if int(x["days_left"]) > 0])
    latest_plans = []
    for item in subs[:5]:
        plan = str(item.get("plan_key") or "без плана")
        if plan not in latest_plans:
            latest_plans.append(plan)
    text = admin_user_profile_text(
        user_id=user_id,
        username=stats.get("username", "—"),
        payments_total=stats["payments_total"],
        payments_success=stats["payments_success"],
        tickets_total=stats["tickets_total"],
        active_subscriptions=active_subscriptions,
        latest_plans=latest_plans,
    )
    back = f"support:admin:view:{ticket_id}:{status_filter}:{page}" if ticket_id else f"support:admin:list:{status_filter}:{page}"
    await safe_edit_text(callback.message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← К тикету", callback_data=back)]]))


@router.callback_query(F.data == "admin:tariffs")
async def admin_tariffs_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    if not is_admin(callback.from_user.id):
        return
    await safe_edit_text(callback.message, tariffs_admin_list_text(), reply_markup=tariff_admin_list_keyboard())


@router.callback_query(F.data.startswith("admin:tariff:view:"))
async def admin_tariff_view_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    tariff_key = callback.data.split(":")[-1]
    tariff = get_tariff(tariff_key)
    if not tariff:
        await safe_edit_text(callback.message, "Тариф не найден.", reply_markup=admin_subpage_keyboard())
        return
    await safe_edit_text(callback.message, tariff_admin_detail_text(tariff), reply_markup=tariff_admin_detail_keyboard(tariff))


@router.callback_query(F.data.startswith("admin:tariff:toggle_enabled:"))
async def admin_tariff_toggle_enabled_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    tariff_key = callback.data.split(":")[-1]
    current = tariff_enabled(tariff_key)
    await upsert_tariff_override(tariff_key, enabled=0 if current else 1)
    tariff = get_tariff(tariff_key)
    await safe_edit_text(callback.message, tariff_admin_detail_text(tariff), reply_markup=tariff_admin_detail_keyboard(tariff))


@router.callback_query(F.data.startswith("admin:tariff:toggle_admin:"))
async def admin_tariff_toggle_admin_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    tariff_key = callback.data.split(":")[-1]
    tariff = get_tariff(tariff_key)
    if not tariff:
        await safe_edit_text(callback.message, "Тариф не найден.", reply_markup=admin_subpage_keyboard())
        return
    new_value = 0 if tariff.is_admin_only else 1
    await upsert_tariff_override(tariff_key, is_admin_only=new_value)
    tariff = get_tariff(tariff_key)
    await safe_edit_text(callback.message, tariff_admin_detail_text(tariff), reply_markup=tariff_admin_detail_keyboard(tariff))



@router.callback_query(F.data == "admin:tariff:create")
async def admin_tariff_create_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    await state.clear()
    await state.set_state(AdminStates.waiting_tariff_create)
    await state.update_data(tariff_create_step="key", tariff_create_data={})
    await safe_edit_text(callback.message, tariff_create_prompt_text("key"), reply_markup=tariff_create_prompt_keyboard())


@router.callback_query(F.data.startswith("admin:tariff:delete:"))
async def admin_tariff_delete_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    tariff_key = callback.data.split(":")[-1]
    tariff = get_tariff(tariff_key)
    if not tariff:
        await safe_edit_text(callback.message, "Тариф не найден.", reply_markup=admin_subpage_keyboard())
        return
    if not tariff.is_custom:
        await safe_edit_text(callback.message, "Базовый тариф нельзя удалить. Его можно выключить.", reply_markup=tariff_admin_detail_keyboard(tariff))
        return
    await delete_custom_tariff(tariff_key)
    await safe_edit_text(callback.message, tariffs_admin_list_text(), reply_markup=tariff_admin_list_keyboard())


@router.callback_query(F.data.startswith("admin:tariff:edit:"))
async def admin_tariff_edit_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    parts = callback.data.split(":")
    tariff_key = parts[3]
    field_name = parts[4]
    if field_name not in ("title", "days", "traffic_gb", "stars_price", "connection_limit", "stars_purchase_url"):
        await safe_edit_text(callback.message, "Поле не поддерживается.", reply_markup=admin_subpage_keyboard())
        return
    await state.clear()
    await state.set_state(AdminStates.waiting_tariff_edit)
    await state.update_data(tariff_key=tariff_key, field_name=field_name)
    await safe_edit_text(
        callback.message,
        tariff_edit_prompt_text(tariff_key, field_name),
        reply_markup=tariff_edit_prompt_keyboard(tariff_key),
    )



@router.message(AdminStates.waiting_tariff_create)
async def admin_tariff_create_message(message: types.Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    step = str(data.get("tariff_create_step") or "key")
    payload = dict(data.get("tariff_create_data") or {})
    raw = (message.text or "").strip()

    try:
        if step == "key":
            key = re.sub(r"[^a-z0-9_]+", "_", raw.lower()).strip("_")
            if not key:
                raise ValueError("Ключ не может быть пустым.")
            if get_tariff(key) or get_tariff_override(key):
                raise ValueError("Тариф с таким ключом уже существует.")
            payload["key"] = key
            next_step = "title"
        elif step == "title":
            if not raw:
                raise ValueError("Название не может быть пустым.")
            payload["title"] = raw[:128]
            next_step = "days"
        elif step == "days":
            value = int(raw)
            if value <= 0:
                raise ValueError("Срок должен быть больше 0.")
            payload["days"] = value
            next_step = "traffic_gb"
        elif step == "traffic_gb":
            if raw in ("∞", "inf", "INF", "unlimited", "UNLIMITED"):
                payload["traffic_gb"] = None
            else:
                value = int(raw)
                if value <= 0:
                    raise ValueError("Трафик должен быть > 0 или ∞.")
                payload["traffic_gb"] = value
            next_step = "stars_price"
        elif step == "stars_price":
            value = int(raw)
            if value <= 0:
                raise ValueError("Цена должна быть > 0.")
            payload["stars_price"] = value
            next_step = "is_admin_only"
        elif step == "is_admin_only":
            norm = raw.lower()
            if norm in ("public", "публичный", "pub", "0"):
                payload["is_admin_only"] = False
            elif norm in ("admin", "админ", "admin-only", "1"):
                payload["is_admin_only"] = True
            else:
                raise ValueError("Введите public или admin.")
            next_step = "connection_limit"
        elif step == "connection_limit":
            value = int(raw)
            if value <= 0:
                raise ValueError("Лимит должен быть > 0.")
            payload["connection_limit"] = value
            next_step = "stars_purchase_url"
        elif step == "stars_purchase_url":
            normalized_url = normalize_stars_purchase_url(raw)
            if raw and normalized_url is None and not is_resettable_stars_url_input(raw):
                raise ValueError("Некорректная ссылка. Используйте https://..., t.me/... или @botname, либо - для пропуска.")
            payload["stars_purchase_url"] = normalized_url
            await create_custom_tariff(
                key=payload["key"],
                title=payload["title"],
                days=int(payload["days"]),
                traffic_gb=payload.get("traffic_gb"),
                stars_price=int(payload["stars_price"]),
                is_admin_only=bool(payload["is_admin_only"]),
                connection_limit=int(payload["connection_limit"]),
                stars_purchase_url=payload.get("stars_purchase_url"),
            )
            await state.clear()
            tariff = get_tariff(payload["key"])
            await message.answer(tariff_admin_detail_text(tariff), reply_markup=tariff_admin_detail_keyboard(tariff))
            return
        else:
            raise ValueError("Неизвестный шаг создания тарифа.")
    except Exception as exc:
        await message.answer(f"Ошибка: {exc}", reply_markup=tariff_create_prompt_keyboard())
        return

    await state.update_data(tariff_create_step=next_step, tariff_create_data=payload)
    await message.answer(tariff_create_prompt_text(next_step), reply_markup=tariff_create_prompt_keyboard())


@router.message(AdminStates.waiting_tariff_edit)
async def admin_tariff_edit_message(message: types.Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    tariff_key = data.get("tariff_key")
    field_name = data.get("field_name")
    if not tariff_key or not field_name:
        await state.clear()
        await message.answer("Сессия редактирования потеряна.", reply_markup=admin_subpage_keyboard())
        return

    raw = (message.text or "").strip()
    patch = {}
    try:
        if field_name == "title":
            if not raw:
                raise ValueError("Название не может быть пустым.")
            patch["title"] = raw[:128]
        elif field_name == "days":
            value = int(raw)
            if value <= 0:
                raise ValueError("Срок должен быть больше 0.")
            patch["days"] = value
        elif field_name == "traffic_gb":
            if raw in ("∞", "inf", "INF", "unlimited", "UNLIMITED"):
                patch["traffic_gb"] = -1
            else:
                value = int(raw)
                if value <= 0:
                    raise ValueError("Трафик должен быть > 0 или ∞.")
                patch["traffic_gb"] = value
        elif field_name == "stars_price":
            value = int(raw)
            if value <= 0:
                raise ValueError("Цена должна быть > 0.")
            patch["stars_price"] = value
        elif field_name == "connection_limit":
            value = int(raw)
            if value <= 0:
                raise ValueError("Лимит должен быть > 0.")
            patch["connection_limit"] = value
        elif field_name == "stars_purchase_url":
            normalized_url = normalize_stars_purchase_url(raw)
            if raw and normalized_url is None and not is_resettable_stars_url_input(raw):
                raise ValueError("Некорректная ссылка. Используйте https://..., t.me/... или @botname, либо - для сброса.")
            patch["stars_purchase_url"] = normalized_url
    except Exception as exc:
        await message.answer(f"Ошибка: {exc}", reply_markup=tariff_edit_prompt_keyboard(tariff_key))
        return

    await upsert_tariff_override(tariff_key, **patch)
    await state.clear()
    tariff = get_tariff(tariff_key)
    await message.answer(tariff_admin_detail_text(tariff), reply_markup=tariff_admin_detail_keyboard(tariff))


@router.message(Command("antiabuse_status"))
async def antiabuse_status_command(message: types.Message) -> None:
    if not is_admin(message.from_user.id):
        return
    cases = await antiabuse_build_cases(force_refresh=True)
    suspicious = [x for x in cases if x["suspicious"]]
    async with aiosqlite.connect(ANTIABUSE_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT COUNT(*) AS c FROM raw_events") as cur:
            raw_row = await cur.fetchone()
        async with conn.execute("SELECT COUNT(*) AS c FROM matches") as cur:
            match_row = await cur.fetchone()
    lines = [
        "🛡 Antiabuse статус",
        "",
        f"Включён: {'да' if ANTIABUSE_ENABLED else 'нет'}",
        f"Базовый лимит за {ANTIABUSE_WINDOW_MINUTES} мин.: {ANTIABUSE_IP_LIMIT}",
        f"Уведомлять пользователя: {'да' if ANTIABUSE_NOTIFY_USER else 'нет'}",
        f"Raw events: {int(raw_row['c'] or 0) if raw_row else 0}",
        f"Matches: {int(match_row['c'] or 0) if match_row else 0}",
    ]
    if suspicious:
        lines += ["", "Подозрительные подписки:"]
        for item in suspicious[:20]:
            lines.append(f"• {item['name']} — {item['unique_window']} уникальных (лимит {item['limit_value']})")
    else:
        lines += ["", "Сейчас подозрительных подписок нет."]
    await message.answer("\n".join(lines))


@router.callback_query(F.data == "antiabuse:report")
async def antiabuse_report_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    if is_admin(callback.from_user.id):
        await render_antiabuse_list(callback, status="suspicious")


@router.callback_query(F.data == "antiabuse:all")
async def antiabuse_all_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    if is_admin(callback.from_user.id):
        await render_antiabuse_list(callback, status="all")


@router.callback_query(F.data == "antiabuse:check")
async def antiabuse_check_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    if is_admin(callback.from_user.id):
        await render_antiabuse_list(callback, status="check")


@router.callback_query(F.data == "antiabuse:ignore")
async def antiabuse_ignore_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    if is_admin(callback.from_user.id):
        await render_antiabuse_list(callback, status="ignore")


@router.callback_query(F.data.startswith("antiabuse:refresh:"))
async def antiabuse_refresh_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    parts = callback.data.split(":")
    mode = parts[2] if len(parts) >= 3 else "suspicious"
    try:
        page = int(parts[3]) if len(parts) >= 4 else 1
    except Exception:
        page = 1
    if mode not in ("all", "suspicious", "check", "ok", "ignore"):
        mode = "suspicious"
    await render_antiabuse_list(callback, status=mode, page=page)


@router.callback_query(F.data.startswith("antiabuse:page:"))
async def antiabuse_page_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    parts = callback.data.split(":")
    if len(parts) != 4:
        await safe_edit_text(callback.message, "Некорректный callback пагинации antiabuse.", reply_markup=admin_subpage_keyboard())
        return
    _, _, mode, page_raw = parts
    try:
        page = int(page_raw)
    except Exception:
        page = 1
    if mode not in ("all", "suspicious", "check", "ok", "ignore"):
        mode = "suspicious"
    await render_antiabuse_list(callback, status=mode, page=page)

@router.callback_query(F.data.startswith("antiabuse:view:"))
async def antiabuse_view_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    parts = callback.data.split(":")
    if len(parts) != 4:
        await safe_edit_text(callback.message, "Некорректный callback кейса antiabuse.", reply_markup=admin_subpage_keyboard())
        return
    _, _, case_id_raw, mode = parts
    try:
        case_id = int(case_id_raw)
    except Exception:
        await safe_edit_text(callback.message, "Некорректный case_id antiabuse.", reply_markup=admin_subpage_keyboard())
        return
    await render_antiabuse_case(callback, case_id=case_id, mode=mode)


@router.callback_query(F.data.startswith("antiabuse:mark:"))
async def antiabuse_mark_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    parts = callback.data.split(":")
    if len(parts) != 5:
        await safe_edit_text(callback.message, "Некорректный callback antiabuse.", reply_markup=admin_subpage_keyboard())
        return
    _, _, status, case_id_raw, mode = parts
    try:
        case_id = int(case_id_raw)
    except Exception:
        await safe_edit_text(callback.message, "Некорректный case_id antiabuse.", reply_markup=admin_subpage_keyboard())
        return

    case = await antiabuse_get_case_by_id(case_id)
    if not case or not case["name"]:
        await safe_edit_text(callback.message, "Кейс antiabuse не найден.", reply_markup=admin_subpage_keyboard())
        return

    await antiabuse_set_status(str(case["name"]), status)
    await render_antiabuse_case(callback, case_id=case_id, mode=mode)


@router.callback_query(F.data.startswith("antiabuse:note:"))
async def antiabuse_note_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    parts = callback.data.split(":")
    if len(parts) != 4:
        await safe_edit_text(callback.message, "Некорректный callback заметки.", reply_markup=admin_subpage_keyboard())
        return
    _, _, case_id_raw, mode = parts
    try:
        case_id = int(case_id_raw)
    except Exception:
        await safe_edit_text(callback.message, "Некорректный case_id заметки.", reply_markup=admin_subpage_keyboard())
        return
    await state.set_state(AdminStates.waiting_antiabuse_note)
    await state.update_data(antiabuse_case_id=case_id, antiabuse_mode=mode)
    await safe_edit_text(callback.message, antiabuse_note_prompt_text(), reply_markup=cancel_inline_keyboard(f"antiabuse:view:{case_id}:{mode}"))


@router.message(AdminStates.waiting_antiabuse_note)
async def antiabuse_note_message(message: types.Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    case_id = int(data["antiabuse_case_id"])
    mode = str(data.get("antiabuse_mode") or "suspicious")
    note = (message.text or "").strip()
    if not note:
        await message.answer("Нужно отправить текст заметки.")
        return
    case = await antiabuse_get_case_by_id(case_id)
    if not case or not case["name"]:
        await state.clear()
        await message.answer("Кейс antiabuse не найден.", reply_markup=admin_subpage_keyboard())
        return
    await antiabuse_set_note(str(case["name"]), note)
    await state.clear()
    cases = await antiabuse_build_cases(force_refresh=True)
    item = next((x for x in cases if int(x["case_id"]) == int(case_id)), None)
    if not item:
        await message.answer("Кейс antiabuse не найден.", reply_markup=admin_subpage_keyboard())
        return
    await message.answer(
        antiabuse_case_detail_text(item),
        reply_markup=antiabuse_detail_keyboard(int(case_id), mode),
    )


@router.callback_query(F.data.startswith("antiabuse:limit:"))
async def antiabuse_limit_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    _, _, case_id_raw, mode = callback.data.split(":", 3)
    case = await antiabuse_get_case_by_id(int(case_id_raw))
    if not case or not case["name"]:
        await safe_edit_text(callback.message, "Кейс antiabuse не найден.", reply_markup=admin_subpage_keyboard())
        return
    await state.set_state(AdminStates.waiting_antiabuse_limit)
    await state.update_data(antiabuse_case_id=int(case_id_raw), antiabuse_mode=mode)
    await safe_edit_text(
        callback.message,
        f"✏️ Новый лимит IP\n\nName: {case['name']}\n\nОтправьте числом новый лимит для этой подписки.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отменить", callback_data=f"antiabuse:view:{case_id_raw}:{mode}")]]),
    )


@router.message(AdminStates.waiting_antiabuse_limit)
async def antiabuse_limit_message(message: types.Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    case_id = int(data["antiabuse_case_id"])
    mode = str(data.get("antiabuse_mode") or "suspicious")
    raw = (message.text or "").strip()
    try:
        limit_value = int(raw)
        if limit_value < 1 or limit_value > 500:
            raise ValueError
    except Exception:
        await message.answer("Введите целое число от 1 до 500.")
        return
    case = await antiabuse_get_case_by_id(case_id)
    if not case or not case["name"]:
        await state.clear()
        await message.answer("Кейс antiabuse не найден.", reply_markup=admin_subpage_keyboard())
        return
    await antiabuse_set_limit_override(str(case["name"]), limit_value)
    await state.clear()
    cases = await antiabuse_build_cases(force_refresh=True)
    item = next((x for x in cases if int(x["case_id"]) == int(case_id)), None)
    if not item:
        await message.answer("Кейс antiabuse не найден.", reply_markup=admin_subpage_keyboard())
        return
    await message.answer(
        antiabuse_case_detail_text(item),
        reply_markup=antiabuse_detail_keyboard(int(case_id), mode),
    )


@router.callback_query(F.data.startswith("antiabuse:limit_reset:"))
async def antiabuse_limit_reset_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    parts = callback.data.split(":")
    if len(parts) != 4:
        await safe_edit_text(callback.message, "Некорректный callback сброса лимита.", reply_markup=admin_subpage_keyboard())
        return
    _, _, case_id_raw, mode = parts
    try:
        case_id = int(case_id_raw)
    except Exception:
        await safe_edit_text(callback.message, "Некорректный case_id лимита.", reply_markup=admin_subpage_keyboard())
        return
    row = await antiabuse_get_case_by_id(case_id)
    if not row:
        await safe_edit_text(callback.message, "Кейс antiabuse не найден.", reply_markup=admin_subpage_keyboard())
        return
    await antiabuse_clear_limit_override(str(row["name"] or ""))
    await render_antiabuse_case(callback, case_id=case_id, mode=mode)

@router.callback_query(F.data.startswith("support:admin:view:"))
async def support_admin_view_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return

    parts = callback.data.split(":")
    ticket_id = int(parts[3]) if len(parts) >= 4 else int(parts[-1])
    status_filter = parts[4] if len(parts) >= 5 else "all"
    try:
        page = int(parts[5]) if len(parts) >= 6 else 1
    except Exception:
        page = 1

    ticket = await get_ticket(ticket_id)
    if not ticket:
        await safe_edit_text(callback.message, "Тикет не найден.")
        return

    msgs = await list_ticket_messages(ticket_id, limit=200)
    await safe_edit_text(
        callback.message,
        support_ticket_card_text(ticket, msgs),
        reply_markup=admin_ticket_keyboard(ticket, status_filter=status_filter, page=page),
    )


@router.callback_query(F.data == "analytics:panel")
async def analytics_panel_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    await safe_edit_text(callback.message, analytics_panel_text(), reply_markup=analytics_panel_keyboard())


@router.callback_query(F.data == "analytics:overview")
async def analytics_overview_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    stats = await analytics_overview_stats()
    await safe_edit_text(callback.message, analytics_overview_text(stats), reply_markup=analytics_subpage_keyboard())


@router.callback_query(F.data == "analytics:payments")
async def analytics_payments_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    stats = await analytics_payments_stats()
    await safe_edit_text(callback.message, analytics_payments_text(stats), reply_markup=analytics_subpage_keyboard())


@router.callback_query(F.data == "analytics:support")
async def analytics_support_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    stats = await analytics_support_stats()
    await safe_edit_text(callback.message, analytics_support_text(stats), reply_markup=analytics_subpage_keyboard())


@router.callback_query(F.data == "analytics:antiabuse")
async def analytics_antiabuse_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    stats = await analytics_antiabuse_stats()
    await safe_edit_text(callback.message, analytics_antiabuse_text(stats), reply_markup=analytics_subpage_keyboard())


@router.callback_query(F.data == "analytics:tariffs")
async def analytics_tariffs_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    stats = await analytics_tariffs_stats()
    await safe_edit_text(callback.message, analytics_tariffs_text(stats), reply_markup=analytics_subpage_keyboard())


@router.callback_query(F.data == "reminders:panel")
async def reminders_panel_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    text = await build_reminders_panel_text()
    await safe_edit_text(callback.message, text, reply_markup=reminders_panel_keyboard())


@router.callback_query(F.data == "reminders:templates")
async def reminders_templates_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    await safe_edit_text(callback.message, reminders_templates_text(), reply_markup=reminders_templates_keyboard())


@router.callback_query(F.data == "reminders:run_now")
async def reminders_run_now_callback(callback: types.CallbackQuery, bot: Bot) -> None:
    await callback.answer("Запускаю проверку...")
    if not is_admin(callback.from_user.id):
        return
    await run_reminder_check(bot)
    text = await build_reminders_panel_text()
    await safe_edit_text(callback.message, text, reply_markup=reminders_panel_keyboard())


@router.callback_query(F.data.startswith("antiabuse:disable_custom:"))
async def antiabuse_disable_custom_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    parts = callback.data.split(":")
    if len(parts) != 4:
        await safe_edit_text(callback.message, "Некорректный callback отключения.", reply_markup=admin_subpage_keyboard())
        return
    _, _, case_id_raw, mode = parts
    try:
        case_id = int(case_id_raw)
    except Exception:
        await safe_edit_text(callback.message, "Некорректный case_id antiabuse.", reply_markup=admin_subpage_keyboard())
        return
    case = await antiabuse_get_case_by_id(case_id)
    if not case or not case["name"]:
        await safe_edit_text(callback.message, "Кейс antiabuse не найден.", reply_markup=admin_subpage_keyboard())
        return
    await state.set_state(AdminStates.waiting_antiabuse_disable)
    await state.update_data(antiabuse_case_id=case_id, antiabuse_mode=mode)
    await safe_edit_text(
        callback.message,
        antiabuse_disable_prompt_text(str(case["name"])),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отменить", callback_data=f"antiabuse:view:{case_id}:{mode}")]]),
    )


@router.callback_query(F.data.startswith("antiabuse:disable:"))
async def antiabuse_disable_callback(callback: types.CallbackQuery, bot: Bot) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    parts = callback.data.split(":")
    if len(parts) != 5:
        await safe_edit_text(callback.message, "Некорректный callback отключения.", reply_markup=admin_subpage_keyboard())
        return
    _, _, case_id_raw, mode, minutes_raw = parts
    try:
        case_id = int(case_id_raw)
        minutes = int(minutes_raw)
    except Exception:
        await safe_edit_text(callback.message, "Некорректные параметры отключения.", reply_markup=admin_subpage_keyboard())
        return
    case = await antiabuse_get_case_by_id(case_id)
    if not case or not case["name"]:
        await safe_edit_text(callback.message, "Кейс antiabuse не найден.", reply_markup=admin_subpage_keyboard())
        return
    try:
        case_name = str(case["name"])
        await antiabuse_disable_for_minutes(case_name, minutes, callback.from_user.id, "manual antiabuse disable")
        await antiabuse_send_user_state_message(bot, case_name, "disabled", minutes)
    except Exception as exc:
        await safe_edit_text(callback.message, "❌ Не удалось отключить подписку.\n\n" + str(exc)[:700], reply_markup=admin_subpage_keyboard())
        return
    await render_antiabuse_case(callback, case_id=case_id, mode=mode)


@router.message(AdminStates.waiting_antiabuse_disable)
async def antiabuse_disable_message(message: types.Message, state: FSMContext, bot: Bot) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    raw = (message.text or "").strip()
    try:
        minutes = int(raw)
        if minutes < 1 or minutes > ANTIABUSE_DISABLE_MAX_MINUTES:
            raise ValueError
    except Exception:
        await message.answer(f"Введите целое число от 1 до {ANTIABUSE_DISABLE_MAX_MINUTES}.")
        return

    data = await state.get_data()
    case_id = int(data["antiabuse_case_id"])
    mode = str(data.get("antiabuse_mode") or "suspicious")
    case = await antiabuse_get_case_by_id(case_id)
    if not case or not case["name"]:
        await state.clear()
        await message.answer("Кейс antiabuse не найден.", reply_markup=admin_subpage_keyboard())
        return

    case_name = str(case["name"])
    try:
        await antiabuse_disable_for_minutes(case_name, minutes, message.from_user.id, "manual antiabuse disable")
        try:
            await antiabuse_send_user_state_message(bot, case_name, "disabled", minutes=minutes)
        except Exception:
            pass
    except Exception as exc:
        await state.clear()
        await message.answer("❌ Не удалось отключить подписку.\n\n" + str(exc)[:700], reply_markup=admin_subpage_keyboard())
        return

    await state.clear()
    cases = await antiabuse_build_cases(force_refresh=True)
    item = next((x for x in cases if int(x["case_id"]) == int(case_id)), None)
    if not item:
        await message.answer("Кейс antiabuse не найден.", reply_markup=admin_subpage_keyboard())
        return
    await message.answer(
        antiabuse_case_detail_text(item),
        reply_markup=antiabuse_detail_keyboard(int(case_id), mode),
    )


@router.callback_query(F.data.startswith("antiabuse:enable:"))
async def antiabuse_enable_callback(callback: types.CallbackQuery, bot: Bot) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    parts = callback.data.split(":")
    if len(parts) != 4:
        await safe_edit_text(callback.message, "Некорректный callback включения.", reply_markup=admin_subpage_keyboard())
        return
    _, _, case_id_raw, mode = parts
    try:
        case_id = int(case_id_raw)
    except Exception:
        await safe_edit_text(callback.message, "Некорректный case_id antiabuse.", reply_markup=admin_subpage_keyboard())
        return
    case = await antiabuse_get_case_by_id(case_id)
    if not case or not case["name"]:
        await safe_edit_text(callback.message, "Кейс antiabuse не найден.", reply_markup=admin_subpage_keyboard())
        return
    try:
        case_name = str(case["name"])
        await antiabuse_enable_now(case_name, callback.from_user.id, "manual enable")
        await antiabuse_send_user_state_message(bot, case_name, "enabled")
    except Exception as exc:
        await safe_edit_text(callback.message, "❌ Не удалось включить подписку.\n\n" + str(exc)[:700], reply_markup=admin_subpage_keyboard())
        return
    await render_antiabuse_case(callback, case_id=case_id, mode=mode)


# =========================
# MAIN
# =========================

async def main() -> None:
    setup_logging()
    write_health_snapshot(status="starting")
    await log_runtime_audit()
    await init_payments_db()
    await init_trials_db()
    await init_support_db()
    await init_reminders_db()
    await init_antiabuse_db()
    await init_tariffs_db()
    await init_tgproxy_db()

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    reminder_task = asyncio.create_task(reminder_loop(bot))
    antiabuse_task = asyncio.create_task(antiabuse_worker(bot))
    health_task = asyncio.create_task(health_loop())
    backup_task = asyncio.create_task(backup_loop())
    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        write_health_snapshot(status="stopping")
        reminder_task.cancel()
        antiabuse_task.cancel()
        health_task.cancel()
        backup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reminder_task
        with contextlib.suppress(asyncio.CancelledError):
            await antiabuse_task
        with contextlib.suppress(asyncio.CancelledError):
            await health_task
        with contextlib.suppress(asyncio.CancelledError):
            await backup_task
        await close_http_session()
        await bot.session.close()
        write_health_snapshot(status="stopped")


if __name__ == "__main__":
    asyncio.run(main())