from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel
from fastapi.responses import JSONResponse, PlainTextResponse
from typing import Optional
import logging
import sqlite3
import math
from datetime import datetime, timedelta, timezone
import uuid as pyuuid
from zoneinfo import ZoneInfo
import json
import os
import secrets

app = FastAPI()
DB_PATH = os.environ.get("DB_PATH", "/app/data/mdragons.db")
API_KEY = os.environ.get("API_KEY", "").strip()
if not API_KEY:
    raise RuntimeError("API_KEY must be set before starting the FastAPI backend. Refusing to run an unauthenticated API.")
API_KEY_HEADER = "X-API-Key"
PUBLIC_API_PATHS = {"/api/health"}
GERMAN_TZ = ZoneInfo("Europe/Berlin")
logger = logging.getLogger("mdragons.app")

# ─── System accounts for special mechanics ───────────────────────────────────
SYSTEM_MM_UUID = "SYSTEM_MANSA_MUSA"
SYSTEM_NO_UUID = "SYSTEM_NETHERITE_OVERLORD"
DRAGON_ACCOUNT_PREFIX = "DISCORD_"

# Price curve factors
MM_FLOOR_FACTOR = 0.55
MM_TARGET_FACTOR = 1.05
NO_CEIL_FACTOR = 1.45
NO_TARGET_FACTOR = 0.95

# Chairman settings
DEFAULT_WEEKLY_TARGET = 250_000.0
MIN_WEEKLY_TARGET = 50_000.0
MAX_WEEKLY_TARGET = 500_000.0
TARGET_STEP = 50_000.0

# Reset: Saturday 21:00 German time
RESET_WEEKDAY = 5
RESET_HOUR = 21
WEEKLY_LOGIN_REWARD = 0


# ─────────────────────────────────────────────────────────────────
# VALID COMMODITIES
# ─────────────────────────────────────────────────────────────────
VALID_COMMODITIES = {
    "coal", "iron", "gold", "copper", "emerald", "redstone", "lapis",
    "stone", "cobblestone", "deepslate", "blackstone", "basalt",
    "overworld_log", "nether_log",
    "wheat", "carrot", "potato", "beetroot", "pumpkin", "melon",
    "sugar_cane", "bamboo", "cactus", "cocoa_bean",
    "rotten_flesh", "bone", "string", "gunpowder", "spider_eye",
    "ender_pearl", "slime_ball", "leather", "arrow",
    "blaze_rod", "ghast_tear", "magma_cream", "shulker_shell",
    "totem_of_undying", "wither_skeleton_skull", "nether_star",
    "netherrack", "soul_sand", "soul_soil", "nether_brick_block",
    "quartz", "glowstone", "nether_wart",
    "end_stone", "chorus_fruit", "popped_chorus", "dragon_breath",
    "sand", "gravel", "clay", "glass", "obsidian", "ice",
    "wool", "concrete_powder", "concrete",
    "white_dye", "orange_dye", "magenta_dye", "light_blue_dye",
    "yellow_dye", "lime_dye", "pink_dye", "gray_dye", "light_gray_dye",
    "cyan_dye", "purple_dye", "blue_dye", "brown_dye", "green_dye",
    "red_dye", "black_dye",
    "feather", "ink_sac", "glow_ink_sac",
    "xp",
    "dirt",
}


# ─────────────────────────────────────────────────────────────────
# SQLITE HELPERS
# ─────────────────────────────────────────────────────────────────
def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def begin_immediate(conn: sqlite3.Connection):
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")


def format_compact_amount(value: float):
    number = float(value or 0)
    return int(number) if number.is_integer() else round(number, 2)


def backup_sqlite_database(prefix: str) -> dict:
    src = os.path.abspath(DB_PATH)
    if not os.path.exists(src):
        raise HTTPException(500, "Database file does not exist")

    backup_dir = os.path.join(os.path.dirname(src), "backups")
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    dest = os.path.join(backup_dir, f"{prefix}-{stamp}.db")

    source = sqlite3.connect(src, timeout=30)
    target = sqlite3.connect(dest)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()

    return {"path": dest, "bytes": os.path.getsize(dest)}


# ─────────────────────────────────────────────────────────────────
# DB INIT
# ─────────────────────────────────────────────────────────────────
def init_db():
    conn = get_conn()
    c = conn.cursor()

    c.execute("""CREATE TABLE IF NOT EXISTS linked_accounts (
        mc_uuid TEXT PRIMARY KEY,
        discord_id TEXT UNIQUE
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS balances (
        mc_uuid TEXT PRIMARY KEY,
        netherite INTEGER DEFAULT 0,
        diamond INTEGER DEFAULT 0,
        mdragons REAL DEFAULT 0
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS pending_links (
        code TEXT PRIMARY KEY,
        mc_uuid TEXT UNIQUE,
        expires TIMESTAMP
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mc_uuid TEXT,
        item TEXT,
        amount REAL,
        price_per REAL,
        remaining REAL,
        created TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        side TEXT DEFAULT 'sell',
        is_system INTEGER DEFAULT 0
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS trade_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item TEXT,
        buyer_uuid TEXT,
        seller_uuid TEXT,
        amount REAL,
        price_per REAL,
        value REAL,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS order_event_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT NOT NULL,
        order_id INTEGER,
        mc_uuid TEXT,
        item TEXT,
        amount REAL,
        price_per REAL,
        side TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS trade_event_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item TEXT,
        buyer_uuid TEXT,
        seller_uuid TEXT,
        amount REAL,
        price_per REAL,
        value REAL,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS special_mechanics (
        role TEXT PRIMARY KEY,
        current_item TEXT,
        pending_item TEXT,
        announced_at TIMESTAMP,
        activates_at TIMESTAMP,
        side TEXT,
        status TEXT DEFAULT 'inactive',
        order_id INTEGER,
        reference_price REAL,
        weekly_value_filled REAL DEFAULT 0,
        last_reset TIMESTAMP
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS chairman_settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS injection_pause (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        paused INTEGER NOT NULL DEFAULT 0,
        paused_by TEXT,
        paused_at TIMESTAMP
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS purchase_lists (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mc_uuid TEXT NOT NULL,
        name TEXT NOT NULL,
        price REAL NOT NULL,
        items TEXT NOT NULL,
        created TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS commodity_balances (
        mc_uuid TEXT NOT NULL,
        commodity TEXT NOT NULL,
        amount REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (mc_uuid, commodity)
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS deposit_withdraw_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mc_uuid TEXT,
        action TEXT,
        item TEXT,
        amount REAL,
        base_units REAL,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS external_credit_events (
        event_id TEXT PRIMARY KEY,
        mc_uuid TEXT NOT NULL,
        amount INTEGER NOT NULL,
        created TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS refund_events (
        refund_id TEXT PRIMARY KEY,
        mc_uuid TEXT NOT NULL,
        amount REAL NOT NULL,
        created TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS weekly_login_rewards (
        mc_uuid TEXT NOT NULL,
        week_start TEXT NOT NULL,
        amount INTEGER NOT NULL,
        claimed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (mc_uuid, week_start)
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS alive_stats (
        mc_uuid TEXT PRIMARY KEY,
        name TEXT,
        current_seconds REAL NOT NULL DEFAULT 0,
        best_seconds REAL NOT NULL DEFAULT 0,
        deaths INTEGER NOT NULL DEFAULT 0,
        last_report TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS bounties (
        target_uuid TEXT PRIMARY KEY,
        target_name TEXT,
        amount REAL NOT NULL DEFAULT 0,
        updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS bounty_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT NOT NULL,
        issuer_uuid TEXT,
        target_name TEXT,
        target_uuid TEXT,
        killer_name TEXT,
        killer_uuid TEXT,
        amount REAL NOT NULL,
        logged INTEGER NOT NULL DEFAULT 0,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("INSERT OR IGNORE INTO injection_pause (id, paused) VALUES (1, 0)")

    c.execute("SELECT value FROM chairman_settings WHERE key='weekly_target_active'")
    active_row = c.fetchone()
    c.execute("SELECT value FROM chairman_settings WHERE key='weekly_target_pending'")
    pending_row = c.fetchone()
    c.execute("SELECT value FROM chairman_settings WHERE key='weekly_target'")
    legacy_row = c.fetchone()
    seed_target = float(legacy_row[0]) if legacy_row else DEFAULT_WEEKLY_TARGET
    if not active_row:
        c.execute("INSERT OR REPLACE INTO chairman_settings (key, value) VALUES ('weekly_target_active',?)", (str(float(seed_target)),))
    if not pending_row:
        c.execute("INSERT OR REPLACE INTO chairman_settings (key, value) VALUES ('weekly_target_pending',?)", (str(float(seed_target)),))

    conn.commit()

    c.execute("PRAGMA table_info(orders)")
    cols = [col[1] for col in c.fetchall()]
    if "side" not in cols:
        c.execute("ALTER TABLE orders ADD COLUMN side TEXT DEFAULT 'sell'")
        c.execute("UPDATE orders SET side='sell' WHERE side IS NULL OR side=''")
    if "is_system" not in cols:
        c.execute("ALTER TABLE orders ADD COLUMN is_system INTEGER DEFAULT 0")
        c.execute("UPDATE orders SET is_system=0 WHERE is_system IS NULL")

    c.execute("PRAGMA table_info(bounty_events)")
    bounty_cols = [col[1] for col in c.fetchall()]
    if "target_name" not in bounty_cols:
        c.execute("ALTER TABLE bounty_events ADD COLUMN target_name TEXT")
    if "killer_name" not in bounty_cols:
        c.execute("ALTER TABLE bounty_events ADD COLUMN killer_name TEXT")
    if "logged" not in bounty_cols:
        c.execute("ALTER TABLE bounty_events ADD COLUMN logged INTEGER NOT NULL DEFAULT 0")

    migrate_discord_dragon_balances(c)

    conn.commit()
    conn.close()


@app.on_event("startup")
async def startup():
    init_db()


@app.middleware("http")
async def api_key_middleware(request: Request, call_next):
    if request.url.path.startswith("/api/") and request.url.path not in PUBLIC_API_PATHS:
        supplied_key = request.headers.get(API_KEY_HEADER, "")
        if not secrets.compare_digest(supplied_key, API_KEY):
            return JSONResponse(status_code=401, content={"detail": "Invalid API key"})
    return await call_next(request)


# ─────────────────────────────────────────────────────────────────
# TIME HELPERS
# ─────────────────────────────────────────────────────────────────
def get_last_reset_time() -> datetime:
    now = datetime.now(GERMAN_TZ)
    days_back = (now.weekday() - RESET_WEEKDAY) % 7
    candidate = now.replace(hour=RESET_HOUR, minute=0, second=0, microsecond=0) - timedelta(days=days_back)
    if candidate > now:
        candidate -= timedelta(days=7)
    return candidate


def get_next_activation_time() -> datetime:
    now = datetime.now(GERMAN_TZ)
    earliest = now + timedelta(days=7)
    days_until_sat = (RESET_WEEKDAY - earliest.weekday()) % 7
    candidate = earliest.replace(hour=RESET_HOUR, minute=0, second=0, microsecond=0) + timedelta(days=days_until_sat)
    if candidate <= earliest:
        candidate += timedelta(days=7)
    return candidate


# ─────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────
def normalize_item(item: str) -> Optional[str]:
    key = item.upper().replace(" ", "_").replace("-", "_")
    if key == "DIAMOND":
        return "DIAMOND"
    if key in ("NETHERITE", "NETHERITE_INGOT"):
        return "NETHERITE_INGOT"
    if key in ("DAEMON", "DAEMONS", "MD", "MDRAGONS"):
        return "DAEMON"

    lower = item.lower().replace("-", "_").replace(" ", "_")
    if lower in VALID_COMMODITIES:
        return lower
    return None


def normalize_legacy_item(item: str) -> Optional[str]:
    key = item.upper().replace(" ", "_").replace("-", "_")
    if key == "DIAMOND":
        return "DIAMOND"
    if key in ("NETHERITE", "NETHERITE_INGOT"):
        return "NETHERITE_INGOT"
    if key in ("DAEMON", "DAEMONS", "MD", "MDRAGONS"):
        return "DAEMON"
    return None


def is_legacy_item(item_key: str) -> bool:
    return item_key in ("DIAMOND", "NETHERITE_INGOT", "DAEMON")


def item_col(item_key: str) -> str:
    if item_key == "DIAMOND":
        return "diamond"
    if item_key == "NETHERITE_INGOT":
        return "netherite"
    if item_key == "DAEMON":
        return "mdragons"
    raise ValueError(f"Unknown legacy item: {item_key}")


def dragon_account_uuid(discord_id: str) -> str:
    return f"{DRAGON_ACCOUNT_PREFIX}{str(discord_id).strip()}"


def discord_id_from_dragon_account(mc_uuid: str) -> Optional[str]:
    if not str(mc_uuid).startswith(DRAGON_ACCOUNT_PREFIX):
        return None
    discord_id = str(mc_uuid)[len(DRAGON_ACCOUNT_PREFIX):]
    return discord_id if discord_id.isdigit() else None


def dragon_account_for_uuid(c, mc_uuid: str) -> str:
    mc_uuid = str(mc_uuid)
    if mc_uuid.startswith("SYSTEM_") or mc_uuid.startswith(DRAGON_ACCOUNT_PREFIX):
        return mc_uuid
    c.execute("SELECT discord_id FROM linked_accounts WHERE mc_uuid=?", (mc_uuid,))
    row = c.fetchone()
    return dragon_account_uuid(row[0]) if row else mc_uuid


def ensure_balance_row(c, mc_uuid: str):
    c.execute("INSERT OR IGNORE INTO balances (mc_uuid) VALUES (?)", (str(mc_uuid),))


def migrate_discord_dragon_balance(c, mc_uuid: str, discord_id: str):
    mc_uuid = str(mc_uuid)
    if mc_uuid.startswith("SYSTEM_") or mc_uuid.startswith(DRAGON_ACCOUNT_PREFIX):
        return
    account_uuid = dragon_account_uuid(discord_id)
    c.execute("SELECT mdragons FROM balances WHERE mc_uuid=?", (mc_uuid,))
    row = c.fetchone()
    amount = float(row[0]) if row and row[0] else 0.0
    if amount <= 0:
        ensure_balance_row(c, account_uuid)
        return
    ensure_balance_row(c, account_uuid)
    c.execute("UPDATE balances SET mdragons=mdragons+? WHERE mc_uuid=?", (amount, account_uuid))
    c.execute("UPDATE balances SET mdragons=0 WHERE mc_uuid=?", (mc_uuid,))


def migrate_discord_dragon_balances(c):
    c.execute("SELECT mc_uuid, discord_id FROM linked_accounts")
    rows = c.fetchall()
    for mc_uuid, discord_id in rows:
        migrate_discord_dragon_balance(c, mc_uuid, discord_id)
        migrate_discord_item_balances(c, mc_uuid, discord_id)
        c.execute(
            "UPDATE orders SET mc_uuid=? WHERE mc_uuid=? AND (is_system IS NULL OR is_system=0)",
            (dragon_account_uuid(discord_id), mc_uuid),
        )


def get_mdragon_balance(c, owner_uuid: str) -> float:
    account_uuid = dragon_account_for_uuid(c, owner_uuid)
    c.execute("SELECT mdragons FROM balances WHERE mc_uuid=?", (account_uuid,))
    row = c.fetchone()
    return float(row[0]) if row and row[0] else 0.0


def credit_mdragons(c, owner_uuid: str, amount: float) -> str:
    account_uuid = dragon_account_for_uuid(c, owner_uuid)
    ensure_balance_row(c, account_uuid)
    c.execute("UPDATE balances SET mdragons=mdragons+? WHERE mc_uuid=?", (amount, account_uuid))
    return account_uuid


def debit_mdragons(c, owner_uuid: str, amount: float) -> bool:
    account_uuid = dragon_account_for_uuid(c, owner_uuid)
    ensure_balance_row(c, account_uuid)
    c.execute(
        "UPDATE balances SET mdragons=mdragons-? WHERE mc_uuid=? AND mdragons>=?",
        (amount, account_uuid, amount),
    )
    return c.rowcount == 1


def dragon_order_owner_keys(c, owner_uuid: str) -> list[str]:
    owner_uuid = str(owner_uuid)
    account_uuid = dragon_account_for_uuid(c, owner_uuid)
    keys = [account_uuid]
    discord_id = discord_id_from_dragon_account(account_uuid)
    if discord_id:
        c.execute("SELECT mc_uuid FROM linked_accounts WHERE discord_id=?", (discord_id,))
        keys.extend(row[0] for row in c.fetchall())
    if owner_uuid not in keys:
        keys.append(owner_uuid)
    return list(dict.fromkeys(keys))


def linked_mc_for_dragon_account(c, account_uuid: str) -> Optional[str]:
    discord_id = discord_id_from_dragon_account(account_uuid)
    if not discord_id:
        return None
    c.execute("SELECT mc_uuid FROM linked_accounts WHERE discord_id=?", (discord_id,))
    row = c.fetchone()
    return row[0] if row else None


def order_owner_uuid(c, owner_uuid: str) -> str:
    owner_uuid = str(owner_uuid)
    if owner_uuid.startswith("SYSTEM_") or owner_uuid.startswith(DRAGON_ACCOUNT_PREFIX):
        return owner_uuid
    return dragon_account_for_uuid(c, owner_uuid)


def order_owner_keys(c, owner_uuid: str) -> list[str]:
    owner_uuid = str(owner_uuid)
    keys = [order_owner_uuid(c, owner_uuid)]
    if owner_uuid.startswith(DRAGON_ACCOUNT_PREFIX):
        linked_mc = linked_mc_for_dragon_account(c, owner_uuid)
        if linked_mc:
            keys.append(linked_mc)
    elif not owner_uuid.startswith("SYSTEM_"):
        account_uuid = dragon_account_for_uuid(c, owner_uuid)
        if account_uuid != owner_uuid:
            keys.append(owner_uuid)
    return list(dict.fromkeys(keys))


def item_account_for_order_owner(c, owner_uuid: str) -> str:
    owner_uuid = str(owner_uuid)
    if owner_uuid.startswith(DRAGON_ACCOUNT_PREFIX):
        linked_mc = linked_mc_for_dragon_account(c, owner_uuid)
        if linked_mc:
            return linked_mc
    return owner_uuid


def migrate_discord_item_balances(c, mc_uuid: str, discord_id: str):
    account_uuid = dragon_account_uuid(discord_id)
    if account_uuid == mc_uuid:
        return

    ensure_balance_row(c, mc_uuid)
    c.execute("SELECT netherite, diamond FROM balances WHERE mc_uuid=?", (account_uuid,))
    legacy = c.fetchone()
    if legacy:
        netherite, diamond = legacy
        if netherite:
            c.execute("UPDATE balances SET netherite=netherite+? WHERE mc_uuid=?", (netherite, mc_uuid))
            c.execute("UPDATE balances SET netherite=0 WHERE mc_uuid=?", (account_uuid,))
        if diamond:
            c.execute("UPDATE balances SET diamond=diamond+? WHERE mc_uuid=?", (diamond, mc_uuid))
            c.execute("UPDATE balances SET diamond=0 WHERE mc_uuid=?", (account_uuid,))

    c.execute("SELECT commodity, amount FROM commodity_balances WHERE mc_uuid=?", (account_uuid,))
    rows = c.fetchall()
    for commodity, amount in rows:
        c.execute(
            """INSERT INTO commodity_balances (mc_uuid, commodity, amount)
               VALUES (?, ?, ?)
               ON CONFLICT(mc_uuid, commodity)
               DO UPDATE SET amount = amount + excluded.amount""",
            (mc_uuid, commodity, amount),
        )
    if rows:
        c.execute("DELETE FROM commodity_balances WHERE mc_uuid=?", (account_uuid,))


def locked_mdragons_for_account(c, owner_uuid: str) -> float:
    owner_keys = dragon_order_owner_keys(c, owner_uuid)
    placeholders = ",".join("?" for _ in owner_keys)
    c.execute(
        f"""SELECT COALESCE(SUM(remaining * price_per), 0) FROM orders
            WHERE mc_uuid IN ({placeholders}) AND side='buy' AND remaining>0
            AND (is_system IS NULL OR is_system=0)""",
        owner_keys,
    )
    row = c.fetchone()
    return float(row[0]) if row and row[0] else 0.0


def compute_reference_price(c, item_key: str) -> float:
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    c.execute("SELECT SUM(value), SUM(amount) FROM trade_log WHERE item=? AND timestamp>?",
              (item_key, week_ago))
    row = c.fetchone()
    if row and row[0] and row[1] and row[1] > 0:
        return row[0] / row[1]
    if item_key == "DAEMON":
        return 1.0
    return 1.0


def get_active_chairman_target(c) -> float:
    c.execute("SELECT value FROM chairman_settings WHERE key='weekly_target_active'")
    row = c.fetchone()
    if row:
        return float(row[0])
    c.execute("SELECT value FROM chairman_settings WHERE key='weekly_target'")
    legacy = c.fetchone()
    return float(legacy[0]) if legacy else DEFAULT_WEEKLY_TARGET


def get_pending_chairman_target(c) -> float:
    c.execute("SELECT value FROM chairman_settings WHERE key='weekly_target_pending'")
    row = c.fetchone()
    return float(row[0]) if row else get_active_chairman_target(c)


def apply_pending_chairman_target(c) -> float:
    active = get_pending_chairman_target(c)
    c.execute("INSERT OR REPLACE INTO chairman_settings (key, value) VALUES ('weekly_target_active',?)", (str(float(active)),))
    c.execute("INSERT OR REPLACE INTO chairman_settings (key, value) VALUES ('weekly_target',?)", (str(float(active)),))
    return active


def get_role_weekly_budget(c) -> float:
    return get_active_chairman_target(c) / 2.0


def get_role_daily_budget(c) -> float:
    return get_role_weekly_budget(c) / 7.0


def get_mechanic_days_unlocked(last_reset_time: datetime, now: datetime) -> int:
    elapsed = max(0.0, (now - last_reset_time).total_seconds())
    return max(1, min(7, int(elapsed // 86400) + 1))


def get_mechanic_allowed_value(c, last_reset_time: datetime, now: datetime) -> float:
    return get_role_daily_budget(c) * get_mechanic_days_unlocked(last_reset_time, now)


def compute_mechanic_price(side: str, reference_price: float, last_reset_time: datetime, now: datetime) -> float:
    T = float(7 * 24 * 3600)
    elapsed = max(0.0, (now - last_reset_time).total_seconds())
    t = max(0.0, min(elapsed, T))
    ratio = (t / T) ** 2

    if side == "buy":
        floor_p = reference_price * MM_FLOOR_FACTOR
        target_p = reference_price * MM_TARGET_FACTOR
        return round(floor_p + (target_p - floor_p) * ratio, 2)

    ceil_p = reference_price * NO_CEIL_FACTOR
    target_p = reference_price * NO_TARGET_FACTOR
    return round(ceil_p - (ceil_p - target_p) * ratio, 2)


def get_chairman_range(c) -> tuple[float, float]:
    c.execute("SELECT value FROM chairman_settings WHERE key='weekly_target_min'")
    row_min = c.fetchone()
    c.execute("SELECT value FROM chairman_settings WHERE key='weekly_target_max'")
    row_max = c.fetchone()
    min_target = float(row_min[0]) if row_min else MIN_WEEKLY_TARGET
    max_target = float(row_max[0]) if row_max else MAX_WEEKLY_TARGET
    if min_target > max_target:
        min_target, max_target = max_target, min_target
    return min_target, max_target


def sync_mechanic_order(
    c,
    *,
    role: str,
    item_key: str,
    side: str,
    reference_price: float,
    weekly_value_filled: float,
    last_reset_time: datetime,
    now: datetime,
    order_id: Optional[int],
) -> tuple[Optional[int], str, float, float, int]:
    price = compute_mechanic_price(side, reference_price, last_reset_time, now)
    daily_budget = get_role_daily_budget(c)
    allowed_value = get_mechanic_allowed_value(c, last_reset_time, now)
    available_value = max(0.0, allowed_value - float(weekly_value_filled or 0.0))
    desired_value = min(daily_budget, available_value)

    if desired_value <= 0:
        if order_id:
            c.execute("DELETE FROM orders WHERE id=? AND is_system=1", (order_id,))
        return None, "active", price, allowed_value, get_mechanic_days_unlocked(last_reset_time, now)

    desired_units = max(1, math.ceil(desired_value / max(price, 0.01)))
    system_uuid = SYSTEM_MM_UUID if role == "MANSA_MUSA" else SYSTEM_NO_UUID

    if order_id:
        c.execute("SELECT id FROM orders WHERE id=? AND is_system=1", (order_id,))
        existing = c.fetchone()
        if existing:
            c.execute(
                "UPDATE orders SET amount=?, remaining=?, price_per=? WHERE id=? AND is_system=1",
                (desired_units, desired_units, price, order_id),
            )
        else:
            order_id = None

    if not order_id:
        order_id = place_system_order(c, system_uuid, item_key, side, price, desired_units)

    return order_id, "active", price, allowed_value, get_mechanic_days_unlocked(last_reset_time, now)


def place_system_order(c, system_uuid: str, item_key: str, side: str, price_per: float, amount: float) -> int:
    c.execute(
        """INSERT INTO orders (mc_uuid, item, amount, price_per, remaining, side, is_system)
           VALUES (?,?,?,?,?,?,1)""",
        (system_uuid, item_key, amount, price_per, amount, side),
    )
    return c.lastrowid


def _credit_item(c, mc_uuid: str, item_key: str, amount: float):
    if item_key == "DAEMON":
        credit_mdragons(c, mc_uuid, amount)
        return
    if is_legacy_item(item_key):
        col = item_col(item_key)
        ensure_balance_row(c, mc_uuid)
        c.execute(f"UPDATE balances SET {col}={col}+? WHERE mc_uuid=?", (amount, mc_uuid))
    else:
        c.execute(
            """INSERT INTO commodity_balances (mc_uuid, commodity, amount)
               VALUES (?, ?, ?)
               ON CONFLICT(mc_uuid, commodity)
               DO UPDATE SET amount = amount + excluded.amount""",
            (mc_uuid, item_key, amount),
        )


def _debit_item(c, mc_uuid: str, item_key: str, amount: float) -> bool:
    if item_key == "DAEMON":
        return debit_mdragons(c, mc_uuid, amount)
    if is_legacy_item(item_key):
        col = item_col(item_key)
        ensure_balance_row(c, mc_uuid)
        c.execute(f"SELECT {col} FROM balances WHERE mc_uuid=?", (mc_uuid,))
        row = c.fetchone()
        current = row[0] if row else 0
        if current < amount:
            return False
        c.execute(f"UPDATE balances SET {col}={col}-? WHERE mc_uuid=?", (amount, mc_uuid))
        return True

    c.execute("SELECT amount FROM commodity_balances WHERE mc_uuid=? AND commodity=?", (mc_uuid, item_key))
    row = c.fetchone()
    current = row[0] if row else 0.0
    if current < amount - 1e-9:
        return False

    new_bal = max(0.0, current - amount)
    c.execute(
        """INSERT INTO commodity_balances (mc_uuid, commodity, amount)
           VALUES (?, ?, ?)
           ON CONFLICT(mc_uuid, commodity)
           DO UPDATE SET amount = ?""",
        (mc_uuid, item_key, new_bal, new_bal),
    )
    return True


def _get_item_balance(c, mc_uuid: str, item_key: str) -> float:
    if item_key == "DAEMON":
        return get_mdragon_balance(c, mc_uuid)
    if is_legacy_item(item_key):
        col = item_col(item_key)
        ensure_balance_row(c, mc_uuid)
        c.execute(f"SELECT {col} FROM balances WHERE mc_uuid=?", (mc_uuid,))
        row = c.fetchone()
        return row[0] if row else 0.0

    c.execute("SELECT amount FROM commodity_balances WHERE mc_uuid=? AND commodity=?", (mc_uuid, item_key))
    row = c.fetchone()
    return row[0] if row else 0.0


def _record_order_event(
    c,
    event_type: str,
    order_id: int,
    mc_uuid: str,
    item: str,
    amount: float,
    price_per: float,
    side: str,
):
    c.execute(
        """INSERT INTO order_event_log
           (event_type, order_id, mc_uuid, item, amount, price_per, side)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (event_type, order_id, mc_uuid, item, amount, price_per, side),
    )


def _record_trade_event(
    c,
    item: str,
    buyer_uuid: str,
    seller_uuid: str,
    amount: float,
    price_per: float,
    value: float,
):
    c.execute(
        """INSERT INTO trade_event_log
           (item, buyer_uuid, seller_uuid, amount, price_per, value)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (item, buyer_uuid, seller_uuid, amount, price_per, value),
    )


def match_orders(c, item: str):
    while True:
        c.execute(
            """SELECT id, mc_uuid, price_per, remaining, created FROM orders
               WHERE item=? AND side='sell' AND remaining>0
               ORDER BY price_per ASC, created ASC LIMIT 1""",
            (item,),
        )
        ask_row = c.fetchone()
        if not ask_row:
            return
        ask_id, seller_uuid, ask_price, ask_remaining, ask_created = ask_row

        c.execute(
            """SELECT id, mc_uuid, price_per, remaining, created FROM orders
               WHERE item=? AND side='buy' AND remaining>0 AND price_per>=?
               ORDER BY price_per DESC, created ASC LIMIT 1""",
            (item, ask_price),
        )
        bid_row = c.fetchone()
        if not bid_row:
            return
        bid_id, buyer_uuid, bid_price, bid_remaining, bid_created = bid_row

        fill_amount = min(ask_remaining, bid_remaining)
        trade_price = ask_price if ask_created < bid_created else bid_price
        cost = fill_amount * trade_price

        is_sys_seller = str(seller_uuid).startswith("SYSTEM_")
        is_sys_buyer = str(buyer_uuid).startswith("SYSTEM_")

        if not is_sys_seller:
            credit_mdragons(c, seller_uuid, cost)

        if not is_sys_buyer:
            _credit_item(c, item_account_for_order_owner(c, buyer_uuid), item, fill_amount)
            if trade_price < bid_price:
                excess = (bid_price - trade_price) * fill_amount
                credit_mdragons(c, buyer_uuid, excess)

        c.execute(
            """INSERT INTO trade_log (item, buyer_uuid, seller_uuid, amount, price_per, value)
               VALUES (?,?,?,?,?,?)""",
            (item, buyer_uuid, seller_uuid, fill_amount, trade_price, cost),
        )
        _record_trade_event(c, item, buyer_uuid, seller_uuid, fill_amount, trade_price, cost)

        if is_sys_seller or is_sys_buyer:
            sys_uuid = buyer_uuid if is_sys_buyer else seller_uuid
            mech_role = "MANSA_MUSA" if sys_uuid == SYSTEM_MM_UUID else "NETHERITE_OVERLORD"
            c.execute("UPDATE special_mechanics SET weekly_value_filled=weekly_value_filled+? WHERE role=?",
                      (cost, mech_role))
            c.execute("SELECT weekly_value_filled FROM special_mechanics WHERE role=?", (mech_role,))
            vf_row = c.fetchone()
            target = get_role_weekly_budget(c)

            if vf_row and vf_row[0] >= target:
                sys_order_id = bid_id if is_sys_buyer else ask_id
                real_order_id = ask_id if is_sys_buyer else bid_id
                real_remaining = (ask_remaining if is_sys_buyer else bid_remaining) - fill_amount

                c.execute("DELETE FROM orders WHERE id=? AND is_system=1", (sys_order_id,))
                c.execute("UPDATE special_mechanics SET status='frozen', order_id=NULL WHERE role=?",
                          (mech_role,))
                if real_remaining > 0:
                    c.execute("UPDATE orders SET remaining=? WHERE id=?",
                              (real_remaining, real_order_id))
                else:
                    c.execute("DELETE FROM orders WHERE id=?", (real_order_id,))
                return

        new_ask = ask_remaining - fill_amount
        if new_ask > 0:
            c.execute("UPDATE orders SET remaining=? WHERE id=?", (new_ask, ask_id))
        else:
            c.execute("DELETE FROM orders WHERE id=?", (ask_id,))

        new_bid = bid_remaining - fill_amount
        if new_bid > 0:
            c.execute("UPDATE orders SET remaining=? WHERE id=?", (new_bid, bid_id))
        else:
            c.execute("DELETE FROM orders WHERE id=?", (bid_id,))


def _do_reset(c, role: str, reset_time: datetime):
    apply_pending_chairman_target(c)
    c.execute(
        """SELECT current_item, pending_item, side, order_id, activates_at
           FROM special_mechanics WHERE role=?""",
        (role,),
    )
    row = c.fetchone()
    if not row:
        return
    current_item, pending_item, side, old_order_id, activates_at = row

    if old_order_id:
        c.execute("DELETE FROM orders WHERE id=? AND is_system=1", (old_order_id,))

    new_item = current_item
    if pending_item and activates_at:
        act_dt = datetime.fromisoformat(activates_at)
        if act_dt.tzinfo is None:
            act_dt = act_dt.replace(tzinfo=GERMAN_TZ)
        if reset_time >= act_dt:
            new_item = pending_item

    if new_item not in ("DIAMOND", "NETHERITE_INGOT"):
        new_item = "NETHERITE_INGOT"

    ref_price = compute_reference_price(c, new_item)
    now_german = max(datetime.now(GERMAN_TZ), reset_time)
    new_order_id, status, _, _, _ = sync_mechanic_order(
        c,
        role=role,
        item_key=new_item,
        side=side,
        reference_price=ref_price,
        weekly_value_filled=0.0,
        last_reset_time=reset_time,
        now=now_german,
        order_id=None,
    )
    match_orders(c, new_item)
    c.execute("SELECT id FROM orders WHERE id=? AND is_system=1", (new_order_id,))
    if new_order_id and c.fetchone() is None:
        new_order_id = None

    c.execute(
        """UPDATE special_mechanics SET
           current_item=?, pending_item=NULL, status=?,
           order_id=?, reference_price=?, weekly_value_filled=0, last_reset=?
           WHERE role=?""",
        (
            new_item,
            status,
            new_order_id,
            ref_price,
            reset_time.isoformat(),
            role,
        ),
    )


# ─────────────────────────────────────────────────────────────────
# MODELS
# ─────────────────────────────────────────────────────────────────
class DepositWithdraw(BaseModel):
    uuid: str
    item: str
    amount: float


class VerifyLink(BaseModel):
    code: str
    discord_id: str


class Exchange(BaseModel):
    item: str
    amount: int
    mc_uuid: str


class PlaceOrder(BaseModel):
    mc_uuid: str
    item: str
    amount: float
    price_per: float


class CancelOrder(BaseModel):
    order_id: int
    mc_uuid: str


class CancelAll(BaseModel):
    mc_uuid: str
    item: Optional[str] = None


class ConvertDragons(BaseModel):
    mc_uuid: str
    amount: float
    refund_id: Optional[str] = None


class AnnounceMechanic(BaseModel):
    role: str
    item: str


class ChairmanTarget(BaseModel):
    delta: int


class ChairmanRange(BaseModel):
    minimum: int
    maximum: int


class CreatePurchaseList(BaseModel):
    mc_uuid: str
    name: str
    items: str
    price: float


class DeletePurchaseList(BaseModel):
    mc_uuid: str
    list_id: int


class FillPurchaseList(BaseModel):
    mc_uuid: str
    list_id: int


class DepositWithdrawLog(BaseModel):
    mc_uuid: str
    action: str
    item: str
    amount: float
    base_units: float


class ExternalGive(BaseModel):
    mc_uuid: str
    amount: int
    event_id: Optional[str] = None


class GiveTransfer(BaseModel):
    sender_uuid: str
    recipient_uuid: str
    amount: int


class LoginRewardClaim(BaseModel):
    mc_uuid: str


class CommodityTransaction(BaseModel):
    mc_uuid: str
    commodity: str
    amount: float


class AdjustBalance(BaseModel):
    mc_uuid: str
    item: str
    delta: float


class AliveReport(BaseModel):
    mc_uuid: str
    name: str
    active_seconds: float


class AliveDeath(BaseModel):
    mc_uuid: str
    name: str


class BountyPlace(BaseModel):
    issuer_uuid: str
    target_uuid: str
    target_name: str
    amount: int


class BountyClaim(BaseModel):
    target_uuid: str
    target_name: str
    killer_uuid: str
    killer_name: str


class BackupRequest(BaseModel):
    requested_by: Optional[str] = None
    reason: Optional[str] = None


class SetPause(BaseModel):
    paused: bool
    paused_by: Optional[str] = None


# ─────────────────────────────────────────────────────────────────
# CORE ENDPOINTS
# ─────────────────────────────────────────────────────────────────
@app.post("/api/deposit")
async def deposit(data: DepositWithdraw):
    item_key = normalize_legacy_item(data.item)
    if not item_key:
        raise HTTPException(400, "Invalid item")
    if data.amount <= 0:
        raise HTTPException(400, "Amount must be positive")

    conn = get_conn()
    c = conn.cursor()
    if item_key != "DAEMON":
        ensure_balance_row(c, data.uuid)
    _credit_item(c, data.uuid, item_key, data.amount)
    conn.commit()
    conn.close()
    return {"status": "success"}


@app.post("/api/withdraw")
async def withdraw(data: DepositWithdraw):
    item_key = normalize_legacy_item(data.item)
    if not item_key:
        raise HTTPException(400, "Invalid item")
    if data.amount <= 0:
        raise HTTPException(400, "Amount must be positive")

    conn = get_conn()
    c = conn.cursor()
    if not _debit_item(c, data.uuid, item_key, data.amount):
        conn.close()
        raise HTTPException(400, "Insufficient balance")
    conn.commit()
    conn.close()
    return {"status": "success"}


@app.get("/api/link/generate")
async def generate_link(uuid: str):
    code = f"{pyuuid.uuid4().int % 1000000:06d}"
    expires = datetime.now() + timedelta(minutes=15)
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM pending_links WHERE mc_uuid=?", (uuid,))
    c.execute("INSERT INTO pending_links (code, mc_uuid, expires) VALUES (?,?,?)", (code, uuid, expires))
    conn.commit()
    conn.close()
    return {"code": code}


@app.post("/api/link/verify")
async def verify_link(data: VerifyLink):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT mc_uuid FROM pending_links WHERE code=? AND expires>?", (data.code, datetime.now()))
    row = c.fetchone()
    if not row:
        conn.close()
        raise HTTPException(400, "Invalid or expired code")
    mc_uuid = row[0]
    c.execute("SELECT mc_uuid FROM linked_accounts WHERE discord_id=?", (data.discord_id,))
    old_link = c.fetchone()
    if old_link:
        migrate_discord_dragon_balance(c, old_link[0], data.discord_id)
    c.execute("SELECT discord_id FROM linked_accounts WHERE mc_uuid=?", (mc_uuid,))
    old_discord = c.fetchone()
    if old_discord and old_discord[0] != data.discord_id:
        migrate_discord_dragon_balance(c, mc_uuid, old_discord[0])
    c.execute("INSERT OR REPLACE INTO linked_accounts (mc_uuid, discord_id) VALUES (?,?)",
              (mc_uuid, data.discord_id))
    ensure_balance_row(c, mc_uuid)
    migrate_discord_dragon_balance(c, mc_uuid, data.discord_id)
    migrate_discord_item_balances(c, mc_uuid, data.discord_id)
    c.execute(
        "UPDATE orders SET mc_uuid=? WHERE mc_uuid=? AND (is_system IS NULL OR is_system=0)",
        (dragon_account_uuid(data.discord_id), mc_uuid),
    )
    c.execute("DELETE FROM pending_links WHERE code=?", (data.code,))
    conn.commit()
    conn.close()
    return {"status": "linked"}


@app.get("/api/balance/{mc_uuid}")
async def get_balance_endpoint(mc_uuid: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT netherite, diamond FROM balances WHERE mc_uuid=?", (mc_uuid,))
    row = c.fetchone()
    netherite, diamond = row if row else (0, 0)
    mdragons = get_mdragon_balance(c, mc_uuid)
    mdragons_locked = locked_mdragons_for_account(c, mc_uuid)
    conn.close()

    return {
        "netherite": netherite,
        "diamond": diamond,
        "mdragons": mdragons,
        "mdragons_locked": round(mdragons_locked, 2),
        "mdragons_total": round(mdragons + mdragons_locked, 2),
    }


@app.post("/api/balance/adjust")
async def adjust_balance(data: AdjustBalance):
    item_key = normalize_item(data.item)
    if not item_key:
        raise HTTPException(400, "Invalid item")

    conn = get_conn()
    try:
        begin_immediate(conn)
        c = conn.cursor()
        old_balance = _get_item_balance(c, data.mc_uuid, item_key)
        if data.delta >= 0:
            if is_legacy_item(item_key) and item_key != "DAEMON":
                ensure_balance_row(c, data.mc_uuid)
            _credit_item(c, data.mc_uuid, item_key, data.delta)
        else:
            if not _debit_item(c, data.mc_uuid, item_key, -data.delta):
                raise HTTPException(400, "Insufficient balance")
        new_balance = _get_item_balance(c, data.mc_uuid, item_key)
        conn.commit()
        return {
            "status": "adjusted",
            "item": item_key,
            "delta": data.delta,
            "old_balance": old_balance,
            "new_balance": new_balance,
        }
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/api/give")
async def give_dragons(data: GiveTransfer):
    if data.amount <= 0:
        raise HTTPException(400, "Amount must be positive")
    if data.sender_uuid == data.recipient_uuid:
        raise HTTPException(400, "Cannot give to yourself")

    conn = get_conn()
    try:
        begin_immediate(conn)
        c = conn.cursor()
        sender_account = dragon_account_for_uuid(c, data.sender_uuid)
        recipient_account = dragon_account_for_uuid(c, data.recipient_uuid)
        if sender_account == recipient_account:
            raise HTTPException(400, "Cannot give to yourself")
        if not debit_mdragons(c, data.sender_uuid, data.amount):
            raise HTTPException(400, "Insufficient dragons")
        credit_mdragons(c, data.recipient_uuid, data.amount)
        sender_balance = get_mdragon_balance(c, data.sender_uuid)
        recipient_balance = get_mdragon_balance(c, data.recipient_uuid)
        conn.commit()
        return {
            "status": "sent",
            "amount": data.amount,
            "sender_balance": sender_balance,
            "recipient_balance": recipient_balance,
        }
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/api/login/reward")
async def claim_login_reward(data: LoginRewardClaim):
    week_start = get_last_reset_time().isoformat()
    conn = get_conn()
    try:
        begin_immediate(conn)
        c = conn.cursor()
        c.execute("SELECT discord_id FROM linked_accounts WHERE mc_uuid=?", (data.mc_uuid,))
        linked = c.fetchone()
        if not linked:
            raise HTTPException(400, "Minecraft account is not linked to Discord")

        c.execute(
            "INSERT OR IGNORE INTO weekly_login_rewards (mc_uuid, week_start, amount) VALUES (?, ?, ?)",
            (data.mc_uuid, week_start, WEEKLY_LOGIN_REWARD),
        )
        duplicate = c.rowcount == 0
        if not duplicate:
            credit_mdragons(c, data.mc_uuid, WEEKLY_LOGIN_REWARD)
        balance = get_mdragon_balance(c, data.mc_uuid)
        conn.commit()
        return {
            "status": "claimed",
            "duplicate": duplicate,
            "amount": 0 if duplicate else WEEKLY_LOGIN_REWARD,
            "configured_amount": WEEKLY_LOGIN_REWARD,
            "balance": balance,
            "week_start": week_start,
            "discord_id": linked[0],
        }
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.get("/api/mc_uuid/{discord_id}")
async def get_mc_uuid_endpoint(discord_id: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT mc_uuid FROM linked_accounts WHERE discord_id=?", (discord_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Account not linked")
    return {"mc_uuid": row[0]}


@app.get("/api/health")
async def health():
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT 1")
        c.fetchone()
        c.execute("SELECT COUNT(*) FROM balances")
        balances_count = c.fetchone()[0]
        return {
            "status": "ok",
            "db_path": DB_PATH,
            "balances": balances_count,
            "time": datetime.now(timezone.utc).isoformat(),
        }
    finally:
        conn.close()


@app.post("/api/admin/backup")
async def admin_backup(data: BackupRequest):
    backup = backup_sqlite_database("mdragons")

    return {
        "status": "backed_up",
        "path": backup["path"],
        "bytes": backup["bytes"],
        "requested_by": data.requested_by,
        "reason": data.reason,
    }


@app.post("/api/alive/report")
async def alive_report(data: AliveReport):
    if data.active_seconds <= 0:
        return {"status": "ignored", "current_seconds": 0, "minecraft_days": 0}
    capped_seconds = min(float(data.active_seconds), 300.0)
    conn = get_conn()
    try:
        begin_immediate(conn)
        c = conn.cursor()
        c.execute(
            """INSERT INTO alive_stats (mc_uuid, name, current_seconds, best_seconds, last_report)
               VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(mc_uuid) DO UPDATE SET
                   name=excluded.name,
                   current_seconds=alive_stats.current_seconds + excluded.current_seconds,
                   best_seconds=MAX(alive_stats.best_seconds, alive_stats.current_seconds + excluded.current_seconds),
                   last_report=CURRENT_TIMESTAMP""",
            (data.mc_uuid, data.name[:32], capped_seconds, capped_seconds),
        )
        c.execute("SELECT current_seconds, best_seconds, deaths FROM alive_stats WHERE mc_uuid=?", (data.mc_uuid,))
        current, best, deaths = c.fetchone()
        conn.commit()
        return {
            "status": "reported",
            "current_seconds": round(current, 2),
            "best_seconds": round(best, 2),
            "deaths": deaths,
            "minecraft_days": round(current / 1200.0, 3),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/api/alive/death")
async def alive_death(data: AliveDeath):
    conn = get_conn()
    try:
        begin_immediate(conn)
        c = conn.cursor()
        c.execute(
            """INSERT INTO alive_stats (mc_uuid, name, current_seconds, best_seconds, deaths, last_report)
               VALUES (?, ?, 0, 0, 1, CURRENT_TIMESTAMP)
               ON CONFLICT(mc_uuid) DO UPDATE SET
                   name=excluded.name,
                   best_seconds=MAX(alive_stats.best_seconds, alive_stats.current_seconds),
                   current_seconds=0,
                   deaths=alive_stats.deaths + 1,
                   last_report=CURRENT_TIMESTAMP""",
            (data.mc_uuid, data.name[:32]),
        )
        c.execute("SELECT best_seconds, deaths FROM alive_stats WHERE mc_uuid=?", (data.mc_uuid,))
        best, deaths = c.fetchone()
        conn.commit()
        return {"status": "reset", "best_seconds": round(best, 2), "deaths": deaths}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.get("/api/alive/leaderboard")
async def alive_leaderboard(limit: int = Query(10, ge=1, le=50), offset: int = Query(0, ge=0)):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        """SELECT mc_uuid, COALESCE(name, mc_uuid), current_seconds, best_seconds, deaths
           FROM alive_stats
           WHERE current_seconds > 0
           ORDER BY current_seconds DESC, mc_uuid ASC
           LIMIT ? OFFSET ?""",
        (limit, offset),
    )
    rows = c.fetchall()
    conn.close()
    return {
        "offset": offset,
        "limit": limit,
        "entries": [
            {
                "rank": offset + idx + 1,
                "mc_uuid": row[0],
                "name": row[1],
                "current_seconds": round(row[2], 2),
                "minecraft_days": round(row[2] / 1200.0, 2),
                "best_minecraft_days": round(row[3] / 1200.0, 2),
                "deaths": row[4],
            }
            for idx, row in enumerate(rows)
        ],
    }


@app.get("/api/alive/leaderboard_text", response_class=PlainTextResponse)
async def alive_leaderboard_text(limit: int = Query(10, ge=1, le=20), offset: int = Query(0, ge=0)):
    data = await alive_leaderboard(limit=limit, offset=offset)
    entries = data["entries"]
    if not entries:
        return "No alive streaks yet."
    return "\n".join(
        f"#{e['rank']} {e['name']} - {e['minecraft_days']} Minecraft days alive ({e['deaths']} deaths)"
        for e in entries
    )


@app.get("/api/bounties")
async def list_bounties(limit: int = Query(10, ge=1, le=50), offset: int = Query(0, ge=0)):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        """SELECT target_uuid, COALESCE(target_name, target_uuid), amount, updated
           FROM bounties
           WHERE amount > 0
           ORDER BY amount DESC, updated DESC, target_uuid ASC
           LIMIT ? OFFSET ?""",
        (limit, offset),
    )
    rows = c.fetchall()
    conn.close()
    return {
        "offset": offset,
        "limit": limit,
        "entries": [
            {
                "rank": offset + idx + 1,
                "target_uuid": row[0],
                "target_name": row[1],
                "amount": round(row[2], 2),
                "updated": row[3],
            }
            for idx, row in enumerate(rows)
        ],
    }


@app.get("/api/bounties_text", response_class=PlainTextResponse)
async def bounties_text(limit: int = Query(10, ge=1, le=20), offset: int = Query(0, ge=0)):
    data = await list_bounties(limit=limit, offset=offset)
    entries = data["entries"]
    if not entries:
        return "No active bounties."
    return "\n".join(
        f"#{e['rank']} {e['target_name']} - {format_compact_amount(e['amount'])} dragons"
        for e in entries
    )


@app.post("/api/bounty/place")
async def bounty_place(data: BountyPlace):
    if data.amount <= 0:
        raise HTTPException(400, "Amount must be positive")
    if data.issuer_uuid == data.target_uuid:
        raise HTTPException(400, "Cannot place a bounty on yourself")

    conn = get_conn()
    try:
        begin_immediate(conn)
        c = conn.cursor()
        if not debit_mdragons(c, data.issuer_uuid, data.amount):
            raise HTTPException(400, "Insufficient dragons")
        c.execute(
            """INSERT INTO bounties (target_uuid, target_name, amount, updated)
               VALUES (?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(target_uuid) DO UPDATE SET
                   target_name=excluded.target_name,
                   amount=bounties.amount + excluded.amount,
                   updated=CURRENT_TIMESTAMP""",
            (data.target_uuid, data.target_name[:32], data.amount),
        )
        c.execute(
            """INSERT INTO bounty_events (event_type, issuer_uuid, target_uuid, target_name, amount)
               VALUES ('placed', ?, ?, ?, ?)""",
            (data.issuer_uuid, data.target_uuid, data.target_name[:32], data.amount),
        )
        c.execute("SELECT amount FROM bounties WHERE target_uuid=?", (data.target_uuid,))
        total = c.fetchone()[0]
        conn.commit()
        return {"status": "placed", "amount": data.amount, "target_total": round(total, 2)}
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/api/bounty/claim")
async def bounty_claim(data: BountyClaim):
    if data.killer_uuid == data.target_uuid:
        return {"status": "ignored", "amount": 0}

    conn = get_conn()
    try:
        begin_immediate(conn)
        c = conn.cursor()
        c.execute("SELECT amount FROM bounties WHERE target_uuid=?", (data.target_uuid,))
        row = c.fetchone()
        amount = float(row[0]) if row else 0.0
        if amount <= 0:
            conn.commit()
            return {"status": "none", "amount": 0}

        c.execute("DELETE FROM bounties WHERE target_uuid=?", (data.target_uuid,))
        credit_mdragons(c, data.killer_uuid, amount)
        c.execute(
            """INSERT INTO bounty_events (event_type, target_uuid, target_name, killer_uuid, killer_name, amount)
               VALUES ('claimed', ?, ?, ?, ?, ?)""",
            (data.target_uuid, data.target_name[:32], data.killer_uuid, data.killer_name[:32], amount),
        )
        conn.commit()
        return {
            "status": "claimed",
            "amount": format_compact_amount(amount),
            "target_name": data.target_name[:32],
            "killer_name": data.killer_name[:32],
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.get("/api/bounty/{target_uuid}")
async def bounty_get(target_uuid: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT target_name, amount FROM bounties WHERE target_uuid=?", (target_uuid,))
    row = c.fetchone()
    conn.close()
    if not row:
        return {"target_uuid": target_uuid, "amount": 0}
    return {"target_uuid": target_uuid, "target_name": row[0], "amount": round(row[1], 2)}


@app.post("/api/exchange")
async def exchange(data: Exchange):
    raise HTTPException(410, "Central exchange is disabled; use the order book.")


@app.post("/api/order/place")
async def place_order(data: PlaceOrder):
    item_key = normalize_item(data.item)
    if not item_key:
        raise HTTPException(400, "Invalid item")
    if data.amount <= 0 or data.price_per <= 0:
        raise HTTPException(400, "Amount and price must be positive")

    conn = get_conn()
    try:
        begin_immediate(conn)
        c = conn.cursor()
        owner_uuid = order_owner_uuid(c, data.mc_uuid)
        item_owner = item_account_for_order_owner(c, owner_uuid)

        if not _debit_item(c, item_owner, item_key, data.amount):
            raise HTTPException(400, "Insufficient items in vault")

        c.execute(
            """INSERT INTO orders (mc_uuid, item, amount, price_per, remaining, side)
               VALUES (?,?,?,?,?,'sell')""",
            (owner_uuid, item_key, data.amount, data.price_per, data.amount),
        )
        order_id = c.lastrowid
        _record_order_event(c, "placed", order_id, owner_uuid, item_key, data.amount, data.price_per, "sell")
        match_orders(c, item_key)
        conn.commit()
        return {"status": "order_placed", "order_id": order_id}
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/api/order/place_buy")
async def place_buy_order(data: PlaceOrder):
    item_key = normalize_item(data.item)
    if not item_key:
        raise HTTPException(400, "Invalid item")
    if data.amount <= 0 or data.price_per <= 0:
        raise HTTPException(400, "Amount and price must be positive")

    conn = get_conn()
    reserve = data.amount * data.price_per
    try:
        begin_immediate(conn)
        c = conn.cursor()
        owner_uuid = order_owner_uuid(c, data.mc_uuid)
        if not debit_mdragons(c, owner_uuid, reserve):
            raise HTTPException(400, "Insufficient 🐉")

        c.execute(
            """INSERT INTO orders (mc_uuid, item, amount, price_per, remaining, side)
               VALUES (?,?,?,?,?,'buy')""",
            (owner_uuid, item_key, data.amount, data.price_per, data.amount),
        )
        order_id = c.lastrowid
        _record_order_event(c, "placed", order_id, owner_uuid, item_key, data.amount, data.price_per, "buy")
        match_orders(c, item_key)
        conn.commit()
        return {"status": "order_placed", "order_id": order_id}
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.get("/api/orders")
async def list_orders_all():
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        """SELECT id, mc_uuid, item, remaining, price_per, side FROM orders
           WHERE remaining>0 AND (is_system IS NULL OR is_system=0)"""
    )
    rows = c.fetchall()
    conn.close()
    return {
        "orders": [
            {
                "id": r[0],
                "owner_mc": r[1],
                "seller_mc": r[1] if r[5] == "sell" else None,
                "buyer_mc": r[1] if r[5] == "buy" else None,
                "item": r[2],
                "remaining": r[3],
                "price_per": r[4],
                "side": r[5],
            }
            for r in rows
        ]
    }


@app.post("/api/order/cancel")
async def cancel_order(data: CancelOrder):
    conn = get_conn()
    try:
        begin_immediate(conn)
        c = conn.cursor()
        c.execute("SELECT mc_uuid, item, remaining, price_per, side FROM orders WHERE id=?", (data.order_id,))
        row = c.fetchone()
        owner_keys = order_owner_keys(c, data.mc_uuid)
        if not row or row[0] not in owner_keys:
            raise HTTPException(400, "Order not found or not yours")

        row_owner, item, remaining, price_per, side = row[0], row[1], row[2], row[3], row[4]
        if side == "sell":
            _credit_item(c, item_account_for_order_owner(c, row_owner), item, remaining)
        else:
            refund = remaining * price_per
            credit_mdragons(c, row_owner, refund)

        _record_order_event(c, "cancelled", data.order_id, row_owner, item, remaining, price_per, side)
        c.execute("DELETE FROM orders WHERE id=?", (data.order_id,))
        conn.commit()
        return {"status": "cancelled"}
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/api/order/cancel_all")
async def cancel_all_orders(data: CancelAll):
    conn = get_conn()
    try:
        begin_immediate(conn)
        c = conn.cursor()
        owner_keys = order_owner_keys(c, data.mc_uuid)
        placeholders = ",".join("?" for _ in owner_keys)

        if data.item:
            item_key = normalize_item(data.item)
            if not item_key:
                raise HTTPException(400, "Invalid item")
            c.execute(
                f"""SELECT id, mc_uuid, item, remaining, price_per, side FROM orders
                   WHERE mc_uuid IN ({placeholders}) AND item=? AND remaining>0 AND (is_system IS NULL OR is_system=0)""",
                (*owner_keys, item_key),
            )
        else:
            c.execute(
                f"""SELECT id, mc_uuid, item, remaining, price_per, side FROM orders
                   WHERE mc_uuid IN ({placeholders}) AND remaining>0 AND (is_system IS NULL OR is_system=0)""",
                owner_keys,
            )

        rows = c.fetchall()
        cancelled = 0
        for order_id, row_owner, item, remaining, price_per, side in rows:
            if side == "sell":
                _credit_item(c, item_account_for_order_owner(c, row_owner), item, remaining)
            else:
                credit_mdragons(c, row_owner, remaining * price_per)
            _record_order_event(c, "cancelled", order_id, row_owner, item, remaining, price_per, side)
            c.execute("DELETE FROM orders WHERE id=?", (order_id,))
            cancelled += 1

        conn.commit()
        return {"status": "cancelled", "cancelled": cancelled}
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/api/convert/to_ub")
async def convert_to_ub(data: ConvertDragons):
    if data.amount <= 0:
        raise HTTPException(400, "Amount must be positive")

    conn = get_conn()
    try:
        begin_immediate(conn)
        c = conn.cursor()
        if not debit_mdragons(c, data.mc_uuid, data.amount):
            raise HTTPException(400, "Insufficient vault 🐉")
        conn.commit()
        return {"status": "deducted"}
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/api/convert/to_vault")
async def convert_to_vault(data: ConvertDragons):
    if data.amount <= 0:
        raise HTTPException(400, "Amount must be positive")

    conn = get_conn()
    c = conn.cursor()
    credit_mdragons(c, data.mc_uuid, data.amount)
    conn.commit()
    conn.close()
    return {"status": "credited"}


@app.post("/api/convert/refund")
async def convert_refund(data: ConvertDragons):
    if data.amount <= 0:
        raise HTTPException(400, "Amount must be positive")

    conn = get_conn()
    try:
        begin_immediate(conn)
        c = conn.cursor()
        if data.refund_id:
            c.execute(
                "INSERT OR IGNORE INTO refund_events (refund_id, mc_uuid, amount) VALUES (?, ?, ?)",
                (data.refund_id, data.mc_uuid, data.amount),
            )
            if c.rowcount == 0:
                c.execute("SELECT mc_uuid, amount FROM refund_events WHERE refund_id=?", (data.refund_id,))
                existing = c.fetchone()
                if not existing:
                    raise HTTPException(409, "Refund already processed")
                existing_account = dragon_account_for_uuid(c, existing[0])
                requested_account = dragon_account_for_uuid(c, data.mc_uuid)
                if existing_account != requested_account or float(existing[1]) != float(data.amount):
                    raise HTTPException(409, "refund_id already used for a different refund")
                conn.rollback()
                return {"status": "refunded", "duplicate": True}

        credit_mdragons(c, data.mc_uuid, data.amount)
        conn.commit()
        return {"status": "refunded", "duplicate": False}
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.get("/api/discord_id/{mc_uuid}")
async def get_discord_id(mc_uuid: str):
    discord_id = discord_id_from_dragon_account(mc_uuid)
    if discord_id:
        return {"discord_id": discord_id}
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT discord_id FROM linked_accounts WHERE mc_uuid=?", (mc_uuid,))
    row = c.fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "No Discord account linked for this UUID")
    return {"discord_id": row[0]}


@app.get("/api/orders/user/{mc_uuid}")
async def list_user_orders(mc_uuid: str):
    conn = get_conn()
    c = conn.cursor()
    owner_keys = order_owner_keys(c, mc_uuid)
    placeholders = ",".join("?" for _ in owner_keys)
    c.execute(
        f"""SELECT id, item, remaining, price_per, side FROM orders
           WHERE mc_uuid IN ({placeholders}) AND remaining>0 AND (is_system IS NULL OR is_system=0)
           ORDER BY created DESC""",
        owner_keys,
    )
    rows = c.fetchall()
    conn.close()
    return {
        "orders": [
            {"id": r[0], "item": r[1], "remaining": r[2], "price_per": r[3], "side": r[4]}
            for r in rows
        ]
    }


@app.get("/api/top_netherite")
async def top_netherite():
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        WITH sell_orders AS (
            SELECT CASE
                       WHEN o.mc_uuid LIKE 'DISCORD_%' AND la.mc_uuid IS NOT NULL THEN la.mc_uuid
                       ELSE o.mc_uuid
                   END AS account_uuid,
                   SUM(o.remaining) AS listed
            FROM orders o
            LEFT JOIN linked_accounts la ON o.mc_uuid = 'DISCORD_' || la.discord_id
            WHERE o.item='NETHERITE_INGOT'
              AND o.side='sell'
              AND o.remaining>0
              AND (o.is_system IS NULL OR o.is_system=0)
            GROUP BY account_uuid
        ),
        account_keys AS (
            SELECT mc_uuid AS account_uuid FROM balances WHERE netherite > 0
            UNION
            SELECT account_uuid FROM sell_orders
        )
        SELECT account_keys.account_uuid,
               COALESCE(b.netherite, 0) + COALESCE(so.listed, 0) AS total
        FROM account_keys
        LEFT JOIN balances b ON b.mc_uuid = account_keys.account_uuid
        LEFT JOIN sell_orders so ON so.account_uuid = account_keys.account_uuid
        WHERE account_keys.account_uuid NOT LIKE 'SYSTEM_%'
        ORDER BY total DESC
        LIMIT 1
    """)
    row = c.fetchone()
    conn.close()
    if not row or row[1] == 0:
        return {"mc_uuid": None, "total": 0}
    return {"mc_uuid": row[0], "total": row[1]}


def _dragon_leaderboard(c: sqlite3.Cursor, limit: int, offset: int):
    c.execute(
        """
        WITH locked_orders AS (
            SELECT CASE
                       WHEN o.mc_uuid LIKE 'DISCORD_%' THEN o.mc_uuid
                       WHEN la.discord_id IS NOT NULL THEN 'DISCORD_' || la.discord_id
                       ELSE o.mc_uuid
                   END AS account_uuid,
                   SUM(o.remaining * o.price_per) AS locked
            FROM orders o
            LEFT JOIN linked_accounts la ON la.mc_uuid = o.mc_uuid
            WHERE o.side='buy'
              AND o.remaining>0
              AND (o.is_system IS NULL OR o.is_system=0)
            GROUP BY account_uuid
        ),
        account_keys AS (
            SELECT mc_uuid AS account_uuid FROM balances WHERE mdragons > 0
            UNION
            SELECT account_uuid FROM locked_orders
        )
        SELECT account_keys.account_uuid,
               COALESCE(b.mdragons, 0) AS vault,
               COALESCE(lo.locked, 0) AS locked,
               COALESCE(b.mdragons, 0) + COALESCE(lo.locked, 0) AS total
        FROM account_keys
        LEFT JOIN balances b ON b.mc_uuid = account_keys.account_uuid
        LEFT JOIN locked_orders lo ON lo.account_uuid = account_keys.account_uuid
        WHERE account_keys.account_uuid NOT LIKE 'SYSTEM_%'
          AND COALESCE(b.mdragons, 0) + COALESCE(lo.locked, 0) > 0
        ORDER BY total DESC, account_keys.account_uuid ASC
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    )
    return c.fetchall()


@app.get("/api/top_dragons")
async def top_dragons():
    conn = get_conn()
    c = conn.cursor()
    rows = _dragon_leaderboard(c, 1, 0)
    conn.close()
    if not rows:
        return {"mc_uuid": None, "total": 0}
    row = rows[0]
    return {"mc_uuid": row[0], "vault": round(row[1], 2), "locked": round(row[2], 2), "total": round(row[3], 2)}


@app.get("/api/leaderboard_dragons")
async def dragon_leaderboard(limit: int = Query(10, ge=1, le=50), offset: int = Query(0, ge=0)):
    conn = get_conn()
    c = conn.cursor()
    rows = _dragon_leaderboard(c, limit, offset)
    conn.close()
    return {
        "item": "DAEMON",
        "offset": offset,
        "limit": limit,
        "entries": [
            {
                "rank": offset + idx + 1,
                "mc_uuid": row[0],
                "vault": round(row[1], 2),
                "locked": round(row[2], 2),
                "total": round(row[3], 2),
            }
            for idx, row in enumerate(rows)
        ],
    }


@app.get("/api/leaderboard/{item}")
async def item_leaderboard(item: str, limit: int = Query(10, ge=1, le=50), offset: int = Query(0, ge=0)):
    item_key = normalize_item(item)
    if not item_key:
        raise HTTPException(400, "Invalid item.")
    if item_key == "DAEMON":
        raise HTTPException(400, "Dragon leaderboard uses the dedicated total-dragon scan.")

    conn = get_conn()
    c = conn.cursor()
    if is_legacy_item(item_key):
        col = item_col(item_key)
        c.execute(
            f"""
            WITH sell_orders AS (
                SELECT CASE
                           WHEN o.mc_uuid LIKE 'DISCORD_%' AND la.mc_uuid IS NOT NULL THEN la.mc_uuid
                           ELSE o.mc_uuid
                       END AS account_uuid,
                       SUM(o.remaining) AS listed
                FROM orders o
                LEFT JOIN linked_accounts la ON o.mc_uuid = 'DISCORD_' || la.discord_id
                WHERE o.item=?
                  AND o.side='sell'
                  AND o.remaining>0
                  AND (o.is_system IS NULL OR o.is_system=0)
                GROUP BY account_uuid
            ),
            account_keys AS (
                SELECT mc_uuid AS account_uuid FROM balances WHERE {col} > 0
                UNION
                SELECT account_uuid FROM sell_orders
            )
            SELECT account_keys.account_uuid,
                   COALESCE(b.{col}, 0) + COALESCE(so.listed, 0) AS total
            FROM account_keys
            LEFT JOIN balances b ON b.mc_uuid = account_keys.account_uuid
            LEFT JOIN sell_orders so ON so.account_uuid = account_keys.account_uuid
            WHERE COALESCE(b.{col}, 0) + COALESCE(so.listed, 0) > 0
            ORDER BY total DESC, account_keys.account_uuid ASC
            LIMIT ? OFFSET ?
            """,
            (item_key, limit, offset),
        )
    else:
        c.execute(
            """
            WITH vaults AS (
                SELECT mc_uuid AS account_uuid, amount AS vault
                FROM commodity_balances
                WHERE commodity=?
            ),
            sell_orders AS (
                SELECT CASE
                           WHEN o.mc_uuid LIKE 'DISCORD_%' AND la.mc_uuid IS NOT NULL THEN la.mc_uuid
                           ELSE o.mc_uuid
                       END AS account_uuid,
                       SUM(o.remaining) AS listed
                FROM orders o
                LEFT JOIN linked_accounts la ON o.mc_uuid = 'DISCORD_' || la.discord_id
                WHERE o.item=?
                  AND o.side='sell'
                  AND o.remaining>0
                  AND (o.is_system IS NULL OR o.is_system=0)
                GROUP BY account_uuid
            ),
            account_keys AS (
                SELECT account_uuid FROM vaults
                UNION
                SELECT account_uuid FROM sell_orders
            )
            SELECT account_keys.account_uuid,
                   COALESCE(v.vault, 0) + COALESCE(so.listed, 0) AS total
            FROM account_keys
            LEFT JOIN vaults v ON v.account_uuid = account_keys.account_uuid
            LEFT JOIN sell_orders so ON so.account_uuid = account_keys.account_uuid
            WHERE COALESCE(v.vault, 0) + COALESCE(so.listed, 0) > 0
            ORDER BY total DESC, account_keys.account_uuid ASC
            LIMIT ? OFFSET ?
            """,
            (item_key, item_key, limit, offset),
        )
    rows = c.fetchall()
    conn.close()
    return {
        "item": item_key,
        "offset": offset,
        "limit": limit,
        "entries": [
            {"rank": offset + idx + 1, "mc_uuid": row[0], "total": row[1]}
            for idx, row in enumerate(rows)
        ],
    }


@app.get("/api/market")
async def market_endpoint(item: str = Query(...), spread: Optional[float] = Query(None)):
    item_key = normalize_item(item)
    if not item_key:
        raise HTTPException(400, f"Invalid item: '{item}'")

    conn = get_conn()
    c = conn.cursor()

    c.execute(
        """SELECT price_per, remaining FROM orders
           WHERE item=? AND side='sell' AND remaining>0
           AND (is_system IS NULL OR is_system=0)
           ORDER BY price_per ASC""",
        (item_key,),
    )
    rows_asks = c.fetchall()
    buckets_asks: dict[float, float] = {}
    for price, qty in rows_asks:
        bk = math.floor(price / spread) * spread if spread and spread > 0 else price
        buckets_asks[bk] = buckets_asks.get(bk, 0) + qty
    asks, cum = [], 0.0
    for price, qty in sorted(buckets_asks.items())[:5]:
        cum += qty
        asks.append({"price": price, "amount": qty, "cumulative": cum})

    c.execute(
        """SELECT price_per, remaining FROM orders
           WHERE item=? AND side='buy' AND remaining>0
           AND (is_system IS NULL OR is_system=0)
           ORDER BY price_per DESC""",
        (item_key,),
    )
    rows_bids = c.fetchall()
    buckets_bids: dict[float, float] = {}
    for price, qty in rows_bids:
        bk = math.floor(price / spread) * spread if spread and spread > 0 else price
        buckets_bids[bk] = buckets_bids.get(bk, 0) + qty
    bids, cum = [], 0.0
    for price, qty in sorted(buckets_bids.items(), reverse=True)[:5]:
        cum += qty
        bids.append({"price": price, "amount": qty, "cumulative": cum})

    conn.close()
    return {"asks": asks, "bids": bids}


@app.get("/api/inventory/{mc_uuid}/{item}")
async def inventory(mc_uuid: str, item: str):
    item_key = normalize_item(item)
    if not item_key:
        raise HTTPException(400, "Invalid item.")

    conn = get_conn()
    c = conn.cursor()
    owner_keys = order_owner_keys(c, mc_uuid)
    item_owner = item_account_for_order_owner(c, order_owner_uuid(c, mc_uuid))
    vault = _get_item_balance(c, item_owner, item_key)
    placeholders = ",".join("?" for _ in owner_keys)

    c.execute(
        f"""SELECT COALESCE(SUM(remaining), 0) FROM orders
           WHERE mc_uuid IN ({placeholders}) AND item=? AND side='sell' AND remaining>0
           AND (is_system IS NULL OR is_system=0)""",
        (*owner_keys, item_key),
    )
    in_orders = c.fetchone()[0]
    conn.close()
    return {"vault": vault, "in_orders": in_orders, "total": vault + in_orders}


# ─────────────────────────────────────────────────────────────────
# SPECIAL MECHANICS
# ─────────────────────────────────────────────────────────────────
@app.post("/api/mechanics/announce")
async def announce_mechanic(data: AnnounceMechanic):
    role = data.role.upper()
    item_key = normalize_item(data.item)
    if role not in ("MANSA_MUSA", "NETHERITE_OVERLORD"):
        raise HTTPException(400, "role must be MANSA_MUSA or NETHERITE_OVERLORD")
    if not item_key:
        raise HTTPException(400, "Invalid item")
    if item_key not in ("DIAMOND", "NETHERITE_INGOT"):
        raise HTTPException(400, "Mechanic target must be diamond or netherite only")

    side = "buy" if role == "MANSA_MUSA" else "sell"
    activates_at = get_next_activation_time()
    now = datetime.now(GERMAN_TZ)

    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT status, pending_item, current_item FROM special_mechanics WHERE role=?", (role,))
    existing = c.fetchone()

    if existing:
        status, pending_item, current_item = existing
        if pending_item:
            conn.close()
            raise HTTPException(
                400,
                f"A market switch to **{pending_item}** is already locked in and cannot be changed. "
                f"It will activate at the next Saturday reset. "
                f"You may choose again after that reset fires."
            )
        c.execute(
            """UPDATE special_mechanics
               SET pending_item=?, announced_at=?, activates_at=?
               WHERE role=?""",
            (item_key, now.isoformat(), activates_at.isoformat(), role),
        )
    else:
        c.execute(
            """INSERT INTO special_mechanics
               (role, current_item, pending_item, announced_at, activates_at,
                side, status, weekly_value_filled)
               VALUES (?,NULL,?,?,?,?,'announced',0)""",
            (role, item_key, now.isoformat(), activates_at.isoformat(), side),
        )

    conn.commit()
    conn.close()
    return {
        "status": "locked_in",
        "role": role,
        "item": item_key,
        "side": side,
        "activates_at": activates_at.isoformat(),
        "message": (
            f"{role} will inject liquidity into the {item_key} market starting "
            f"{activates_at.strftime('%A %d %b %Y at 21:00 German time')}. "
            f"This choice is now locked and cannot be undone."
        ),
    }


@app.post("/api/mechanics/set_pause")
async def set_mechanics_pause(data: SetPause):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        """UPDATE injection_pause
           SET paused=?, paused_by=?, paused_at=?
           WHERE id=1""",
        (1 if data.paused else 0, data.paused_by, datetime.now(GERMAN_TZ).isoformat()),
    )
    conn.commit()
    conn.close()
    return {"status": "ok", "paused": data.paused, "paused_by": data.paused_by}


@app.get("/api/mechanics/pause_status")
async def mechanics_pause_status():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT paused, paused_by, paused_at FROM injection_pause WHERE id=1")
    row = c.fetchone()
    conn.close()
    return {
        "paused": bool(row[0]) if row else False,
        "paused_by": row[1] if row else None,
        "paused_at": row[2] if row else None,
    }


@app.post("/api/mechanics/tick")
async def mechanics_tick():
    conn = get_conn()
    c = conn.cursor()
    last_reset_time = get_last_reset_time()
    now_german = datetime.now(GERMAN_TZ)
    results = []

    c.execute("SELECT paused FROM injection_pause WHERE id=1")
    pause_row = c.fetchone()
    injection_paused = bool(pause_row and pause_row[0])

    c.execute(
        """SELECT role, current_item, side, status, order_id,
                  reference_price, weekly_value_filled, last_reset
           FROM special_mechanics"""
    )
    mechanisms = c.fetchall()

    for mech in mechanisms:
        role, current_item, side, status, order_id, ref_price, wv_filled, last_reset_str = mech

        last_reset_dt = None
        if last_reset_str:
            last_reset_dt = datetime.fromisoformat(last_reset_str)
            if last_reset_dt.tzinfo is None:
                last_reset_dt = last_reset_dt.replace(tzinfo=GERMAN_TZ)

        needs_reset = (last_reset_dt is None) or (last_reset_dt < last_reset_time)
        if needs_reset and (current_item or status in ("announced", "frozen", "active")):
            _do_reset(c, role, last_reset_time)
            c.execute("SELECT status, order_id FROM special_mechanics WHERE role=?", (role,))
            upd = c.fetchone()
            results.append({"role": role, "action": "reset", "new_status": upd[0] if upd else None})
            continue

        if status != "active" or not ref_price or not current_item:
            continue

        if injection_paused:
            results.append({"role": role, "action": "paused", "item": current_item})
            continue

        order_id, new_status, new_price, allowed_value, days_unlocked = sync_mechanic_order(
            c,
            role=role,
            item_key=current_item,
            side=side,
            reference_price=ref_price,
            weekly_value_filled=float(wv_filled or 0.0),
            last_reset_time=last_reset_time,
            now=now_german,
            order_id=order_id,
        )
        c.execute("UPDATE special_mechanics SET order_id=?, status=? WHERE role=?", (order_id, new_status, role))
        match_orders(c, current_item)

        T = float(7 * 24 * 3600)
        elapsed = max(0.0, (now_german - last_reset_time).total_seconds())
        t = max(0.0, min(elapsed, T))
        results.append({
            "role": role,
            "action": "ticked",
            "item": current_item,
            "price": new_price,
            "allowed_value": round(allowed_value, 2),
            "days_unlocked": days_unlocked,
            "week_progress_pct": round(t / T * 100, 1),
        })

    conn.commit()
    conn.close()
    return {"results": results}


@app.get("/api/mechanics/status")
async def mechanics_status():
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        """SELECT role, current_item, pending_item, announced_at, activates_at,
                  side, status, reference_price, weekly_value_filled, last_reset
           FROM special_mechanics"""
    )
    rows = c.fetchall()
    active_target = get_active_chairman_target(c)
    pending_target = get_pending_chairman_target(c)
    role_weekly_target = active_target / 2.0
    role_daily_target = role_weekly_target / 7.0
    last_reset = get_last_reset_time()
    next_reset = last_reset + timedelta(days=7)
    now = datetime.now(GERMAN_TZ)

    result = []
    for row in rows:
        role, current_item, pending_item, announced_at, activates_at, side, status, ref_price, wv_filled, lr = row
        last_reset_dt = last_reset
        if lr:
            last_reset_dt = datetime.fromisoformat(lr)
            if last_reset_dt.tzinfo is None:
                last_reset_dt = last_reset_dt.replace(tzinfo=GERMAN_TZ)
        days_unlocked = get_mechanic_days_unlocked(last_reset_dt, now)
        allowed_value = get_mechanic_allowed_value(c, last_reset_dt, now)
        entry = {
            "role": role,
            "current_item": current_item,
            "pending_item": pending_item,
            "side": side,
            "status": status,
            "weekly_value_filled": round(wv_filled or 0, 2),
            "weekly_target": round(role_weekly_target, 2),
            "daily_target": round(role_daily_target, 2),
            "allowed_value": round(allowed_value, 2),
            "days_unlocked": days_unlocked,
            "threshold_pct": round((wv_filled or 0) / role_weekly_target * 100, 1) if role_weekly_target else 0,
            "announced_at": announced_at,
            "activates_at": activates_at,
            "last_reset": lr,
        }
        if status == "active" and ref_price and current_item:
            entry["current_price"] = compute_mechanic_price(side, ref_price, last_reset_dt, now)
            entry["reference_price"] = round(ref_price, 2)
            T = float(7 * 24 * 3600)
            elapsed = max(0.0, (now - last_reset_dt).total_seconds())
            entry["week_progress_pct"] = round(min(elapsed, T) / T * 100, 1)
        result.append(entry)

    c.execute("SELECT paused FROM injection_pause WHERE id=1")
    pause_row = c.fetchone()
    injection_paused = bool(pause_row and pause_row[0])

    conn.close()
    return {
        "mechanisms": result,
        "weekly_target": round(active_target, 2),
        "weekly_target_pending": round(pending_target, 2),
        "role_weekly_target": round(role_weekly_target, 2),
        "role_daily_target": round(role_daily_target, 2),
        "last_reset": last_reset.isoformat(),
        "next_reset": next_reset.isoformat(),
        "injection_paused": injection_paused,
    }


# ─────────────────────────────────────────────────────────────────
# DEPOSIT/WITHDRAW LOGGING + EXTERNAL GIVE
# ─────────────────────────────────────────────────────────────────
@app.post("/api/log/deposit_withdraw")
async def log_deposit_withdraw(data: DepositWithdrawLog):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        """INSERT INTO deposit_withdraw_log (mc_uuid, action, item, amount, base_units)
           VALUES (?,?,?,?,?)""",
        (data.mc_uuid, data.action, data.item, data.amount, data.base_units),
    )
    conn.commit()
    conn.close()
    return {"status": "logged"}


@app.get("/api/log/deposit_withdraw/pending")
async def get_pending_dw_logs():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id, mc_uuid, action, item, amount, base_units, timestamp FROM deposit_withdraw_log ORDER BY id")
    rows = c.fetchall()
    if rows:
        ids = [r[0] for r in rows]
        c.execute(f"DELETE FROM deposit_withdraw_log WHERE id IN ({','.join('?' * len(ids))})", ids)
    conn.commit()
    conn.close()
    return {
        "entries": [
            {
                "id": r[0],
                "mc_uuid": r[1],
                "action": r[2],
                "item": r[3],
                "amount": r[4],
                "base_units": r[5],
                "timestamp": r[6],
            }
            for r in rows
        ]
    }


@app.get("/api/log/order_events/pending")
async def get_pending_order_event_logs():
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        """SELECT id, event_type, order_id, mc_uuid, item, amount, price_per, side, timestamp
           FROM order_event_log ORDER BY id"""
    )
    rows = c.fetchall()
    if rows:
        ids = [r[0] for r in rows]
        c.execute(f"DELETE FROM order_event_log WHERE id IN ({','.join('?' * len(ids))})", ids)
    conn.commit()
    conn.close()
    return {
        "entries": [
            {
                "id": r[0],
                "event_type": r[1],
                "order_id": r[2],
                "mc_uuid": r[3],
                "item": r[4],
                "amount": r[5],
                "price_per": r[6],
                "side": r[7],
                "timestamp": r[8],
            }
            for r in rows
        ]
    }


@app.get("/api/log/trade_events/pending")
async def get_pending_trade_event_logs():
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        """SELECT id, item, buyer_uuid, seller_uuid, amount, price_per, value, timestamp
           FROM trade_event_log ORDER BY id"""
    )
    rows = c.fetchall()
    if rows:
        ids = [r[0] for r in rows]
        c.execute(f"DELETE FROM trade_event_log WHERE id IN ({','.join('?' * len(ids))})", ids)
    conn.commit()
    conn.close()
    return {
        "entries": [
            {
                "id": r[0],
                "item": r[1],
                "buyer_uuid": r[2],
                "seller_uuid": r[3],
                "amount": r[4],
                "price_per": r[5],
                "value": r[6],
                "timestamp": r[7],
            }
            for r in rows
        ]
    }


@app.get("/api/log/bounty_events/pending")
async def get_pending_bounty_event_logs():
    conn = get_conn()
    try:
        begin_immediate(conn)
        c = conn.cursor()
        c.execute(
            """SELECT id, event_type, issuer_uuid, target_uuid, target_name,
                      killer_uuid, killer_name, amount, timestamp
               FROM bounty_events
               WHERE logged=0
               ORDER BY id"""
        )
        rows = c.fetchall()
        if rows:
            ids = [r[0] for r in rows]
            c.execute(f"UPDATE bounty_events SET logged=1 WHERE id IN ({','.join('?' * len(ids))})", ids)
        conn.commit()
        return {
            "entries": [
                {
                    "id": r[0],
                    "event_type": r[1],
                    "issuer_uuid": r[2],
                    "target_uuid": r[3],
                    "target_name": r[4],
                    "killer_uuid": r[5],
                    "killer_name": r[6],
                    "amount": r[7],
                    "timestamp": r[8],
                }
                for r in rows
            ]
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/api/external/give")
async def external_give(data: ExternalGive):
    if data.amount <= 0:
        raise HTTPException(400, "Amount must be positive")

    conn = get_conn()
    try:
        begin_immediate(conn)
        c = conn.cursor()

        if data.event_id:
            c.execute(
                "INSERT OR IGNORE INTO external_credit_events (event_id, mc_uuid, amount) VALUES (?, ?, ?)",
                (data.event_id, data.mc_uuid, data.amount),
            )
            if c.rowcount == 0:
                c.execute("SELECT mc_uuid, amount FROM external_credit_events WHERE event_id=?", (data.event_id,))
                existing = c.fetchone()
                if not existing:
                    raise HTTPException(409, "External credit already processed")
                existing_account = dragon_account_for_uuid(c, existing[0])
                requested_account = dragon_account_for_uuid(c, data.mc_uuid)
                if existing_account != requested_account or int(existing[1]) != int(data.amount):
                    raise HTTPException(409, "event_id already used for a different external credit")
                conn.rollback()
                return {
                    "status": "credited",
                    "mc_uuid": data.mc_uuid,
                    "amount": data.amount,
                    "duplicate": True,
                }

        credit_mdragons(c, data.mc_uuid, data.amount)
        conn.commit()
        return {
            "status": "credited",
            "mc_uuid": data.mc_uuid,
            "amount": data.amount,
            "duplicate": False,
        }
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────
# PURCHASE LISTS
# ─────────────────────────────────────────────────────────────────
def _parse_items(items_str: str) -> dict[str, float]:
    result = {}
    for part in items_str.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            raise ValueError(f"Bad item format: '{part}' — expected item:qty")
        raw_item, raw_qty = part.split(":", 1)
        item_key = normalize_item(raw_item.strip())
        if not item_key:
            raise ValueError(f"Unknown item: '{raw_item.strip()}'")
        qty = float(raw_qty.strip())
        if qty <= 0:
            raise ValueError(f"Quantity must be > 0 for {raw_item}")
        result[item_key] = result.get(item_key, 0) + qty
    if not result:
        raise ValueError("No valid items parsed")
    return result


@app.post("/api/purchase_list/create")
async def create_purchase_list(data: CreatePurchaseList):
    try:
        parsed = _parse_items(data.items)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if data.price <= 0:
        raise HTTPException(400, "Price must be positive")
    name = data.name.strip()[:64]
    if not name:
        raise HTTPException(400, "Name is required")

    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM purchase_lists WHERE mc_uuid=?", (data.mc_uuid,))
    count = c.fetchone()[0]
    if count >= 5:
        conn.close()
        raise HTTPException(400, "You already have 5 purchase lists. Delete one to create a new one.")

    c.execute("INSERT INTO purchase_lists (mc_uuid, name, price, items) VALUES (?,?,?,?)",
              (data.mc_uuid, name, data.price, json.dumps(parsed)))
    list_id = c.lastrowid
    conn.commit()
    conn.close()
    return {"status": "created", "list_id": list_id, "name": name, "price": data.price, "items": parsed}


@app.get("/api/purchase_list/user/{mc_uuid}")
async def get_user_purchase_lists(mc_uuid: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id, name, price, items, created FROM purchase_lists WHERE mc_uuid=? ORDER BY id", (mc_uuid,))
    rows = c.fetchall()
    conn.close()
    return {
        "lists": [
            {"id": r[0], "name": r[1], "price": r[2], "items": json.loads(r[3]), "created": r[4]}
            for r in rows
        ]
    }


@app.get("/api/purchase_list/all")
async def get_all_purchase_lists():
    conn = get_conn()
    c = conn.cursor()
    c.execute("""SELECT id, mc_uuid, name, price, items, created
                 FROM purchase_lists ORDER BY id""")
    rows = c.fetchall()
    conn.close()
    return {
        "lists": [
            {"id": r[0], "owner_uuid": r[1], "name": r[2], "price": r[3], "items": json.loads(r[4]), "created": r[5]}
            for r in rows
        ]
    }


@app.delete("/api/purchase_list/delete")
async def delete_purchase_list(data: DeletePurchaseList):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT mc_uuid FROM purchase_lists WHERE id=?", (data.list_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Purchase list not found")
    if row[0] != data.mc_uuid:
        conn.close()
        raise HTTPException(403, "That is not your purchase list")
    c.execute("DELETE FROM purchase_lists WHERE id=?", (data.list_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted"}


@app.post("/api/purchase_list/fill")
async def fill_purchase_list(data: FillPurchaseList):
    conn = get_conn()
    try:
        begin_immediate(conn)
        c = conn.cursor()

        c.execute("SELECT mc_uuid, name, price, items FROM purchase_lists WHERE id=?", (data.list_id,))
        row = c.fetchone()
        if not row:
            raise HTTPException(404, "Purchase list not found")
        buyer_uuid, list_name, price, items_json = row
        items = json.loads(items_json)

        if buyer_uuid == data.mc_uuid:
            raise HTTPException(400, "You cannot fill your own purchase list")

        for item_key, qty in items.items():
            current = _get_item_balance(c, data.mc_uuid, item_key)
            if current < qty - 1e-9:
                raise HTTPException(400, f"Insufficient {item_key} in vault (need {qty})")

        if get_mdragon_balance(c, buyer_uuid) < price:
            raise HTTPException(400, "The buyer does not have enough 🐉 to pay for this list")

        for item_key, qty in items.items():
            if not _debit_item(c, data.mc_uuid, item_key, qty):
                raise HTTPException(400, f"Insufficient {item_key} in vault")
            _credit_item(c, buyer_uuid, item_key, qty)

        if not debit_mdragons(c, buyer_uuid, price):
            raise HTTPException(400, "The buyer does not have enough dragons to pay for this list")
        credit_mdragons(c, data.mc_uuid, price)

        total_qty = sum(items.values())
        if total_qty > 0:
            for item_key, qty in items.items():
                item_value = (qty / total_qty) * price
                c.execute(
                    """INSERT INTO trade_log (item, buyer_uuid, seller_uuid, amount, price_per, value)
                       VALUES (?,?,?,?,?,?)""",
                    (item_key, buyer_uuid, data.mc_uuid, qty,
                     round(item_value / qty, 4), round(item_value, 4)),
                )

        c.execute("DELETE FROM purchase_lists WHERE id=?", (data.list_id,))
        conn.commit()
        return {"status": "filled", "list_name": list_name, "price_paid": price, "items": items}
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────
# CHAIRMAN
# ─────────────────────────────────────────────────────────────────
@app.post("/api/chairman/set_injection_cap")
async def set_target(data: ChairmanTarget):
    if data.delta not in (int(TARGET_STEP), -int(TARGET_STEP)):
        raise HTTPException(400, f"delta must be +{int(TARGET_STEP)} or -{int(TARGET_STEP)}")

    now = datetime.now(GERMAN_TZ)
    last_reset = get_last_reset_time()
    reset_key = last_reset.isoformat()

    conn = get_conn()
    c = conn.cursor()

    c.execute("SELECT value FROM chairman_settings WHERE key='target_last_changed_cycle'")
    row = c.fetchone()
    if row and row[0] == reset_key:
        conn.close()
        raise HTTPException(400, "Injection cap already set for the upcoming weekly cycle. It unlocks after the next Saturday 21:00 German time reset.")

    current_active = get_active_chairman_target(c)
    current_pending = get_pending_chairman_target(c)
    min_target, max_target = get_chairman_range(c)
    new_pending = max(min_target, min(max_target, current_pending + data.delta))

    c.execute("INSERT OR REPLACE INTO chairman_settings (key, value) VALUES ('weekly_target_pending',?)", (str(float(new_pending)),))
    c.execute("INSERT OR REPLACE INTO chairman_settings (key, value) VALUES ('target_last_changed',?)", (now.date().isoformat(),))
    c.execute("INSERT OR REPLACE INTO chairman_settings (key, value) VALUES ('target_last_changed_cycle',?)", (reset_key,))
    conn.commit()
    conn.close()
    return {
        "current_active_target": current_active,
        "previous_pending_target": current_pending,
        "new_pending_target": new_pending,
        "min": min_target,
        "max": max_target,
        "activates_at": get_next_activation_time().isoformat(),
    }


@app.get("/api/chairman/injection_cap")
async def get_target():
    conn = get_conn()
    c = conn.cursor()
    active_target = get_active_chairman_target(c)
    pending_target = get_pending_chairman_target(c)
    min_target, max_target = get_chairman_range(c)
    c.execute("SELECT value FROM chairman_settings WHERE key='target_last_changed'")
    row = c.fetchone()
    conn.close()
    return {
        "weekly_target": active_target,
        "weekly_target_pending": pending_target,
        "last_changed": row[0] if row else None,
        "min": min_target,
        "max": max_target,
    }


@app.post("/api/chairman/injection_cap_range")
async def set_chairman_range(data: ChairmanRange):
    minimum = int(data.minimum)
    maximum = int(data.maximum)

    if minimum < 0 or maximum < 0:
        raise HTTPException(400, "minimum and maximum must be non-negative")
    if minimum > maximum:
        raise HTTPException(400, "minimum cannot be greater than maximum")
    if minimum % int(TARGET_STEP) != 0 or maximum % int(TARGET_STEP) != 0:
        raise HTTPException(400, f"minimum and maximum must be multiples of {int(TARGET_STEP):,}")

    conn = get_conn()
    c = conn.cursor()
    active_current = get_active_chairman_target(c)
    pending_current = get_pending_chairman_target(c)
    clamped_active = max(minimum, min(maximum, active_current))
    clamped_pending = max(minimum, min(maximum, pending_current))

    c.execute("INSERT OR REPLACE INTO chairman_settings (key, value) VALUES ('weekly_target_min',?)", (str(float(minimum)),))
    c.execute("INSERT OR REPLACE INTO chairman_settings (key, value) VALUES ('weekly_target_max',?)", (str(float(maximum)),))
    c.execute("INSERT OR REPLACE INTO chairman_settings (key, value) VALUES ('weekly_target_active',?)", (str(float(clamped_active)),))
    c.execute("INSERT OR REPLACE INTO chairman_settings (key, value) VALUES ('weekly_target_pending',?)", (str(float(clamped_pending)),))
    c.execute("INSERT OR REPLACE INTO chairman_settings (key, value) VALUES ('weekly_target',?)", (str(float(clamped_active)),))
    conn.commit()
    conn.close()

    return {
        "weekly_target": clamped_active,
        "weekly_target_pending": clamped_pending,
        "min": float(minimum),
        "max": float(maximum),
    }


# ─────────────────────────────────────────────────────────────────
# COMMODITIES
# ─────────────────────────────────────────────────────────────────
@app.post("/api/commodity/deposit")
async def commodity_deposit(data: CommodityTransaction):
    if data.commodity not in VALID_COMMODITIES:
        raise HTTPException(400, f"Unknown commodity: {data.commodity}")
    if data.amount <= 0:
        raise HTTPException(400, "Amount must be positive")

    conn = get_conn()
    try:
        begin_immediate(conn)
        c = conn.cursor()
        c.execute(
            """INSERT INTO commodity_balances (mc_uuid, commodity, amount)
               VALUES (?, ?, ?)
               ON CONFLICT(mc_uuid, commodity)
               DO UPDATE SET amount = amount + excluded.amount""",
            (data.mc_uuid, data.commodity, data.amount),
        )
        c.execute("SELECT amount FROM commodity_balances WHERE mc_uuid=? AND commodity=?",
                  (data.mc_uuid, data.commodity))
        new_bal = c.fetchone()[0]
        conn.commit()
        return {
            "status": "deposited",
            "commodity": data.commodity,
            "deposited": data.amount,
            "new_balance": round(new_bal, 6),
        }
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/api/commodity/withdraw")
async def commodity_withdraw(data: CommodityTransaction):
    if data.commodity not in VALID_COMMODITIES:
        raise HTTPException(400, f"Unknown commodity: {data.commodity}")
    if data.amount <= 0:
        raise HTTPException(400, "Amount must be positive")

    conn = get_conn()
    try:
        begin_immediate(conn)
        c = conn.cursor()
        c.execute("SELECT amount FROM commodity_balances WHERE mc_uuid=? AND commodity=?",
                  (data.mc_uuid, data.commodity))
        row = c.fetchone()
        current = row[0] if row else 0.0
        if current < data.amount - 1e-9:
            raise HTTPException(400, f"Insufficient {data.commodity}. Have {round(current, 4)}, need {data.amount}")

        new_bal = max(0.0, current - data.amount)
        c.execute(
            """INSERT INTO commodity_balances (mc_uuid, commodity, amount)
               VALUES (?, ?, ?)
               ON CONFLICT(mc_uuid, commodity)
               DO UPDATE SET amount = ?""",
            (data.mc_uuid, data.commodity, new_bal, new_bal),
        )
        conn.commit()
        return {
            "status": "withdrawn",
            "commodity": data.commodity,
            "withdrawn": data.amount,
            "new_balance": round(new_bal, 6),
        }
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.get("/api/commodity/balance/{mc_uuid}")
async def commodity_balance(mc_uuid: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        """SELECT commodity, amount FROM commodity_balances
           WHERE mc_uuid=? AND amount > 0.000001
           ORDER BY commodity""",
        (mc_uuid,),
    )
    rows = c.fetchall()
    conn.close()
    return {"balances": {r[0]: round(r[1], 6) for r in rows}}
