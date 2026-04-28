import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiohttp
import asyncio
import math
import re
import sqlite3
import json
import uuid
import time
import random
import os
import shlex
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo
import logging


def _env_int(name: str, default: int = 0) -> int:
    """Read an integer environment variable.

    Defaults to 0 so public copies do not grant permissions or target private
    channels until the operator explicitly configures IDs.
    """
    raw = os.environ.get(name, "").strip()
    if raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


def _env_int_set(name: str) -> set[int]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return set()
    result: set[int] = set()
    for part in raw.split(","):
        value = part.strip()
        if not value:
            continue
        try:
            result.add(int(value))
        except ValueError as exc:
            raise RuntimeError(f"{name} must contain comma-separated integer IDs") from exc
    return result


def _env_initial_balances() -> dict[int, int]:
    """Read optional DAEMON seed balances from JSON.

    Example:
      DAEMON_INITIAL_BALANCES='{"123": 7200}'
    """
    raw = os.environ.get("DAEMON_INITIAL_BALANCES", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        balances: dict[int, int] = {}
        for user_id, amount in data.items():
            parsed_amount = int(amount)
            if parsed_amount > 0:
                balances[int(user_id)] = parsed_amount
        return balances
    except Exception as exc:
        raise RuntimeError("DAEMON_INITIAL_BALANCES must be a JSON object of Discord user IDs to positive integer balances") from exc


# ─────────────────────────────────────────────────────────────────
# CONFIG — Economy bot
# ─────────────────────────────────────────────────────────────────
TOKEN    = os.environ.get("DISCORD_TOKEN")
BASE_URL = os.environ.get("BACKEND_URL", "http://mdragons-backend:8000/api").rstrip("/")
API_KEY = os.environ.get("API_KEY", "").strip()
API_HEADERS = {"X-API-Key": API_KEY} if API_KEY else {}

# ─── Role IDs ─────────────────────────────────────────────────────
CASHOUT_ROLE_ID            = _env_int("CASHOUT_ROLE_ID")
MDRAGONS_ROLE_ID           = _env_int("MDRAGONS_ROLE_ID")
TEN_MDRAGONS_ROLE_ID       = _env_int("TEN_MDRAGONS_ROLE_ID")
TEN_CASHOUT_ROLE_ID        = _env_int("TEN_CASHOUT_ROLE_ID")
CHAIRMAN_ROLE_ID           = _env_int("CHAIRMAN_ROLE_ID")
MANSA_MUSA_ROLE_ID         = _env_int("MANSA_MUSA_ROLE_ID")
NETHERITE_OVERLORD_ROLE_ID = _env_int("NETHERITE_OVERLORD_ROLE_ID")
SATOSHI_NAKAMOTO_ROLE_ID   = _env_int("SATOSHI_NAKAMOTO_ROLE_ID")

# ─── Channel IDs ──────────────────────────────────────────────────
ANNOUNCEMENT_CHANNEL_ID    = _env_int("ANNOUNCEMENT_CHANNEL_ID")
TRANSACTION_LOG_CHANNEL_ID = _env_int("TRANSACTION_LOG_CHANNEL_ID")
DEPOSIT_LOG_CHANNEL_ID     = _env_int("DEPOSIT_LOG_CHANNEL_ID")
TRADE_LOG_CHANNEL_ID       = _env_int("TRADE_LOG_CHANNEL_ID")
EXTERNAL_ECONOMY_LOG_ID    = _env_int("EXTERNAL_ECONOMY_LOG_ID")
UNBELIEVABOAT_BOT_ID       = _env_int("UNBELIEVABOAT_BOT_ID")
EXTERNAL_ECONOMY_BOT_ID    = _env_int("EXTERNAL_ECONOMY_BOT_ID")
EXTERNAL_ECONOMY_SCANNER   = _env_int("EXTERNAL_ECONOMY_SCANNER")
COMMIT_LOG_CHANNEL_ID      = _env_int("COMMIT_LOG_CHANNEL_ID")

# ─── Special permissions ──────────────────────────────────────────
PAUSE_INJECTION_USER_ID = _env_int("PAUSE_INJECTION_USER_ID")

# ─── Thresholds & rewards ─────────────────────────────────────────
CASHOUT_THRESHOLD_1 = 35_000
CASHOUT_THRESHOLD_2 = 350_000
MDRAGONS_REWARD     = 35_000
TEN_MDRAGONS_REWARD = 350_000
CASHOUT_COOLDOWN    = 60  # seconds

# ─── Colour palette ───────────────────────────────────────────────
BLUE  = 0x1E90FF
GREEN = 0x00C853
RED   = 0xE53935

# ─── Per-user cashout cooldown tracker ───────────────────────────
_cashout_cooldowns: dict[int, float] = {}

# ─────────────────────────────────────────────────────────────────
# CONFIG — Daemon bot
# ─────────────────────────────────────────────────────────────────
DAEMON_EMOJI        = os.environ.get("DAEMON_EMOJI", "DAEMON")
TRANSACTION_CHANNEL = _env_int("DAEMON_TRANSACTION_CHANNEL_ID")
ARENA_CHANNEL       = _env_int("ARENA_CHANNEL_ID")
ADMIN_USER_ID       = _env_int("ADMIN_USER_ID")
GAMEFIX_USER_ID     = _env_int("GAMEFIX_USER_ID")

MAX_SUPPLY         = 21_000_000
INITIAL_DAILY      = 7_200
HALVING_THRESHOLD  = 2_100_000
HALVING_MULTIPLIER = 0.9

PROPOSAL_BASE_HOURS = 7 * 24
PROPOSAL_COOLDOWN   = 17 * 24 * 3600
PROPOSAL_BID_EVERY  = 2
SYNC_COMMANDS_TO_GUILDS = os.environ.get("SYNC_COMMANDS_TO_GUILDS", "1").strip().lower() not in {"0", "false", "no"}
COMMAND_SYNC_GUILD_IDS = [
    int(gid.strip())
    for gid in os.environ.get("COMMAND_SYNC_GUILD_IDS", "").split(",")
    if gid.strip().isdigit()
]
PREFIX_COMMAND_BOT_IDS = _env_int_set("PREFIX_COMMAND_BOT_IDS")

CEST = ZoneInfo("Europe/Berlin")

INITIAL_BALANCES = _env_initial_balances()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")
_arena_resolution_lock = asyncio.Lock()
_daemon_market_lock = asyncio.Lock()
_http_session: Optional[aiohttp.ClientSession] = None
_commands_synced = False


async def get_http_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        timeout = aiohttp.ClientTimeout(total=30)
        _http_session = aiohttp.ClientSession(timeout=timeout, headers=API_HEADERS)
    return _http_session


async def close_http_session():
    global _http_session
    if _http_session is not None and not _http_session.closed:
        await _http_session.close()
    _http_session = None


async def sync_application_commands_once():
    global _commands_synced
    if _commands_synced:
        return

    try:
        synced = await bot.tree.sync()
        log.info("Synced %s global commands.", len(synced))
    except Exception:
        log.exception("Global command sync failed.")
        return

    if SYNC_COMMANDS_TO_GUILDS:
        guild_ids = COMMAND_SYNC_GUILD_IDS or [guild.id for guild in bot.guilds]
        seen: set[int] = set()
        for guild_id in guild_ids:
            if guild_id in seen:
                continue
            seen.add(guild_id)
            guild_obj = discord.Object(id=guild_id)
            try:
                bot.tree.copy_global_to(guild=guild_obj)
                guild_synced = await bot.tree.sync(guild=guild_obj)
                log.info("Synced %s guild commands to guild %s.", len(guild_synced), guild_id)
            except Exception:
                log.exception("Guild command sync failed for guild %s.", guild_id)

    _commands_synced = True

# ─────────────────────────────────────────────────────────────────
# DATABASE (Daemon)
# ─────────────────────────────────────────────────────────────────
DB_PATH = os.environ.get("DAEMON_DB_PATH", "/app/data/daemon.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS balances (
            user_id  INTEGER PRIMARY KEY,
            balance  INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS emission (
            id               INTEGER PRIMARY KEY CHECK (id = 1),
            cumulative_minted INTEGER NOT NULL DEFAULT 0,
            current_daily    INTEGER NOT NULL DEFAULT 7200,
            cycle_start_ts   REAL    NOT NULL,
            reduction_year   INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS arena (
            id             INTEGER PRIMARY KEY CHECK (id = 1),
            game_open      INTEGER NOT NULL DEFAULT 0,
            cycle_start_ts REAL    NOT NULL DEFAULT 0,
            pot            INTEGER NOT NULL DEFAULT 7200,
            dashboard_msg  INTEGER,
            game_date      TEXT,
            game_id        INTEGER NOT NULL DEFAULT 0,
            next_end_ts    REAL    NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS investments (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            choice      TEXT    NOT NULL CHECK (choice IN ('rock','paper','scissors')),
            amount      INTEGER NOT NULL,
            invested_at REAL    NOT NULL,
            game_date   TEXT    NOT NULL,
            game_id     INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS proposals (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            author_id      INTEGER NOT NULL,
            threshold      INTEGER NOT NULL,
            duration_hours INTEGER NOT NULL DEFAULT 192,
            text           TEXT    NOT NULL,
            yes_votes      TEXT    NOT NULL DEFAULT '[]',
            no_votes       TEXT    NOT NULL DEFAULT '[]',
            votes          TEXT    NOT NULL DEFAULT '[]',
            created_at     REAL    NOT NULL,
            closed         INTEGER NOT NULL DEFAULT 0,
            final_yes_pct  REAL
        );
        CREATE TABLE IF NOT EXISTS proposal_cooldowns (
            user_id        INTEGER PRIMARY KEY,
            last_created   REAL    NOT NULL
        );
        CREATE TABLE IF NOT EXISTS proposal_bids (
            cycle          INTEGER PRIMARY KEY,
            user_id        INTEGER NOT NULL,
            amount         INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS autosplit_settings (
            user_id          INTEGER PRIMARY KEY,
            amount_per_split INTEGER NOT NULL DEFAULT 0,
            games_remaining  INTEGER NOT NULL DEFAULT 0,
            last_game_id     INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS autosplit_config (
            id                   INTEGER PRIMARY KEY CHECK (id = 1),
            expiration_games     INTEGER NOT NULL DEFAULT 12
        );
        CREATE TABLE IF NOT EXISTS daemon_orders (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            mc_uuid     TEXT    NOT NULL,
            side        TEXT    NOT NULL CHECK (side IN ('buy','sell')),
            amount      INTEGER NOT NULL,
            price_per   REAL    NOT NULL,
            remaining   INTEGER NOT NULL,
            created_at  REAL    NOT NULL
        );
        CREATE TABLE IF NOT EXISTS daemon_meta (
            key         TEXT PRIMARY KEY,
            value       TEXT
        );
        """)

        migrations = [
            ("arena",       "game_id",       "INTEGER NOT NULL DEFAULT 0"),
            ("arena",       "next_end_ts",   "REAL    NOT NULL DEFAULT 0"),
            ("investments", "game_id",        "INTEGER NOT NULL DEFAULT 0"),
            ("proposals",   "duration_hours", "INTEGER NOT NULL DEFAULT 192"),
            ("proposals",   "yes_votes",      "TEXT    NOT NULL DEFAULT '[]'"),
            ("proposals",   "no_votes",       "TEXT    NOT NULL DEFAULT '[]'"),
            ("proposals",   "final_yes_pct",  "REAL"),
        ]
        for table, col, typedef in migrations:
            existing = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typedef}")

        if not conn.execute("SELECT id FROM emission WHERE id=1").fetchone():
            conn.execute(
                "INSERT INTO emission (id,cumulative_minted,current_daily,cycle_start_ts,reduction_year) VALUES (1,?,?,?,0)",
                (sum(INITIAL_BALANCES.values()), INITIAL_DAILY, time.time())
            )

        for uid, bal in INITIAL_BALANCES.items():
            conn.execute(
                "INSERT OR IGNORE INTO balances (user_id,balance) VALUES (?,?)", (uid, bal)
            )

        if not conn.execute("SELECT id FROM arena WHERE id=1").fetchone():
            conn.execute(
                "INSERT INTO arena (id,game_open,cycle_start_ts,pot,game_id,next_end_ts) VALUES (1,0,?,7200,0,0)",
                (time.time(),)
            )

        if not conn.execute("SELECT id FROM autosplit_config WHERE id=1").fetchone():
            conn.execute(
                "INSERT INTO autosplit_config (id, expiration_games) VALUES (1, 12)"
            )


# ─────────────────────────────────────────────────────────────────
# ARENA SCHEDULING HELPERS
# ─────────────────────────────────────────────────────────────────
def compute_next_end_ts() -> float:
    """
    Returns the Unix timestamp at which the current convergence should resolve.
    Target = tomorrow 12:00 Berlin time minus random(0..120) minutes.
    """
    offset_minutes = random.randint(0, 120)
    now_berlin = datetime.now(CEST)

    noon_tomorrow = (now_berlin + timedelta(days=1)).replace(
        hour=12,
        minute=0,
        second=0,
        microsecond=0,
    )
    candidate = noon_tomorrow - timedelta(minutes=offset_minutes)
    return candidate.timestamp()


def get_channel_safe(bot: commands.Bot, channel_id: int):
    ch = bot.get_channel(channel_id)
    if ch is not None:
        return ch
    for guild in bot.guilds:
        ch = guild.get_channel(channel_id)
        if ch is not None:
            return ch
    return None


async def fetch_channel_safe(bot: commands.Bot, channel_id: int):
    ch = get_channel_safe(bot, channel_id)
    if ch is not None:
        return ch
    try:
        return await bot.fetch_channel(channel_id)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────
# EMISSION
# ─────────────────────────────────────────────────────────────────
def get_emission() -> dict:
    with get_db() as c:
        return dict(c.execute("SELECT * FROM emission WHERE id=1").fetchone())


def update_emission(em: dict):
    with get_db() as c:
        c.execute(
            "UPDATE emission SET cumulative_minted=?,current_daily=?,cycle_start_ts=?,reduction_year=? WHERE id=1",
            (em["cumulative_minted"], em["current_daily"], em["cycle_start_ts"], em["reduction_year"])
        )


def maybe_apply_reduction(em: dict) -> dict:
    now   = time.time()
    yr365 = 365 * 24 * 3600
    circ  = get_circulating_supply()
    if em["reduction_year"] == 0:
        if circ >= HALVING_THRESHOLD:
            em["current_daily"]   = max(1, int(em["current_daily"] * HALVING_MULTIPLIER))
            em["cycle_start_ts"]  = now
            em["reduction_year"]  = 1
            update_emission(em)
    else:
        if now - em["cycle_start_ts"] >= yr365:
            em["current_daily"]   = max(1, int(em["current_daily"] * HALVING_MULTIPLIER))
            em["cycle_start_ts"]  = now
            em["reduction_year"] += 1
            update_emission(em)
    return em


def compute_payout(em: dict) -> int:
    remaining = MAX_SUPPLY - get_circulating_supply()
    return max(0, min(em["current_daily"], remaining))


# ─────────────────────────────────────────────────────────────────
# BALANCES
# ─────────────────────────────────────────────────────────────────
def get_balance(uid: int) -> int:
    with get_db() as c:
        row = c.execute("SELECT balance FROM balances WHERE user_id=?", (uid,)).fetchone()
        return row["balance"] if row else 0


def set_balance(uid: int, amt: int):
    with get_db() as c:
        c.execute(
            "INSERT INTO balances (user_id,balance) VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET balance=excluded.balance",
            (uid, amt)
        )


def add_balance(uid: int, delta: int):
    with get_db() as c:
        c.execute(
            "INSERT INTO balances (user_id,balance) VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET balance = balance + ?",
            (uid, max(0, delta), delta)
        )


def count_holders() -> int:
    with get_db() as c:
        return c.execute("SELECT COUNT(*) FROM balances WHERE balance>0").fetchone()[0]


def get_top_daemon_holder() -> Optional[int]:
    with get_db() as c:
        row = c.execute(
            "SELECT user_id, balance FROM balances WHERE balance>0 ORDER BY balance DESC, user_id ASC LIMIT 1"
        ).fetchone()
        return int(row["user_id"]) if row else None


async def update_satoshi_role(guild: Optional[discord.Guild]):
    if not guild:
        return
    role = guild.get_role(SATOSHI_NAKAMOTO_ROLE_ID)
    if not role:
        return

    claimed_raw = get_daemon_market_meta("satoshi_claim_user_id")
    claimed_id = int(claimed_raw) if claimed_raw and claimed_raw.isdigit() else None
    top_id = get_top_daemon_holder()

    for member in list(role.members):
        if member.id != claimed_id or claimed_id != top_id:
            try:
                await member.remove_roles(role, reason="No longer Satoshi Nakamoto claimant/top DAEMON holder")
            except discord.Forbidden:
                pass

    if not claimed_id or claimed_id != top_id:
        if claimed_raw:
            set_daemon_market_meta("satoshi_claim_user_id", "")
        return

    try:
        member = await guild.fetch_member(claimed_id)
    except (discord.NotFound, discord.HTTPException):
        return
    if role not in member.roles:
        await member.add_roles(role, reason="Satoshi Nakamoto claimant and top DAEMON holder")


# ─────────────────────────────────────────────────────────────────
# DAEMON MARKET
# ─────────────────────────────────────────────────────────────────
def get_daemon_market_meta(key: str) -> str | None:
    with get_db() as c:
        row = c.execute("SELECT value FROM daemon_meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None


def set_daemon_market_meta(key: str, value: str):
    with get_db() as c:
        c.execute(
            "INSERT INTO daemon_meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def get_daemon_orderbook() -> tuple[list[dict], list[dict]]:
    with get_db() as c:
        ask_rows = c.execute(
            "SELECT price_per, remaining FROM daemon_orders WHERE side='sell' AND remaining>0 ORDER BY price_per ASC, created_at ASC"
        ).fetchall()
        bid_rows = c.execute(
            "SELECT price_per, remaining FROM daemon_orders WHERE side='buy' AND remaining>0 ORDER BY price_per DESC, created_at ASC"
        ).fetchall()

    asks: list[dict] = []
    bids: list[dict] = []

    cum = 0
    current_price = None
    current_amt = 0
    for row in ask_rows:
        price = float(row["price_per"])
        amt = int(row["remaining"])
        if current_price is None or price != current_price:
            if current_price is not None:
                cum += current_amt
                asks.append({"price": current_price, "amount": current_amt, "cumulative": cum})
            current_price = price
            current_amt = amt
        else:
            current_amt += amt
    if current_price is not None:
        cum += current_amt
        asks.append({"price": current_price, "amount": current_amt, "cumulative": cum})

    cum = 0
    current_price = None
    current_amt = 0
    for row in bid_rows:
        price = float(row["price_per"])
        amt = int(row["remaining"])
        if current_price is None or price != current_price:
            if current_price is not None:
                cum += current_amt
                bids.append({"price": current_price, "amount": current_amt, "cumulative": cum})
            current_price = price
            current_amt = amt
        else:
            current_amt += amt
    if current_price is not None:
        cum += current_amt
        bids.append({"price": current_price, "amount": current_amt, "cumulative": cum})

    return asks[:5], bids[:5]


def get_user_daemon_orders(uid: int) -> list[dict]:
    with get_db() as c:
        rows = c.execute(
            "SELECT id, side, amount, remaining, price_per, created_at FROM daemon_orders WHERE user_id=? AND remaining>0 ORDER BY created_at DESC",
            (uid,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_daemon_order(order_id: int) -> dict | None:
    with get_db() as c:
        row = c.execute(
            "SELECT * FROM daemon_orders WHERE id=?",
            (order_id,),
        ).fetchone()
        return dict(row) if row else None


def insert_daemon_order(user_id: int, mc_uuid: str, side: str, amount: int, price_per: float) -> int:
    with get_db() as c:
        cur = c.execute(
            "INSERT INTO daemon_orders (user_id, mc_uuid, side, amount, price_per, remaining, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, mc_uuid, side, amount, price_per, amount, time.time()),
        )
        return int(cur.lastrowid)


def update_daemon_order_remaining(order_id: int, remaining: int):
    with get_db() as c:
        if remaining > 0:
            c.execute("UPDATE daemon_orders SET remaining=? WHERE id=?", (remaining, order_id))
        else:
            c.execute("DELETE FROM daemon_orders WHERE id=?", (order_id,))


def delete_daemon_order(order_id: int):
    with get_db() as c:
        c.execute("DELETE FROM daemon_orders WHERE id=?", (order_id,))


def get_best_daemon_orders() -> tuple[dict | None, dict | None]:
    with get_db() as c:
        ask = c.execute(
            "SELECT * FROM daemon_orders WHERE side='sell' AND remaining>0 ORDER BY price_per ASC, created_at ASC LIMIT 1"
        ).fetchone()
        bid = c.execute(
            "SELECT * FROM daemon_orders WHERE side='buy' AND remaining>0 ORDER BY price_per DESC, created_at ASC LIMIT 1"
        ).fetchone()
    return (dict(ask) if ask else None, dict(bid) if bid else None)


async def get_vault_dragons(mc_uuid: str) -> float:
    session = await get_http_session()
    async with session.get(f"{BASE_URL}/balance/{mc_uuid}") as r:
        if r.status != 200:
            detail = "Error fetching vault balance."
            try:
                detail = (await r.json()).get("detail", detail)
            except Exception:
                pass
            raise RuntimeError(detail)
        data = await r.json()
        return float(data.get("mdragons", 0.0))


async def reserve_vault_dragons(mc_uuid: str, amount: float):
    if amount <= 0:
        return
    session = await get_http_session()
    async with session.post(f"{BASE_URL}/convert/to_ub", json={"mc_uuid": mc_uuid, "amount": amount}) as r:
        if r.status != 200:
            detail = "Failed to reserve 🐉."
            try:
                detail = (await r.json()).get("detail", detail)
            except Exception:
                pass
            raise RuntimeError(detail)


async def credit_vault_dragons(mc_uuid: str, amount: float):
    if amount <= 0:
        return
    session = await get_http_session()
    async with session.post(f"{BASE_URL}/convert/to_vault", json={"mc_uuid": mc_uuid, "amount": amount}) as r:
        if r.status != 200:
            detail = "Failed to credit 🐉."
            try:
                detail = (await r.json()).get("detail", detail)
            except Exception:
                pass
            raise RuntimeError(detail)


async def refund_vault_dragons(mc_uuid: str, amount: float, refund_id: str):
    if amount <= 0:
        return
    session = await get_http_session()
    async with session.post(
        f"{BASE_URL}/convert/refund",
        json={"mc_uuid": mc_uuid, "amount": amount, "refund_id": refund_id},
    ) as r:
        if r.status != 200:
            detail = "Failed to refund 🐉."
            try:
                detail = (await r.json()).get("detail", detail)
            except Exception:
                pass
            raise RuntimeError(detail)


async def match_daemon_orders() -> list[dict]:
    trades: list[dict] = []

    while True:
        ask, bid = get_best_daemon_orders()
        if not ask or not bid:
            break
        if float(bid["price_per"]) + 1e-12 < float(ask["price_per"]):
            break

        fill = min(int(ask["remaining"]), int(bid["remaining"]))
        trade_price = float(ask["price_per"]) if float(ask["created_at"]) <= float(bid["created_at"]) else float(bid["price_per"])
        gross = fill * trade_price
        bid_price = float(bid["price_per"])
        refund = fill * max(0.0, bid_price - trade_price)

        seller_credited = False
        buyer_credited = False
        buyer_refunded = False

        try:
            await credit_vault_dragons(str(ask["mc_uuid"]), gross)
            seller_credited = True

            add_balance(int(bid["user_id"]), fill)
            buyer_credited = True

            if refund > 0:
                await refund_vault_dragons(
                    str(bid["mc_uuid"]),
                    refund,
                    f"daemon-trade-refund:{bid['id']}:{ask['id']}:{fill}:{trade_price:.8f}",
                )
                buyer_refunded = True

            ask_remaining = int(ask["remaining"]) - fill
            bid_remaining = int(bid["remaining"]) - fill
            update_daemon_order_remaining(int(ask["id"]), ask_remaining)
            update_daemon_order_remaining(int(bid["id"]), bid_remaining)
        except Exception:
            rollback_errors: list[str] = []

            if buyer_refunded:
                try:
                    await reserve_vault_dragons(str(bid["mc_uuid"]), refund)
                except Exception as rollback_exc:
                    rollback_errors.append(f"buyer refund rollback failed: {rollback_exc}")

            if buyer_credited:
                try:
                    add_balance(int(bid["user_id"]), -fill)
                except Exception as rollback_exc:
                    rollback_errors.append(f"buyer balance rollback failed: {rollback_exc}")

            if seller_credited:
                try:
                    await reserve_vault_dragons(str(ask["mc_uuid"]), gross)
                except Exception as rollback_exc:
                    rollback_errors.append(f"seller vault rollback failed: {rollback_exc}")

            if rollback_errors:
                raise RuntimeError(
                    "Daemon trade failed and rollback was incomplete: " + "; ".join(rollback_errors)
                )
            raise

        trades.append({
            "buyer_id": int(bid["user_id"]),
            "seller_id": int(ask["user_id"]),
            "amount": fill,
            "price_per": trade_price,
            "value": gross,
        })

    return trades


async def log_daemon_trades(guild: Optional[discord.Guild], trades: list[dict]):
    if not guild or not trades:
        return
    ch = guild.get_channel(TRADE_LOG_CHANNEL_ID)
    if not ch:
        return
    for t in trades:
        embed = discord.Embed(
            title="✅  DAEMON Trade Executed",
            description=(
                f"Buyer: <@{t['buyer_id']}>\n"
                f"Seller: <@{t['seller_id']}>\n"
                f"Amount: **{int(t['amount']):,} DAEMON**\n"
                f"Price: **🐉 {float(t['price_per']):,.8f}** each\n"
                f"Value: **🐉 {float(t['value']):,.4f}**"
            ),
            color=0x00C853,
        )
        try:
            await ch.send(embed=embed)
        except discord.Forbidden:
            pass


async def maybe_send_daemon_holder_log():
    if not ADMIN_USER_ID:
        return
    now = datetime.now(CEST)
    today = now.strftime("%Y-%m-%d")
    if get_daemon_market_meta("holder_log_last_sent") == today:
        return

    rows = []
    with get_db() as c:
        rows = c.execute(
            "SELECT user_id, balance FROM balances WHERE balance>0 ORDER BY balance DESC LIMIT 100"
        ).fetchall()

    try:
        user = await bot.fetch_user(ADMIN_USER_ID)
    except Exception:
        return
    if not user:
        return

    if not rows:
        await user.send(f"```\nDAEMON HOLDER LOG — {today}\n\nNo daemon holders found.\n```")
        set_daemon_market_meta("holder_log_last_sent", today)
        return

    for start in range(0, len(rows), 10):
        chunk = rows[start:start + 10]
        start_rank = start + 1
        end_rank = start + len(chunk)
        lines = [
            f"{rank:>3}. {int(row['user_id'])}  |  {int(row['balance']):,} DAEMON"
            for rank, row in enumerate(chunk, start=start_rank)
        ]
        await user.send(
            "```\n"
            f"DAEMON HOLDER LOG — {today}\n"
            f"Ranks {start_rank}-{end_rank}\n\n"
            + "\n".join(lines)
            + "\n```"
        )
        if end_rank < len(rows):
            await asyncio.sleep(2.0)

    set_daemon_market_meta("holder_log_last_sent", today)


# ─────────────────────────────────────────────────────────────────
# ARENA DATA
# ─────────────────────────────────────────────────────────────────
def get_arena() -> dict:
    with get_db() as c:
        return dict(c.execute("SELECT * FROM arena WHERE id=1").fetchone())


def set_arena(**kw):
    fields = ", ".join(f"{k}=?" for k in kw)
    with get_db() as c:
        c.execute(f"UPDATE arena SET {fields} WHERE id=1", list(kw.values()))


def get_investments(game_id: int) -> list:
    with get_db() as c:
        return c.execute(
            "SELECT * FROM investments WHERE game_id=? ORDER BY invested_at ASC", (game_id,)
        ).fetchall()


def add_investment(uid: int, choice: str, amount: int, game_date: str, game_id: int):
    with get_db() as c:
        c.execute(
            "INSERT INTO investments (user_id,choice,amount,invested_at,game_date,game_id) VALUES (?,?,?,?,?,?)",
            (uid, choice, amount, time.time(), game_date, game_id)
        )


def refund_investments(game_id: int) -> dict[int, int]:
    with get_db() as c:
        rows = c.execute("SELECT user_id, amount FROM investments WHERE game_id=?", (game_id,)).fetchall()
        refunds: dict[int, int] = {}
        for uid, amt in rows:
            refunds[uid] = refunds.get(uid, 0) + amt
        for uid, amt in refunds.items():
            c.execute(
                "INSERT INTO balances (user_id,balance) VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET balance = balance + ?",
                (uid, amt, amt)
            )
        c.execute("DELETE FROM investments WHERE game_id=?", (game_id,))
        return refunds


def delete_investments(game_id: int):
    with get_db() as c:
        c.execute("DELETE FROM investments WHERE game_id=?", (game_id,))


def get_autosplit_expiration() -> int:
    with get_db() as c:
        row = c.execute("SELECT expiration_games FROM autosplit_config WHERE id=1").fetchone()
        return row[0] if row else 12


def set_autosplit_expiration(games: int):
    with get_db() as c:
        c.execute(
            "INSERT INTO autosplit_config (id, expiration_games) VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET expiration_games=excluded.expiration_games",
            (games,)
        )


def set_autosplit(uid: int, amount: int, games_remaining: int, last_game_id: int):
    with get_db() as c:
        c.execute(
            "INSERT INTO autosplit_settings (user_id, amount_per_split, games_remaining, last_game_id) VALUES (?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET amount_per_split=excluded.amount_per_split, games_remaining=excluded.games_remaining, last_game_id=excluded.last_game_id",
            (uid, amount, games_remaining, last_game_id)
        )


def get_all_active_autosplits() -> list:
    with get_db() as c:
        return c.execute(
            "SELECT * FROM autosplit_settings WHERE amount_per_split > 0 AND games_remaining > 0"
        ).fetchall()


def expire_autosplit(uid: int):
    with get_db() as c:
        c.execute(
            "UPDATE autosplit_settings SET amount_per_split=0, games_remaining=0 WHERE user_id=?",
            (uid,)
        )


# ─────────────────────────────────────────────────────────────────
# VISUAL HELPERS
# ─────────────────────────────────────────────────────────────────
def block_bar(progress: float, length: int = 15) -> str:
    n = max(0, min(length, round(progress * length)))
    return "█" * n + "░" * (length - n)


def star_bar(progress: float, length: int = 20) -> str:
    n = max(0, min(length, round(progress * length)))
    return "★" * n + "☆" * (length - n)


def proposal_bar(pct_current: float, threshold: int, length: int = 22) -> str:
    tpos = max(0, min(length - 1, round(threshold / 100 * length)))
    cpos = max(0, min(length, round(pct_current / 100 * length)))
    bar = []
    for i in range(length):
        if i == tpos:
            bar.append("┃")
        elif i < cpos:
            bar.append("★")
        else:
            bar.append("☆")
    return "".join(bar)


def ts_now() -> str:
    return datetime.now(CEST).strftime("%Y-%m-%d %H:%M:%S CEST")


def gen_txid() -> str:
    return "DMN-" + uuid.uuid4().hex[:12].upper()


# ─────────────────────────────────────────────────────────────────
# ARENA RESOLUTION
# ─────────────────────────────────────────────────────────────────
def resolve_arena(game_id: int, pot: int):
    invs = get_investments(game_id)
    if not invs:
        return {}, [], "No players committed."

    pools: dict[str, list] = {"rock": [], "paper": [], "scissors": []}
    for inv in invs:
        pools[inv["choice"]].append({"uid": inv["user_id"], "amt": inv["amount"], "ts": inv["invested_at"]})

    totals = {k: sum(p["amt"] for p in v) for k, v in pools.items()}
    active = {k for k, v in totals.items() if v > 0}

    if len(active) == 0:
        return {}, [], "No investments at all."
    if len(active) == 1:
        return {}, [], "Only one option was committed to — void day."

    def first_ts(k): return min(p["ts"] for p in pools[k]) if pools[k] else float("inf")

    underdog_reward = pot // 2
    fight_reward = pot - underdog_reward
    def rank_key(k): return (totals[k], -first_ts(k))

    if len(active) == 2:
        ranked = sorted(active, key=rank_key)
        underdog = ranked[0]
        biggest = max(ranked, key=rank_key)
        second = [k for k in ranked if k != biggest][0]
        rps = {"rock": "scissors", "paper": "rock", "scissors": "paper"}
        winner = biggest if rps[biggest] == second else second
        if totals[underdog] == 0:
            return {}, [], "Underdog pool is empty."
        pm = {}
        _merge(pm, _split_pool(pools[underdog], underdog_reward))
        _merge(pm, _split_pool(pools[winner], fight_reward))
        lines = [
            f"🥉 UNDERDOG  ▸ {underdog.upper()} ({totals[underdog]:,})",
            f"🥇 BIGGEST   ▸ {biggest.upper()} ({totals[biggest]:,})",
            f"🥈 2ND PLACE ▸ {second.upper()} ({totals[second]:,})",
            f"⚔  FIGHT    ▸ {biggest.upper()} vs {second.upper()}",
        ]
        return pm, lines, None

    ranked   = sorted(active, key=rank_key)
    underdog = ranked[0]
    rest     = [ranked[1], ranked[2]]
    biggest  = max(rest, key=rank_key)
    second   = [k for k in rest if k != biggest][0]

    if totals[underdog] == 0:
        return {}, [], "Underdog pool is empty."

    rps    = {"rock": "scissors", "paper": "rock", "scissors": "paper"}
    winner = biggest if rps[biggest] == second else second
    loser  = second  if winner == biggest else biggest

    pm = {}
    _merge(pm, _split_pool(pools[underdog], underdog_reward))
    _merge(pm, _split_pool(pools[winner], fight_reward))

    lines = [
        f"🥉 UNDERDOG  ▸ {underdog.upper()} ({totals[underdog]:,})",
        f"🥇 BIGGEST   ▸ {biggest.upper()} ({totals[biggest]:,})",
        f"🥈 2ND PLACE ▸ {second.upper()} ({totals[second]:,})",
        f"⚔  FIGHT    ▸ {biggest.upper()} vs {second.upper()}",
    ]
    return pm, lines, None


def _split_pool(pool: list, reward: int) -> dict:
    commitments: dict[int, int] = {}
    for p in pool:
        commitments[p["uid"]] = commitments.get(p["uid"], 0) + p["amt"]
    weights = {uid: amount ** 1.5 for uid, amount in commitments.items()}
    total_w = sum(weights.values())
    if total_w <= 0 or reward <= 0:
        return {}

    payouts: dict[int, int] = {}
    remainders: list[tuple[float, int]] = []
    allocated = 0

    for uid, w in weights.items():
        exact = w / total_w * reward
        base = int(exact)
        payouts[uid] = base
        allocated += base
        remainders.append((exact - base, uid))

    leftover = reward - allocated
    for _, uid in sorted(remainders, key=lambda x: (-x[0], x[1]))[:leftover]:
        payouts[uid] += 1

    return payouts


def _merge(base: dict, extra: dict):
    for uid, amt in extra.items():
        base[uid] = base.get(uid, 0) + amt


# ─────────────────────────────────────────────────────────────────
# EMBEDS — DASHBOARD
# ─────────────────────────────────────────────────────────────────


def format_arena_display_id(game_date: str) -> str:
    base = (game_date or '')[:10]
    try:
        dt = datetime.strptime(base, "%Y-%m-%d")
        return f"#{dt.strftime('%Y/%m/%d')}"
    except Exception:
        return f"#{base.replace('-', '/')}" if base else "#????/??/??"

def _dashboard_name_for_uid(uid: int) -> str:
    try:
        for guild in bot.guilds:
            member = guild.get_member(uid)
            if member is not None:
                return member.display_name[:24]
        user = bot.get_user(uid)
        if user is not None:
            return user.display_name[:24]
    except Exception:
        pass
    return f"User {uid}"[:24]


def build_dashboard_embed(game_id: int, game_date: str, pot: int, end_ts: float) -> discord.Embed:
    invs = get_investments(game_id)

    user_totals: dict[int, int] = {}
    for inv in invs:
        user_totals[inv["user_id"]] = user_totals.get(inv["user_id"], 0) + inv["amount"]

    grand        = sum(user_totals.values())
    sorted_users = sorted(user_totals.items(), key=lambda x: -x[1])

    G = "\u001b[1;32m"; g = "\u001b[0;32m"; C = "\u001b[1;36m"
    W = "\u001b[0;37m"; Y = "\u001b[0;33m"; D = "\u001b[0;90m"; R = "\u001b[0m"

    lines = [
        "```ansi",
        f"{G}╔══════════════════════════════════════════╗",
        f"║  DAEMON ARENA  {format_arena_display_id(game_date):<16}          ║",
        f"╚══════════════════════════════════════════╝{R}",
        f"{D}  {game_date}{R}",
        f"{Y}  EMISSION ▸ {pot:,} DAEMON{R}",
        "",
    ]

    if sorted_users:
        lines += [f"{C}  ┌─ COMMITTED ──────────────────────────────┐{R}"]
        for rank, (uid, total) in enumerate(sorted_users, 1):
            pct = total / grand if grand else 0
            bar = block_bar(pct, 14)
            pct_str = f"{pct*100:4.1f}%"
            amt_str = f"{total:,}"
            name = _dashboard_name_for_uid(uid)
            lines += [
                f"{W}  │  {rank:>2}. {name}{R}",
                f"{g}  │      [{bar}] {pct_str}  {amt_str}{R}",
            ]
        lines += [f"{C}  └──────────────────────────────────────────┘{R}", ""]
    else:
        lines += [f"{D}  [ no commits yet ]{R}", ""]

    lines += [
        f"{D}  POOL SIZES  ──  \u001b[1;31m[ HIDDEN UNTIL REVEAL ]{R}",
        f"{D}  🪨 ROCK      [██████████████████]{R}",
        f"{D}  📄 PAPER     [██████████████████]{R}",
        f"{D}  ✂️ SCISSORS  [██████████████████]{R}",
        "",
        f"{Y}  TOTAL COMMITTED ▸ {grand:,} DAEMON{R}",
        "```",
    ]

    embed = discord.Embed(description="\n".join(lines), color=0x00FF41)
    embed.set_footer(text="Picks are hidden until reveal  •  Min pledge: 1 DAEMON  •  Buttons commit 111")
    return embed


# ─────────────────────────────────────────────────────────────────
# EMBEDS — REVEAL
# ─────────────────────────────────────────────────────────────────
def build_reveal_embed(
    game_id: int, game_date: str, pot: int,
    result_lines: list, payout_map: dict,
    void_reason: str | None, distributed_amount: int
) -> discord.Embed:
    invs        = get_investments(game_id)
    pool_totals = {"rock": 0, "paper": 0, "scissors": 0}
    user_inv: dict[int, dict] = {}
    grand = 0
    for inv in invs:
        pool_totals[inv["choice"]] += inv["amount"]
        grand += inv["amount"]
        uid = inv["user_id"]
        c   = inv["choice"]
        if uid not in user_inv: user_inv[uid] = {}
        user_inv[uid][c] = user_inv[uid].get(c, 0) + inv["amount"]

    G = "\u001b[1;32m"; g = "\u001b[0;32m"; C = "\u001b[1;36m"
    W = "\u001b[0;37m"; Y = "\u001b[0;33m"; D = "\u001b[0;90m"
    RR = "\u001b[1;31m"; P = "\u001b[0;35m"; R = "\u001b[0m"

    lines = [
        "```ansi",
        f"{RR}╔══════════════════════════════════════════╗",
        f"║  CONSENSUS REACHED  {format_arena_display_id(game_date):<16}     ║",
        f"╚══════════════════════════════════════════╝{R}",
        f"{D}  {game_date}{R}",
        "",
        f"{C}  ┌─ POOL REVEAL ──────────────────────────┐{R}",
    ]

    for opt, emoji in [("rock", "🪨"), ("paper", "📄"), ("scissors", "✂️")]:
        t   = pool_totals[opt]
        pct = t / grand if grand else 0
        bar = block_bar(pct, 15)
        lines.append(f"{W}  │  {emoji} {opt.upper():<9} [{bar}] {t:,}{R}")

    lines += [f"{C}  └────────────────────────────────────────┘{R}", ""]

    if void_reason:
        lines += [
            f"{Y}  ⚠  VOID DAY{R}",
            f"{Y}  {void_reason}{R}",
            f"{Y}  No DAEMON were distributed. This day is skipped.{R}",
        ]
    else:
        lines += [f"{C}  ┌─ OUTCOME ──────────────────────────────┐{R}"]
        for rl in result_lines:
            lines.append(f"{W}  │  {rl}{R}")
        lines += [f"{C}  └────────────────────────────────────────┘{R}", ""]

        if payout_map:
            lines += [f"{G}  ┌─ PAYOUTS ──────────────────────────────┐{R}"]
            for uid, coins in sorted(payout_map.items(), key=lambda x: -x[1]):
                name = _dashboard_name_for_uid(uid)
                lines.append(f"{g}  │  {name}  +{coins:,} DAEMON{R}")
            lines += [f"{G}  └────────────────────────────────────────┘{R}", ""]

        if distributed_amount > 0:
            lines.append(f"{P}  ⛏  EMITTED ▸ {distributed_amount:,} DAEMON distributed this cycle{R}")

    if user_inv:
        lines += ["", f"{C}  ┌─ PLAYER BREAKDOWN ──────────────────────┐{R}"]
        for uid, choices in user_inv.items():
            detail  = "  ".join(f"{c.upper()}:{a:,}" for c, a in choices.items())
            won     = payout_map.get(uid, 0)
            won_str = f"  {g}+{won:,}{R}" if won else f"  {D}+0{R}"
            name = _dashboard_name_for_uid(uid)
            lines  += [
                f"{W}  │  {name}{R}",
                f"{W}  │    {detail}{won_str}",
            ]
        lines.append(f"{C}  └──────────────────────────────────────────┘{R}")

    lines.append("```")
    color = 0xFF3333 if void_reason else 0x00FF41
    return discord.Embed(description="\n".join(lines), color=color)


# ─────────────────────────────────────────────────────────────────
# EMBEDS — BALANCE (Daemon)
# ─────────────────────────────────────────────────────────────────
def build_balance_embed(user: discord.Member, bal: int) -> discord.Embed:
    circ      = get_circulating_supply()
    share_pct = bal / circ * 100 if circ else 0
    bar       = block_bar(bal / circ if circ else 0, 20)

    G = "\u001b[1;32m"; g = "\u001b[0;32m"; W = "\u001b[0;37m"
    D = "\u001b[0;90m"; R = "\u001b[0m"

    lines = [
        "```ansi",
        f"{G}╔══════════════════════════════════════════╗",
        f"║          W A L L E T   A C C E S S       ║",
        f"╚══════════════════════════════════════════╝{R}",
        "",
        f"{D}  USER ▸ {user.display_name}{R}",
        f"{D}  ID   ▸ {user.id}{R}",
        "",
        f"{G}  BALANCE ▸ {bal:,} DAEMON{R}",
        f"{W}  Share of circulating supply: {share_pct:.4f}%{R}",
        f"{g}  [{bar}]{R}",
        "",
        f"{D}  {ts_now()}{R}",
        "```",
    ]
    return discord.Embed(description="\n".join(lines), color=0x00FF41)


# ─────────────────────────────────────────────────────────────────
# EMBEDS — TRANSACTION (Daemon)
# ─────────────────────────────────────────────────────────────────
def build_tx_sender_embed(recipient: discord.Member, amount: int, txid: str, hidden: bool, new_bal: int, message: str | None = None) -> discord.Embed:
    G = "\u001b[1;32m"; RR = "\u001b[1;31m"; W = "\u001b[0;37m"; D = "\u001b[0;90m"; R = "\u001b[0m"
    tag = "HIDDEN" if hidden else "PUBLIC"
    lines = [
        "```ansi",
        f"{G}╔══════════════════════════════════════════╗",
        f"║         T R A N S F E R   S E N T        ║",
        f"╚══════════════════════════════════════════╝{R}",
        "",
        f"{D}  TX-ID  ▸ {txid}{R}",
        f"{D}  MODE   ▸ {tag}{R}",
        "",
        f"{W}  TO      ▸ {recipient.display_name}{R}",
        f"{RR}  SENT    ▸ -{amount:,} DAEMON{R}",
        f"{W}  BALANCE ▸ {new_bal:,} DAEMON{R}",
    ]
    if message:
        lines += ["", f"\u001b[0;35m  MSG  ▸ {message[:200]}{R}"]
    lines += ["", f"{D}  {ts_now()}{R}",
        "```",
    ]
    return discord.Embed(description="\n".join(lines), color=0x00FF41)


def build_tx_recipient_embed(sender: discord.Member, amount: int, txid: str, hidden: bool, new_bal: int, message: str | None = None) -> discord.Embed:
    G = "\u001b[1;32m"; g = "\u001b[0;32m"; W = "\u001b[0;37m"; D = "\u001b[0;90m"; R = "\u001b[0m"
    sender_str = "ANONYMOUS" if hidden else f"{sender.display_name} ({sender.id})"
    tag        = "ANONYMOUS TRANSFER" if hidden else "TRANSFER RECEIVED"
    lines = [
        "```ansi",
        f"{G}╔══════════════════════════════════════════╗",
        f"║     {tag:<37}║",
        f"╚══════════════════════════════════════════╝{R}",
        "",
        f"{D}  TX-ID ▸ {txid}{R}",
        f"{W}  FROM  ▸ {sender_str}{R}",
        f"{g}  AMT   ▸ +{amount:,} DAEMON{R}",
        f"{W}  BAL   ▸ {new_bal:,} DAEMON{R}",
    ]
    if message:
        lines += ["", f"\u001b[0;35m  MSG  ▸ {message[:200]}{R}"]
    lines += ["", f"{D}  {ts_now()}{R}",
        "```",
    ]
    return discord.Embed(description="\n".join(lines), color=0x00FF41)


def build_tx_log_embed(sender: discord.Member, recipient: discord.Member, amount: int, txid: str, hidden: bool, message: str | None = None) -> discord.Embed:
    Y = "\u001b[1;33m"; W = "\u001b[0;37m"; g = "\u001b[0;32m"; D = "\u001b[0;90m"; R = "\u001b[0m"
    tag        = "PRIVATE" if hidden else "PUBLIC"
    sender_str = "ANONYMOUS" if hidden else f"{sender.display_name} ({sender.id})"
    lines = [
        "```ansi",
        f"{Y}╔══════════════════════════════════════════╗",
        f"║       T R A N S A C T I O N   L O G      ║",
        f"╚══════════════════════════════════════════╝{R}",
        "",
        f"{D}  TX-ID ▸ {txid}{R}",
        f"{D}  TYPE  ▸ {tag}{R}",
        "",
        f"{W}  FROM  ▸ {sender_str}{R}",
        f"{W}  TO    ▸ {recipient.display_name} ({recipient.id}){R}",
        f"{g}  AMT   ▸ {amount:,} DAEMON{R}",
    ]
    if message:
        lines += ["", f"\u001b[0;35m  MSG  ▸ {message[:200]}{R}"]
    lines += ["", f"{D}  {ts_now()}{R}",
        "```",
    ]
    color = 0x444444 if hidden else 0xF7931A
    return discord.Embed(description="\n".join(lines), color=color)


# ─────────────────────────────────────────────────────────────────
# EMBEDS — DAEMON INFO
# ─────────────────────────────────────────────────────────────────
def build_daemon_info_embed() -> discord.Embed:
    em      = get_emission()
    em      = maybe_apply_reduction(em)
    circ    = get_circulating_supply()
    daily   = em["current_daily"]
    yr      = em["reduction_year"]
    holders = count_holders()
    now     = time.time()
    yr365   = 365 * 24 * 3600

    if yr == 0:
        phase        = "INITIAL EMISSION PHASE"
        prog         = min(circ / HALVING_THRESHOLD, 1.0)
        days_elapsed = (now - em["cycle_start_ts"]) / 86400
        days_left    = max(0.0, (HALVING_THRESHOLD - circ) / daily) if daily else 0.0
        cycle_lbl    = f"Until threshold: {max(0, HALVING_THRESHOLD - circ):,} DAEMON"
    else:
        phase        = f"REDUCTION YEAR {yr}"
        elapsed      = now - em["cycle_start_ts"]
        prog         = min(elapsed / yr365, 1.0)
        days_elapsed = elapsed / 86400
        days_left    = max(0.0, (yr365 - elapsed) / 86400)
        cycle_lbl    = "Until next annual reduction"

    supply_pct = circ / MAX_SUPPLY
    G = "\u001b[1;32m"; g = "\u001b[0;32m"; C = "\u001b[1;36m"
    W = "\u001b[0;37m"; Y = "\u001b[0;33m"; D = "\u001b[0;90m"
    P = "\u001b[0;35m"; R = "\u001b[0m"

    lines = [
        "```ansi",
        f"{Y}╔══════════════════════════════════════════╗",
        f"║       D A E M O N   E C O N O M Y        ║",
        f"╚══════════════════════════════════════════╝{R}",
        "",
        f"{C}  ┌─ SUPPLY ───────────────────────────────┐{R}",
        f"{W}  │  Circulating  {circ:>14,}{R}",
        f"{W}  │  Hard Cap     {MAX_SUPPLY:>14,}{R}",
        f"{W}  │  Of hard cap  {supply_pct*100:>13.6f}%{R}",
        f"{g}  │  [{block_bar(supply_pct, 20)}]{R}",
        f"{C}  └────────────────────────────────────────┘{R}",
        "",
        f"{C}  ┌─ EMISSION ─────────────────────────────┐{R}",
        f"{P}  │  Phase        {phase}{R}",
        f"{W}  │  Daily Emission {daily:>14,}{R}",
        f"{W}  │  Elapsed      {days_elapsed:>13.1f}d{R}",
        f"{W}  │  Next change  {days_left:>13.1f}d{R}",
        f"{D}  │  {cycle_lbl}{R}",
        f"{Y}  │  [{block_bar(prog, 20)}]{R}",
        f"{C}  └────────────────────────────────────────┘{R}",
        "",
        f"{W}  Holders  ▸ {G}{holders}{R}",
        f"{D}  {ts_now()}{R}",
        "```",
    ]
    return discord.Embed(description="\n".join(lines), color=0xF7931A)


# ─────────────────────────────────────────────────────────────────
# PROPOSALS DB
# ─────────────────────────────────────────────────────────────────
def get_circulating_supply() -> int:
    with get_db() as c:
        row = c.execute("SELECT SUM(balance) FROM balances WHERE balance>0").fetchone()
        return row[0] or 1


def get_balance_weighted_yes_pct(yes_votes: list) -> float:
    circ    = get_circulating_supply()
    yes_bal = 0
    with get_db() as c:
        for uid in yes_votes:
            row = c.execute("SELECT balance FROM balances WHERE user_id=?", (uid,)).fetchone()
            if row:
                yes_bal += row["balance"]
    return yes_bal / circ * 100 if circ else 0


def build_proposal_embed(prop: dict) -> discord.Embed:
    yes_votes   = json.loads(prop.get("yes_votes") or "[]")
    no_votes    = json.loads(prop.get("no_votes")  or "[]")
    holders     = count_holders()
    yes_count   = len(yes_votes)
    no_count    = len(no_votes)
    total_votes = yes_count + no_count
    threshold   = prop["threshold"]
    duration_hours = prop.get("duration_hours", 72)

    if prop.get("closed") and prop.get("final_yes_pct") is not None:
        network_yes_pct = prop["final_yes_pct"]
    else:
        network_yes_pct = get_balance_weighted_yes_pct(yes_votes)

    sentiment_pct = yes_count / total_votes * 100 if total_votes else 0
    passed        = network_yes_pct >= threshold
    created_at    = prop.get("created_at", time.time())
    expires_ts    = int(created_at + duration_hours * 3600)
    now           = time.time()
    expired       = now > created_at + duration_hours * 3600

    G = "\u001b[1;32m"; C = "\u001b[1;36m"; W = "\u001b[0;37m"
    Y = "\u001b[0;33m"; P = "\u001b[0;35m"; D = "\u001b[0;90m"
    RR = "\u001b[1;31m"; R = "\u001b[0m"

    status_c = G if passed else (RR if expired else Y)
    status   = "PASSED ✓" if passed else ("EXPIRED" if expired else "ACTIVE")
    net_bar  = proposal_bar(network_yes_pct, threshold, 22)
    tpad     = max(0, round(threshold / 100 * 22) - 1)

    sent_n    = 22
    yes_cells = max(0, min(sent_n, round(sentiment_pct / 100 * sent_n)))
    no_cells  = sent_n - yes_cells
    sent_bar  = f"{G}" + "█" * yes_cells + f"{RR}" + "█" * no_cells + f"{R}"

    lines = [
        "```ansi",
        f"{P}╔══════════════════════════════════════════╗",
        f"║  PROPOSAL #{prop['id']:<5}                        ║",
        f"╚══════════════════════════════════════════╝{R}",
        "",
        f"{W}  {prop['text']}{R}",
        "",
        f"{C}  ┌─ VOTE STATUS ──────────────────────────┐{R}",
        f"{W}  │  Author    ▸ <@{prop['author_id']}>{R}",
        f"{W}  │  Required  ▸ {threshold}% of circulating supply (YES weight) to pass{R}",
        f"{W}  │  Duration  ▸ {duration_hours}h  (expires <t:{expires_ts}:R>){R}",
        f"{W}  │  Wallets   ▸ {holders}{R}",
        f"{W}  │  Votes     ▸ {total_votes} total  ({G}✓ {yes_count}{R}  {RR}✗ {no_count}{R}){R}",
        "",
        f"{C}  │  NETWORK AGREEMENT  ({network_yes_pct:.1f}% of circulating supply){R}",
        f"{P}  │  0%[{net_bar}]100%{R}",
        f"{D}  │      {' ' * tpad}↑ threshold ({threshold}%){R}",
        "",
        f"{C}  │  VOTER SENTIMENT  (of those who voted){R}",
        f"  │  [{sent_bar}]",
        f"{G}  │  YES {yes_count} ({sentiment_pct:.1f}%)  {RR}NO {no_count} ({100-sentiment_pct:.1f}%){R}",
        "",
        f"  │  Status ▸ {status_c}{status}{R}",
        f"{C}  └───────────────────────────────────────┘{R}",
        "",
        f"{Y}  ⚠  ADVISORY ONLY — votes are recorded but not{R}",
        f"{Y}     binding or effective at this time.{R}",
        "",
        f"{D}  {ts_now()}{R}",
        "```",
    ]
    color = 0x00FF41 if passed else 0xAA44FF
    return discord.Embed(description="\n".join(lines), color=color)


def get_active_proposal():
    with get_db() as c:
        rows = c.execute(
            "SELECT * FROM proposals WHERE closed=0 ORDER BY created_at DESC"
        ).fetchall()
    now = time.time()
    for row in rows:
        row = dict(row)
        if now <= row["created_at"] + row["duration_hours"] * 3600:
            return row
    return None


def get_proposal_cooldown(uid: int) -> float | None:
    with get_db() as c:
        row = c.execute(
            "SELECT last_created FROM proposal_cooldowns WHERE user_id=?", (uid,)
        ).fetchone()
    return row["last_created"] if row else None


def set_proposal_cooldown(uid: int):
    with get_db() as c:
        c.execute(
            "INSERT INTO proposal_cooldowns (user_id,last_created) VALUES (?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET last_created=excluded.last_created",
            (uid, time.time())
        )


def next_proposal_cycle() -> int:
    with get_db() as c:
        row = c.execute("SELECT COUNT(*) FROM proposals").fetchone()
    return (row[0] or 0) + 1


def is_biddable_slot() -> bool:
    return next_proposal_cycle() % PROPOSAL_BID_EVERY == 0


def get_current_bid() -> dict | None:
    cycle = next_proposal_cycle()
    with get_db() as c:
        row = c.execute("SELECT * FROM proposal_bids WHERE cycle=?", (cycle,)).fetchone()
    return dict(row) if row else None


def place_bid(uid: int, amount: int) -> tuple[bool, str]:
    cycle = next_proposal_cycle()
    if not is_biddable_slot():
        return False, "This proposal slot is not biddable."
    existing = get_current_bid()
    if existing and existing["amount"] >= amount:
        return False, f"Current bid is {existing['amount']:,} DAEMON. You must bid higher."
    bal = get_balance(uid)
    if bal < amount:
        return False, f"Insufficient balance: {bal:,} DAEMON."
    if existing:
        add_balance(existing["user_id"], existing["amount"])
    add_balance(uid, -amount)
    with get_db() as c:
        c.execute(
            "INSERT INTO proposal_bids (cycle,user_id,amount) VALUES (?,?,?) "
            "ON CONFLICT(cycle) DO UPDATE SET user_id=excluded.user_id, amount=excluded.amount",
            (cycle, uid, amount)
        )
    return True, f"Bid of {amount:,} DAEMON placed for slot #{cycle}."


def create_proposal(author_id: int, threshold: int, text: str) -> tuple[int | None, str]:
    if get_active_proposal():
        return None, "There is already an active proposal. Wait for it to close."

    cycle      = next_proposal_cycle()
    bid        = get_current_bid()
    bid_winner = bid["user_id"] if bid else None

    if author_id != bid_winner:
        last = get_proposal_cooldown(author_id)
        if last and time.time() - last < PROPOSAL_COOLDOWN:
            remaining = PROPOSAL_COOLDOWN - (time.time() - last)
            days_left = remaining / 86400
            return None, f"You must wait {days_left:.1f} more days before submitting another proposal."

    extra_hours    = random.randint(0, 24)
    duration_hours = PROPOSAL_BASE_HOURS + extra_hours

    with get_db() as c:
        cur = c.execute(
            "INSERT INTO proposals (author_id,threshold,duration_hours,text,yes_votes,no_votes,votes,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (author_id, threshold, duration_hours, text, "[]", "[]", "[]", time.time())
        )
        pid = cur.lastrowid

    set_proposal_cooldown(author_id)
    if bid:
        with get_db() as c:
            c.execute("DELETE FROM proposal_bids WHERE cycle=?", (cycle,))

    return pid, ""


def get_proposal(pid: int):
    with get_db() as c:
        row = c.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
        return dict(row) if row else None


def close_expired_proposals():
    now = time.time()
    with get_db() as c:
        open_props = c.execute("SELECT * FROM proposals WHERE closed=0").fetchall()
    for row in open_props:
        row = dict(row)
        if now > row["created_at"] + row["duration_hours"] * 3600:
            yes_votes = json.loads(row.get("yes_votes") or "[]")
            final_pct = get_balance_weighted_yes_pct(yes_votes)
            with get_db() as c:
                c.execute(
                    "UPDATE proposals SET closed=1, final_yes_pct=? WHERE id=?",
                    (final_pct, row["id"])
                )


def vote_proposal(pid: int, uid: int, vote: str) -> tuple[bool, str]:
    close_expired_proposals()
    prop = get_proposal(pid)
    if not prop:       return False, "Proposal not found."
    if prop["closed"]: return False, "Proposal is closed."
    now = time.time()
    if now > prop["created_at"] + prop["duration_hours"] * 3600:
        return False, "Proposal has expired."
    yes_votes = json.loads(prop.get("yes_votes") or "[]")
    no_votes  = json.loads(prop.get("no_votes")  or "[]")
    all_voted = set(yes_votes) | set(no_votes)
    if uid in all_voted: return False, "You have already voted."
    if vote == "yes":
        yes_votes.append(uid)
    else:
        no_votes.append(uid)
    all_list = yes_votes + no_votes
    with get_db() as c:
        c.execute(
            "UPDATE proposals SET yes_votes=?, no_votes=?, votes=? WHERE id=?",
            (json.dumps(yes_votes), json.dumps(no_votes), json.dumps(all_list), pid)
        )
    return True, "Vote cast."


# ─────────────────────────────────────────────────────────────────
# BOT SETUP
# ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ─────────────────────────────────────────────────────────────────
# VIEWS
# ─────────────────────────────────────────────────────────────────
class ArenaView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _invest(self, interaction: discord.Interaction, choice: str):
        arena = get_arena()
        if not arena["game_open"]:
            await interaction.response.send_message("❌ No convergence is currently active.", ephemeral=True)
            return
        uid = interaction.user.id
        bal = get_balance(uid)
        if bal < 111:
            await interaction.response.send_message(
                f"❌ Insufficient balance: `{bal:,}` {DAEMON_EMOJI}", ephemeral=True
            )
            return
        add_balance(uid, -111)
        add_investment(uid, choice, 111, arena["game_date"], arena["game_id"])

        D = "\u001b[0;90m"; G = "\u001b[1;32m"; W = "\u001b[0;37m"; Y = "\u001b[0;33m"; R = "\u001b[0m"
        choice_emoji = {"rock": "🪨 ROCK", "paper": "📄 PAPER", "scissors": "✂️  SCISSORS"}
        lines = [
            "```ansi",
            f"{G}  COMMIT CONFIRMED  ▸  111 DAEMON{R}",
            f"{D}  Arena Date   ▸ {arena['game_date'][:10]}{R}",
            f"{Y}  Pledged  ▸ {choice_emoji[choice]}{R}",
            f"{W}  Bal      ▸ {get_balance(uid):,} DAEMON{R}",
            "```",
        ]
        embed = discord.Embed(description="\n".join(lines), color=0x00FF41)
        await interaction.response.send_message(embed=embed, ephemeral=True)

        ch = get_channel_safe(bot, COMMIT_LOG_CHANNEL_ID)
        if ch:
            await ch.send(f"```diff\n+ {interaction.user.display_name} ({interaction.user.id}) committed 111 DAEMON to the arena\n```")

        await update_dashboard(get_arena())
        await update_satoshi_role(interaction.guild)

    @discord.ui.button(label="🪨  ROCK",      style=discord.ButtonStyle.secondary, custom_id="arena_rock")
    async def rock_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._invest(interaction, "rock")

    @discord.ui.button(label="📄  PAPER",     style=discord.ButtonStyle.primary,   custom_id="arena_paper")
    async def paper_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._invest(interaction, "paper")

    @discord.ui.button(label="✂️  SCISSORS",  style=discord.ButtonStyle.danger,    custom_id="arena_scissors")
    async def scissors_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._invest(interaction, "scissors")


class ProposalView(discord.ui.View):
    def __init__(self, proposal_id: int):
        super().__init__(timeout=None)
        self.proposal_id = proposal_id
        self.add_item(discord.ui.Button(
            label="✅  YES", style=discord.ButtonStyle.success,
            custom_id=f"prop_yes_{proposal_id}"
        ))
        self.add_item(discord.ui.Button(
            label="❌  NO", style=discord.ButtonStyle.danger,
            custom_id=f"prop_no_{proposal_id}"
        ))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        cid = interaction.data.get("custom_id", "")
        if cid.startswith("prop_yes_"):
            vote = "yes"
            pid  = int(cid.split("prop_yes_")[1])
        elif cid.startswith("prop_no_"):
            vote = "no"
            pid  = int(cid.split("prop_no_")[1])
        else:
            return True

        ok, msg = vote_proposal(pid, interaction.user.id, vote)
        if not ok:
            await interaction.response.send_message(f"❌ {msg}", ephemeral=True)
            return False
        prop = get_proposal(pid)
        await interaction.response.edit_message(embed=build_proposal_embed(prop), view=self)
        return False


async def _sync_dashboard_message(arena: dict | None = None, *, force_new: bool = False):
    """Single source of truth for the live arena dashboard message."""
    if arena is None:
        arena = get_arena()

    ch = await fetch_channel_safe(bot, ARENA_CHANNEL)
    if not ch:
        return None

    embed = build_dashboard_embed(
        arena["game_id"],
        arena["game_date"],
        arena["pot"],
        arena.get("next_end_ts", 0),
    )

    if not force_new and arena.get("dashboard_msg"):
        try:
            msg = await ch.fetch_message(arena["dashboard_msg"])
            await msg.edit(embed=embed, view=ArenaView())
            return msg.id
        except (discord.NotFound, discord.HTTPException):
            pass
        except Exception as e:
            print(f"[_sync_dashboard_message] edit failed: {e}")
            return None

    try:
        header = f"```ansi\n\u001b[1;32m▶ DAEMON ARENA {format_arena_display_id(arena['game_date'])} (LIVE)\u001b[0m\n```"
        msg = await ch.send(content=header, embed=embed, view=ArenaView())
        set_arena(dashboard_msg=msg.id)
        return msg.id
    except Exception as e:
        print(f"[_sync_dashboard_message] send failed: {e}")
        return None


async def resend_dashboard(arena: dict):
    return await _sync_dashboard_message(arena, force_new=True)


async def update_dashboard(arena: dict | None = None):
    return await _sync_dashboard_message(arena, force_new=False)


@tasks.loop(seconds=60)
async def arena_cycle():
    await bot.wait_until_ready()
    close_expired_proposals()

    arena = get_arena()
    now = time.time()

    if not arena["game_open"]:
        ch = await fetch_channel_safe(bot, ARENA_CHANNEL)
        if ch:
            await _boot_open_arena()
        return

    if now < arena.get("next_end_ts", 0):
        return

    if _arena_resolution_lock.locked():
        log.warning("Arena resolution already in progress — skipping this tick.")
        return

    async with _arena_resolution_lock:
        arena = get_arena()
        now = time.time()
        if not arena["game_open"] or now < arena.get("next_end_ts", 0):
            return

        ch = await fetch_channel_safe(bot, ARENA_CHANNEL)
        if not ch:
            log.warning("Arena channel not found.")
            return

        gid = arena.get("game_id") or 0
        pot = arena["pot"]

        payout_map, result_lines, void_reason = resolve_arena(gid, pot)

        em = get_emission()
        em = maybe_apply_reduction(em)
        distributed_amount = 0

        if not void_reason and payout_map:
            distributed_amount = sum(payout_map.values())
            for uid, coins in payout_map.items():
                add_balance(uid, coins)
            em["cumulative_minted"] += distributed_amount
            update_emission(em)
            await update_satoshi_role(getattr(ch, "guild", None))

        reveal_embed = build_reveal_embed(gid, arena["game_date"], pot, result_lines, payout_map, void_reason, distributed_amount)
        await ch.send(embed=reveal_embed)
        delete_investments(gid)
        set_arena(game_open=0, pot=7200)

        arena = get_arena()
        new_gid = arena["game_id"] + 1
        new_date = datetime.now(CEST).strftime("%Y-%m-%d %H:%M")
        end_ts = compute_next_end_ts()

        em = get_emission()
        pot = compute_payout(em)

        view = ArenaView()
        embed = build_dashboard_embed(new_gid, new_date, pot, end_ts)
        header = f"```ansi\n\u001b[1;32m▶ DAEMON ARENA {format_arena_display_id(new_date)}\u001b[0m\n```"
        msg = await ch.send(content=header, embed=embed, view=view)

        set_arena(game_open=1, game_date=new_date, dashboard_msg=msg.id, pot=pot, cycle_start_ts=now, game_id=new_gid, next_end_ts=end_ts)
        end_dt = datetime.fromtimestamp(end_ts, tz=CEST).strftime("%H:%M CEST")
        log.info(f"Convergence #{new_gid} opened. Ends at {end_dt} (ts={int(end_ts)})")

        await _execute_autosplits(get_channel_safe(bot, COMMIT_LOG_CHANNEL_ID), new_gid, new_date)
        await update_dashboard(get_arena())
        await update_satoshi_role(getattr(ch, "guild", None))
        await maybe_send_daemon_holder_log()


async def _execute_autosplits(arena_ch, game_id: int, game_date: str):
    if get_autosplit_expiration() == 0:
        return
    rows = get_all_active_autosplits()
    for row in rows:
        row = dict(row)
        uid = row["user_id"]
        amount = row["amount_per_split"]
        remaining = row["games_remaining"]
        last_gid = row["last_game_id"]

        if last_gid == game_id:
            continue

        share = amount // 3
        bal = get_balance(uid)

        if bal < amount:
            expire_autosplit(uid)
            try:
                user = await bot.fetch_user(uid)
                await user.send(
                    f"⚠️ Your **Daily Auto-Split** ({amount:,} DAEMON/game) has been **cancelled** because your balance (`{bal:,}`) is too low to cover the next split.\nUse `/daemon dailysplit <amount>` to re-enable it."
                )
            except Exception:
                pass
            continue

        new_remaining = remaining - 1
        set_autosplit(uid, amount, new_remaining, game_id)
        add_balance(uid, -amount)
        for choice in ("rock", "paper", "scissors"):
            add_investment(uid, choice, share, game_date, game_id)

        try:
            member = bot.get_user(uid)
            name = member.display_name if member else f"<@{uid}>"
            if arena_ch:
                await arena_ch.send(f"```diff\n+ {name} ({uid}) auto-split {amount:,} DAEMON evenly across Rock, Paper & Scissors\n```")
        except Exception:
            pass

        if new_remaining <= 0:
            expire_autosplit(uid)
            try:
                user = await bot.fetch_user(uid)
                await user.send(
                    f"ℹ️ Your **Daily Auto-Split** ({amount:,} DAEMON/game) has **expired** — it ran for the configured number of games.\nUse `/daemon dailysplit <amount>` to start a new one."
                )
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────
# ECONOMY HELPERS (MDragons)
# ─────────────────────────────────────────────────────────────────
async def get_mc_uuid(discord_id: str) -> Optional[str]:
    session = await get_http_session()
    async with session.get(f"{BASE_URL}/mc_uuid/{discord_id}") as r:
        if r.status == 200:
            return (await r.json())["mc_uuid"]
    return None


async def update_holder_role(
    guild: Optional[discord.Guild],
    endpoint: str,
    role_id: int,
    role_name: str,
):
    if not guild:
        return
    try:
        session = await get_http_session()
        async with session.get(f"{BASE_URL}/{endpoint}") as r:
            if r.status != 200:
                return
            top = await r.json()
        top_uuid = top.get("mc_uuid")
        role = guild.get_role(role_id)
        if not role:
            return

        if not top_uuid:
            for member in list(role.members):
                try:
                    await member.remove_roles(role, reason=f"{role_name} has no eligible holder")
                except discord.Forbidden:
                    pass
            return

        async with session.get(f"{BASE_URL}/discord_id/{top_uuid}") as r:
            if r.status != 200:
                for member in list(role.members):
                    try:
                        await member.remove_roles(role, reason=f"{role_name} holder is not linked to Discord")
                    except discord.Forbidden:
                        pass
                return
            top_discord_id = str((await r.json()).get("discord_id"))

        for member in list(role.members):
            if str(member.id) != top_discord_id:
                try:
                    await member.remove_roles(role, reason=f"{role_name} dethroned")
                except discord.Forbidden:
                    pass

        try:
            holder = await guild.fetch_member(int(top_discord_id))
        except (discord.NotFound, discord.HTTPException):
            return
        if role not in holder.roles:
            await holder.add_roles(role, reason=f"New {role_name}")

    except Exception:
        log.exception("[%s] update failed", role_name.lower())


async def update_netherite_overlord(guild: Optional[discord.Guild]):
    await update_holder_role(guild, "top_netherite", NETHERITE_OVERLORD_ROLE_ID, "Netherite Overlord")


async def update_mansa_musa(guild: Optional[discord.Guild]):
    await update_holder_role(guild, "top_dragons", MANSA_MUSA_ROLE_ID, "Mansa Musa")


def item_display(item_key: str) -> str:
    mapping = {"NETHERITE_INGOT": "Netherite Ingot", "DIAMOND": "Diamond", "DAEMON": "Dragons", "xp": "XP"}
    if item_key in mapping:
        return mapping[item_key]
    return item_key.replace("_", " ").title()


def err_embed(msg: str) -> discord.Embed:
    return discord.Embed(description=f"❌  {msg}", color=RED)


def ok_embed(msg: str) -> discord.Embed:
    return discord.Embed(description=f"✅  {msg}", color=BLUE)


async def log_transaction(guild: Optional[discord.Guild], title: str, description: str, color: int = BLUE):
    if not guild:
        return
    ch = guild.get_channel(TRANSACTION_LOG_CHANNEL_ID)
    if not ch:
        return
    embed = discord.Embed(title=title, description=description, color=color)
    try:
        await ch.send(embed=embed)
    except discord.Forbidden:
        pass


def format_decimal(value, decimals: int = 2) -> str:
    number = float(value or 0)
    return f"{int(number):,}" if number.is_integer() else f"{number:,.{decimals}f}"


def normalize_audit_reason(reason: Optional[str]) -> str:
    return (reason or "").strip()[:500] or "No reason provided"


def is_economy_admin(user_id: int) -> bool:
    return user_id == PAUSE_INJECTION_USER_ID


def loop_state(loop: tasks.Loop) -> str:
    return "running" if loop.is_running() else "stopped"


def create_daemon_backup() -> Optional[dict]:
    src = os.path.abspath(DB_PATH)
    if not os.path.exists(src):
        return None
    backup_dir = os.path.join(os.path.dirname(src), "backups")
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    dest = os.path.join(backup_dir, f"daemon-{stamp}.db")

    source = sqlite3.connect(src, timeout=30)
    target = sqlite3.connect(dest)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return {"path": dest, "bytes": os.path.getsize(dest)}


def format_backup_result(label: str, backup: Optional[dict], missing_path: str) -> str:
    if not backup:
        return f"{label} backup skipped: `{missing_path}` was not found"
    return f"{label} backup: `{backup['path']}` ({int(backup['bytes']):,} bytes)"


async def resolve_mc_mention(session: aiohttp.ClientSession, mc_uuid: str) -> str:
    mention = f"`{mc_uuid[:8]}...`"
    try:
        async with session.get(f"{BASE_URL}/discord_id/{mc_uuid}") as r:
            if r.status == 200:
                did = (await r.json()).get("discord_id")
                if did:
                    mention = f"<@{did}>"
    except Exception:
        log.warning("[resolve_mc_mention] failed for %s", mc_uuid)
    return mention


def parse_discord_id(raw: str) -> Optional[int]:
    token = raw.strip()
    m = re.fullmatch(r"<@!?(\d+)>", token)
    if m:
        return int(m.group(1))
    if token.isdigit():
        return int(token)
    return None


def parse_external_give_amount(text: str) -> Optional[int]:
    patterns = (
        r"Cash:\s*`?\+?\s*([0-9][0-9,._\s]*)`?",
        r"\+\s*([0-9][0-9,._\s]*)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        digits = re.sub(r"\D", "", match.group(1))
        if digits:
            return int(digits)
    return None


def resolve_exchange_response_channel(
    guild: discord.Guild,
    source_message: discord.Message,
    text: str,
) -> Optional[discord.abc.Messageable]:
    for raw_id in re.findall(r"<#(\d+)>", text):
        channel = guild.get_channel(int(raw_id))
        if channel and hasattr(channel, "send"):
            return channel
    if hasattr(source_message.channel, "send"):
        return source_message.channel
    return None


async def get_required_mc_uuid(discord_id: int) -> tuple[Optional[str], Optional[str]]:
    mc_uuid = await get_mc_uuid(str(discord_id))
    if not mc_uuid:
        return None, f"<@{discord_id}> is not linked."
    return mc_uuid, None


async def post_order_for_discord_user(
    discord_id: int,
    item_type: str,
    amount: int,
    price_per: float,
    side: str,
) -> tuple[bool, str, Optional[int]]:
    if amount <= 0 or price_per <= 0:
        return False, "Amount and price must be positive.", None
    mc_uuid, err = await get_required_mc_uuid(discord_id)
    if err:
        return False, err, None

    endpoint = "order/place_buy" if side == "buy" else "order/place"
    session = await get_http_session()
    async with session.post(
        f"{BASE_URL}/{endpoint}",
        json={"mc_uuid": mc_uuid, "item": item_type, "amount": amount, "price_per": price_per},
    ) as r:
        if r.status != 200:
            detail = (await r.json()).get("detail", "Failed to place order.")
            return False, detail, None
        data = await r.json()
    return True, "Order placed.", int(data["order_id"])


async def cancel_order_for_discord_user(discord_id: int, order_id: int) -> tuple[bool, str]:
    mc_uuid, err = await get_required_mc_uuid(discord_id)
    if err:
        return False, err
    session = await get_http_session()
    async with session.post(f"{BASE_URL}/order/cancel", json={"order_id": order_id, "mc_uuid": mc_uuid}) as r:
        if r.status != 200:
            return False, (await r.json()).get("detail", "Failed to cancel.")
    return True, "Order cancelled. Items/money returned to your vault."


async def cancel_all_orders_for_discord_user(discord_id: int, item: Optional[str] = None) -> tuple[bool, str, int]:
    mc_uuid, err = await get_required_mc_uuid(discord_id)
    if err:
        return False, err, 0
    payload: dict = {"mc_uuid": mc_uuid}
    if item:
        payload["item"] = item
    session = await get_http_session()
    async with session.post(f"{BASE_URL}/order/cancel_all", json=payload) as r:
        if r.status != 200:
            return False, (await r.json()).get("detail", "Failed to cancel."), 0
        data = await r.json()
    return True, "Orders cancelled.", int(data.get("cancelled", 0))


async def give_dragons_for_discord_users(sender_id: int, recipient_id: int, amount: int) -> tuple[bool, str]:
    if amount <= 0:
        return False, "Amount must be positive."
    if sender_id == recipient_id:
        return False, "Cannot give to yourself."

    sender_uuid, err = await get_required_mc_uuid(sender_id)
    if err:
        return False, err
    recipient_uuid, err = await get_required_mc_uuid(recipient_id)
    if err:
        return False, err

    session = await get_http_session()
    async with session.post(
        f"{BASE_URL}/give",
        json={"sender_uuid": sender_uuid, "recipient_uuid": recipient_uuid, "amount": amount},
    ) as r:
        if r.status != 200:
            return False, (await r.json()).get("detail", "Transfer failed.")
    return True, f"<@{sender_id}> sent **🐉 {amount:,}** to <@{recipient_id}>."


async def adjust_dragons_for_discord_user(discord_id: int, delta: int) -> tuple[bool, str]:
    mc_uuid, err = await get_required_mc_uuid(discord_id)
    if err:
        return False, err
    session = await get_http_session()
    async with session.post(
        f"{BASE_URL}/balance/adjust",
        json={"mc_uuid": mc_uuid, "item": "DAEMON", "delta": delta},
    ) as r:
        if r.status != 200:
            return False, (await r.json()).get("detail", "Failed to adjust dragons.")
        data = await r.json()
    old_balance = int(float(data.get("old_balance", 0)))
    new_balance = int(float(data.get("new_balance", 0)))
    verb = "added" if delta >= 0 else "removed"
    prep = "to" if delta >= 0 else "from"
    return True, (
        f"{verb.title()} **{abs(delta):,} dragons** {prep} <@{discord_id}>. "
        f"Balance: **{old_balance:,} -> {new_balance:,} dragons**."
    )


async def handle_admin_dragon_adjustment(
    interaction: discord.Interaction,
    user: discord.Member,
    amount: int,
    direction: int,
    reason: Optional[str],
    color: int,
):
    await interaction.response.defer(ephemeral=True)
    if not is_economy_admin(interaction.user.id):
        await interaction.followup.send(embed=err_embed("You cannot use this command."), ephemeral=True)
        return
    if amount <= 0:
        await interaction.followup.send(embed=err_embed("Amount must be positive."), ephemeral=True)
        return

    ok, msg = await adjust_dragons_for_discord_user(user.id, amount * direction)
    await interaction.followup.send(embed=ok_embed(msg) if ok else err_embed(msg), ephemeral=True)
    if not ok:
        return

    reason_text = normalize_audit_reason(reason)
    await log_transaction(
        interaction.guild,
        "Admin Dragon Adjustment",
        f"Admin: <@{interaction.user.id}>\nTarget: <@{user.id}>\nReason: {reason_text}\n{msg}",
        color=color,
    )
    await update_mansa_musa(interaction.guild)


async def handle_prefix_economy_message(message: discord.Message) -> bool:
    content = message.content.strip()
    if not content.startswith("!"):
        return False
    try:
        parts = shlex.split(content)
    except ValueError:
        parts = content.split()
    if not parts:
        return False

    cmd = parts[0].lower()
    if cmd == "!market" and len(parts) >= 2:
        cmd = f"!{parts[1].lower()}"
        parts = [cmd] + parts[2:]

    supported = {"!give", "!sell", "!buy", "!cancel", "!cancel_all", "!cancel-all"}
    if cmd not in supported:
        return False

    allowed_bot_author = message.author.bot and message.author.id in PREFIX_COMMAND_BOT_IDS
    if message.author.bot and not allowed_bot_author:
        return True

    try:
        if cmd == "!give":
            if allowed_bot_author:
                if len(parts) != 4:
                    await message.reply("Usage: `!give <sender_id> <recipient_id> <amount>`", mention_author=False)
                    return True
                sender_id = parse_discord_id(parts[1])
                recipient_id = parse_discord_id(parts[2])
                amount = int(parts[3])
            else:
                if len(parts) != 3:
                    await message.reply("Usage: `!give <user> <amount>`", mention_author=False)
                    return True
                sender_id = message.author.id
                recipient_id = parse_discord_id(parts[1])
                amount = int(parts[2])
            if not sender_id or not recipient_id:
                await message.reply(embed=err_embed("Could not parse user id."), mention_author=False)
                return True
            ok, msg = await give_dragons_for_discord_users(sender_id, recipient_id, amount)
            await message.reply(embed=ok_embed(msg) if ok else err_embed(msg), mention_author=False)
            if ok:
                await log_transaction(message.guild, "🐉 Economy++ Give", msg, color=GREEN)
                await update_mansa_musa(message.guild)
            return True

        if cmd in {"!sell", "!buy"}:
            side = "buy" if cmd == "!buy" else "sell"
            if allowed_bot_author:
                if len(parts) != 5:
                    await message.reply(f"Usage: `{cmd} <user_id> <item> <amount> <price_per>`", mention_author=False)
                    return True
                actor_id = parse_discord_id(parts[1])
                item_type, amount_raw, price_raw = parts[2], parts[3], parts[4]
            else:
                if len(parts) != 4:
                    await message.reply(f"Usage: `{cmd} <item> <amount> <price_per>`", mention_author=False)
                    return True
                actor_id = message.author.id
                item_type, amount_raw, price_raw = parts[1], parts[2], parts[3]
            if not actor_id:
                await message.reply(embed=err_embed("Could not parse user id."), mention_author=False)
                return True
            amount = int(amount_raw)
            price_per = float(price_raw)
            ok, msg, order_id = await post_order_for_discord_user(actor_id, item_type, amount, price_per, side)
            if ok:
                await message.reply(
                    embed=ok_embed(
                        f"{side.title()} order placed! Order ID: **#{order_id}**\n"
                        f"{amount}x {item_display(item_type)} @ **🐉 {price_per:.2f}** each"
                    ),
                    mention_author=False,
                )
                await update_netherite_overlord(message.guild)
                await update_mansa_musa(message.guild)
            else:
                await message.reply(embed=err_embed(msg), mention_author=False)
            return True

        if cmd == "!cancel":
            if allowed_bot_author:
                if len(parts) != 3:
                    await message.reply("Usage: `!cancel <user_id> <order_id>`", mention_author=False)
                    return True
                actor_id = parse_discord_id(parts[1])
                order_id = int(parts[2])
            else:
                if len(parts) != 2:
                    await message.reply("Usage: `!cancel <order_id>`", mention_author=False)
                    return True
                actor_id = message.author.id
                order_id = int(parts[1])
            if not actor_id:
                await message.reply(embed=err_embed("Could not parse user id."), mention_author=False)
                return True
            ok, msg = await cancel_order_for_discord_user(actor_id, order_id)
            await message.reply(embed=ok_embed(msg) if ok else err_embed(msg), mention_author=False)
            if ok:
                await update_netherite_overlord(message.guild)
            return True

        if cmd in {"!cancel_all", "!cancel-all"}:
            if allowed_bot_author:
                if len(parts) not in {2, 3}:
                    await message.reply("Usage: `!cancel_all <user_id> [item]`", mention_author=False)
                    return True
                actor_id = parse_discord_id(parts[1])
                item = parts[2] if len(parts) == 3 else None
            else:
                if len(parts) > 2:
                    await message.reply("Usage: `!cancel_all [item]`", mention_author=False)
                    return True
                actor_id = message.author.id
                item = parts[1] if len(parts) == 2 else None
            if not actor_id:
                await message.reply(embed=err_embed("Could not parse user id."), mention_author=False)
                return True
            ok, msg, count = await cancel_all_orders_for_discord_user(actor_id, item)
            if ok:
                item_str = f" {item}" if item else ""
                await message.reply(embed=ok_embed(f"Cancelled **{count}**{item_str} order(s)."), mention_author=False)
                await update_netherite_overlord(message.guild)
            else:
                await message.reply(embed=err_embed(msg), mention_author=False)
            return True
    except ValueError:
        await message.reply(embed=err_embed("Amount, order id, and price must be valid numbers."), mention_author=False)
        return True
    except Exception:
        log.exception("[prefix economy] failed")
        await message.reply(embed=err_embed("Command failed."), mention_author=False)
        return True

    return False


def _role_for_market_chooser(member: discord.Member) -> Optional[str]:
    role_ids = {r.id for r in member.roles}
    if MANSA_MUSA_ROLE_ID in role_ids:
        return "MANSA_MUSA"
    if NETHERITE_OVERLORD_ROLE_ID in role_ids:
        return "NETHERITE_OVERLORD"
    return None


ITEM_ALIASES = {
    "d": "diamond", "dia": "diamond", "diamond": "diamond",
    "n": "netherite", "neth": "netherite", "netherite": "netherite",
}

_last_external_msg_id: Optional[int] = None


# ─────────────────────────────────────────────────────────────────
# BACKGROUND TASKS (MDragons)
# ─────────────────────────────────────────────────────────────────
async def _run_mechanics_tick():
    try:
        session = await get_http_session()
        async with session.post(f"{BASE_URL}/mechanics/tick") as r:
            if r.status == 200:
                data = await r.json()
                for result in data.get("results", []):
                    action = result.get("action")
                    role   = result.get("role")
                    if action == "reset":
                        print(f"[mechanics] Reset → {role} | new status: {result.get('new_status')}")
                    elif action == "ticked":
                        print(f"[mechanics] Tick  → {role} | {result.get('item')} "
                              f"@ {result.get('price')} 🐉  "
                              f"({result.get('week_progress_pct')}% through week)")
                    elif action == "paused":
                        print(f"[mechanics] PAUSED → {role} | {result.get('item')} price movement skipped")
    except Exception:
        log.exception("[mechanics loop] tick failed")


@tasks.loop(minutes=30)
async def mechanics_loop():
    await _run_mechanics_tick()


@mechanics_loop.before_loop
async def before_mechanics_loop():
    await bot.wait_until_ready()


@tasks.loop(seconds=15)
async def deposit_log_loop():
    try:
        guild = discord.utils.get(bot.guilds)
        if not guild:
            return
        ch = guild.get_channel(DEPOSIT_LOG_CHANNEL_ID)
        if not ch:
            return
        session = await get_http_session()
        async with session.get(f"{BASE_URL}/log/deposit_withdraw/pending") as r:
            if r.status != 200:
                return
            data = await r.json()
        entries = data.get("entries", [])
        if not entries:
            return
        for e in entries:
            action_word  = "📥 Deposited" if e["action"] == "deposit" else "📤 Withdrew"
            action_color = 0x00C853 if e["action"] == "deposit" else 0xFF6F00
            mention = f"`{e['mc_uuid'][:8]}…`"
            try:
                async with session.get(f"{BASE_URL}/discord_id/{e['mc_uuid']}") as r2:
                    if r2.status == 200:
                        did = (await r2.json()).get("discord_id")
                        if did:
                            mention = f"<@{did}>"
            except Exception:
                log.warning("[deposit_log_loop] failed to resolve discord id for %s", e.get("mc_uuid"))
            item_label = item_display(e["item"])
            embed = discord.Embed(
                title=f"{action_word} — {item_label}",
                color=action_color,
                timestamp=datetime.fromisoformat(e["timestamp"]) if e.get("timestamp") else None
            )
            embed.add_field(name="Player",     value=mention,                        inline=True)
            embed.add_field(name="Item",       value=f"`{e['item']}`",               inline=True)
            embed.add_field(name="Quantity",   value=f"**{e['amount']}**",           inline=True)
            embed.add_field(name="Base Units", value=f"{round(e['base_units'], 4)}", inline=True)
            try:
                await ch.send(embed=embed)
            except discord.Forbidden:
                pass
            if e["item"] == "NETHERITE_INGOT":
                await update_netherite_overlord(guild)
    except Exception:
        log.exception("[deposit_log_loop] failed")


@deposit_log_loop.before_loop
async def before_deposit_log_loop():
    await bot.wait_until_ready()


@tasks.loop(seconds=15)
async def order_event_log_loop():
    try:
        guild = discord.utils.get(bot.guilds)
        if not guild:
            return
        ch = guild.get_channel(TRANSACTION_LOG_CHANNEL_ID)
        if not ch:
            return
        session = await get_http_session()
        async with session.get(f"{BASE_URL}/log/order_events/pending") as r:
            if r.status != 200:
                return
            data = await r.json()
        entries = data.get("entries", [])
        if not entries:
            return
        for e in entries:
            event_type = e.get("event_type", "placed")
            side = str(e.get("side") or "").upper()
            item_label = item_display(str(e.get("item") or ""))
            player = await resolve_mc_mention(session, str(e.get("mc_uuid") or ""))
            color = 0x1E90FF if event_type == "placed" else 0xFF6F00
            title = "Order Placed" if event_type == "placed" else "Order Cancelled"
            embed = discord.Embed(
                title=f"📋  {title}",
                description=(
                    f"{player} {event_type} **{side}** order **#{e.get('order_id')}**\n"
                    f"**{e.get('amount')}x {item_label}** @ **🐉 {float(e.get('price_per') or 0):,.4f}** each"
                ),
                color=color,
                timestamp=datetime.fromisoformat(e["timestamp"]) if e.get("timestamp") else None,
            )
            try:
                await ch.send(embed=embed)
            except discord.Forbidden:
                pass
    except Exception:
        log.exception("[order_event_log_loop] failed")


@order_event_log_loop.before_loop
async def before_order_event_log_loop():
    await bot.wait_until_ready()


@tasks.loop(seconds=15)
async def trade_event_log_loop():
    try:
        guild = discord.utils.get(bot.guilds)
        if not guild:
            return
        ch = guild.get_channel(TRADE_LOG_CHANNEL_ID)
        if not ch:
            return
        session = await get_http_session()
        async with session.get(f"{BASE_URL}/log/trade_events/pending") as r:
            if r.status != 200:
                return
            data = await r.json()
        entries = data.get("entries", [])
        if not entries:
            return
        for e in entries:
            buyer = await resolve_mc_mention(session, str(e.get("buyer_uuid") or ""))
            seller = await resolve_mc_mention(session, str(e.get("seller_uuid") or ""))
            item_label = item_display(str(e.get("item") or ""))
            embed = discord.Embed(
                title="✅  Trade Executed",
                description=(
                    f"Buyer: {buyer}\n"
                    f"Seller: {seller}\n"
                    f"Item: **{e.get('amount')}x {item_label}**\n"
                    f"Price: **🐉 {float(e.get('price_per') or 0):,.4f}** each\n"
                    f"Value: **🐉 {float(e.get('value') or 0):,.4f}**"
                ),
                color=0x00C853,
                timestamp=datetime.fromisoformat(e["timestamp"]) if e.get("timestamp") else None,
            )
            try:
                await ch.send(embed=embed)
            except discord.Forbidden:
                pass
        await update_mansa_musa(guild)
        await update_netherite_overlord(guild)
    except Exception:
        log.exception("[trade_event_log_loop] failed")


@trade_event_log_loop.before_loop
async def before_trade_event_log_loop():
    await bot.wait_until_ready()


@tasks.loop(seconds=15)
async def bounty_event_log_loop():
    try:
        guild = discord.utils.get(bot.guilds)
        if not guild:
            return
        ch = guild.get_channel(TRANSACTION_LOG_CHANNEL_ID)
        if not ch:
            return
        session = await get_http_session()
        async with session.get(f"{BASE_URL}/log/bounty_events/pending") as r:
            if r.status != 200:
                return
            data = await r.json()
        entries = data.get("entries", [])
        if not entries:
            return

        for e in entries:
            event_type = str(e.get("event_type") or "")
            amount_txt = format_decimal(e.get("amount"), decimals=2)
            target_name = str(e.get("target_name") or e.get("target_uuid") or "unknown")
            target_label = f"**{target_name}**"
            target_uuid = str(e.get("target_uuid") or "")
            if target_uuid:
                target_label += f" (`{target_uuid[:8]}...`)"

            if event_type == "claimed":
                killer = await resolve_mc_mention(session, str(e.get("killer_uuid") or ""))
                title = "Bounty Claimed"
                description = f"{killer} claimed **{amount_txt} dragons** for killing {target_label}."
                color = GREEN
            else:
                issuer = await resolve_mc_mention(session, str(e.get("issuer_uuid") or ""))
                title = "Bounty Placed"
                description = f"{issuer} placed **{amount_txt} dragons** on {target_label}."
                color = 0xFFB300

            embed = discord.Embed(
                title=title,
                description=description,
                color=color,
                timestamp=datetime.fromisoformat(e["timestamp"]) if e.get("timestamp") else None,
            )
            try:
                await ch.send(embed=embed)
            except discord.Forbidden:
                pass
        await update_mansa_musa(guild)
    except Exception:
        log.exception("[bounty_event_log_loop] failed")


@bounty_event_log_loop.before_loop
async def before_bounty_event_log_loop():
    await bot.wait_until_ready()


@tasks.loop(minutes=5)
async def holder_role_loop():
    for guild in bot.guilds:
        await update_mansa_musa(guild)
        await update_netherite_overlord(guild)


@holder_role_loop.before_loop
async def before_holder_role_loop():
    await bot.wait_until_ready()


@tasks.loop(seconds=30)
async def external_give_loop():
    global _last_external_msg_id
    try:
        guild = discord.utils.get(bot.guilds)
        if not guild:
            return
        ch = guild.get_channel(EXTERNAL_ECONOMY_LOG_ID)
        if not ch:
            return

        kwargs = {"limit": 50}
        if _last_external_msg_id:
            kwargs["after"] = discord.Object(id=_last_external_msg_id)

        valid_authors = {UNBELIEVABOAT_BOT_ID, EXTERNAL_ECONOMY_SCANNER}
        messages = [m async for m in ch.history(**kwargs, oldest_first=True)
                    if m.author.id in valid_authors]

        session = await get_http_session()
        for msg in messages:
            _last_external_msg_id = msg.id
            for embed_index, embed in enumerate(msg.embeds):
                text = (msg.content or "") + " " + (embed.description or "") + " ".join(f.value for f in embed.fields)
                if str(EXTERNAL_ECONOMY_BOT_ID) not in text and "Economy++" not in text:
                    continue
                if "give-money" not in text.lower() and "give_money" not in text.lower():
                    continue
                amount = parse_external_give_amount(text)
                if amount is None:
                    continue
                if amount <= 0:
                    continue

                sender_id = None
                for raw_id in re.findall(r'<@!?(\d+)>', text):
                    candidate = int(raw_id)
                    if candidate != EXTERNAL_ECONOMY_BOT_ID:
                        sender_id = candidate
                        break

                if not sender_id:
                    continue

                async with session.get(f"{BASE_URL}/mc_uuid/{sender_id}") as r:
                    if r.status != 200:
                        log.info("[external_give] sender %s not linked — skipping", sender_id)
                        continue
                    mc_uuid = (await r.json()).get("mc_uuid")
                if not mc_uuid:
                    continue

                event_id = f"external_give:{msg.id}:{embed_index}"
                async with session.post(
                    f"{BASE_URL}/external/give",
                    json={"mc_uuid": mc_uuid, "amount": amount, "event_id": event_id},
                ) as r2:
                    if r2.status != 200:
                        log.warning("[external_give] credit failed for %s: %s", mc_uuid, await r2.text())
                        continue
                    credit_data = await r2.json()
                if credit_data.get("duplicate"):
                    continue

                public_channel = resolve_exchange_response_channel(guild, msg, text)
                try:
                    if public_channel:
                        await public_channel.send(
                            f"<@{sender_id}> exchanged **🐉 {amount:,}** from Economy:dragon: "
                            f"into Economy++:dragon:."
                        )
                except discord.Forbidden:
                    pass

                ch_log = guild.get_channel(TRANSACTION_LOG_CHANNEL_ID)
                if ch_log:
                    log_embed = discord.Embed(
                        title="💸  External Transfer Received",
                        description=(
                            f"<@{sender_id}> sent **{amount:,} UB** via Economy++ → "
                            f"credited **🐉 {amount:,}** to their MDragons vault."
                        ),
                        color=0x00C853
                    )
                    try:
                        await ch_log.send(embed=log_embed)
                    except discord.Forbidden:
                        pass
                await update_mansa_musa(guild)
                log.info("[external_give] +%s 🐉 credited to %s (Discord: %s)", amount, mc_uuid, sender_id)

    except Exception:
        log.exception("[external_give_loop] failed")


@external_give_loop.before_loop
async def before_external_give_loop():
    await bot.wait_until_ready()


# ─────────────────────────────────────────────────────────────────
# EVENTS
# ─────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    await get_http_session()
    log.info(f"Online: {bot.user} ({bot.user.id})")
    bot.add_view(ArenaView())
    await sync_application_commands_once()

    if not mechanics_loop.is_running():
        mechanics_loop.start()
    if not deposit_log_loop.is_running():
        deposit_log_loop.start()
    if not order_event_log_loop.is_running():
        order_event_log_loop.start()
    if not trade_event_log_loop.is_running():
        trade_event_log_loop.start()
    if not bounty_event_log_loop.is_running():
        bounty_event_log_loop.start()
    if not holder_role_loop.is_running():
        holder_role_loop.start()
    if not external_give_loop.is_running():
        external_give_loop.start()
    if not arena_cycle.is_running():
        arena_cycle.start()

    await _ensure_live_dashboard()
    for guild in bot.guilds:
        await update_mansa_musa(guild)
        await update_netherite_overlord(guild)
    asyncio.create_task(_run_mechanics_tick())


@bot.event
async def on_disconnect():
    await close_http_session()


@bot.event
async def on_message(message: discord.Message):
    if bot.user and message.author.id == bot.user.id:
        return
    handled = await handle_prefix_economy_message(message)
    if not handled and not message.author.bot:
        await bot.process_commands(message)


async def _ensure_live_dashboard():
    await bot.wait_until_ready()
    arena = get_arena()
    if not arena.get("game_open"):
        await _boot_open_arena()
        return
    await update_dashboard(arena)


async def _boot_open_arena():
    await bot.wait_until_ready()
    ch = await fetch_channel_safe(bot, ARENA_CHANNEL)
    if not ch:
        return
    arena = get_arena()
    new_gid = arena["game_id"] + 1
    new_date = datetime.now(CEST).strftime("%Y-%m-%d %H:%M")
    end_ts = compute_next_end_ts()
    em = get_emission()
    pot = compute_payout(em)

    view = ArenaView()
    embed = build_dashboard_embed(new_gid, new_date, pot, end_ts)
    header = f"```ansi\n\u001b[1;32m▶ DAEMON ARENA {format_arena_display_id(new_date)}\u001b[0m\n```"
    msg = await ch.send(content=header, embed=embed, view=view)

    set_arena(game_open=1, game_date=new_date, dashboard_msg=msg.id, pot=pot, cycle_start_ts=time.time(), game_id=new_gid, next_end_ts=end_ts)
    await _execute_autosplits(get_channel_safe(bot, COMMIT_LOG_CHANNEL_ID), new_gid, new_date)
    await update_dashboard(get_arena())
    await update_satoshi_role(getattr(ch, "guild", None))
    await maybe_send_daemon_holder_log()
    end_dt = datetime.fromtimestamp(end_ts, tz=CEST).strftime("%H:%M CEST")
    log.info(f"Boot convergence #{new_gid} opened. Ends at {end_dt}")


# ─────────────────────────────────────────────────────────────────
# SLASH COMMANDS — ECONOMY CATEGORY (MDragons)
# ─────────────────────────────────────────────────────────────────
economy = app_commands.Group(name="economy", description="MDragons economy commands")
market_group = app_commands.Group(name="market", description="Market trading commands")
lists_group = app_commands.Group(name="lists", description="Purchase list commands")


@economy.command(name="link", description="Link your Minecraft account using the in-game /discord code")
@app_commands.describe(code="6-digit code shown by /discord in Minecraft")
async def link(interaction: discord.Interaction, code: str):
    await interaction.response.defer(ephemeral=True)
    session = await get_http_session()
    async with session.post(f"{BASE_URL}/link/verify",
                          json={"code": code, "discord_id": str(interaction.user.id)}) as r:
            if r.status == 200:
                await interaction.followup.send(embed=ok_embed("Account linked successfully!"), ephemeral=True)
            else:
                detail = (await r.json()).get("detail", "Failed to link.")
                await interaction.followup.send(embed=err_embed(detail), ephemeral=True)


@economy.command(name="balance", description="View your vault balance (items + 🐉)")
async def economy_balance(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    mc_uuid = await get_mc_uuid(str(interaction.user.id))
    if not mc_uuid:
        await interaction.followup.send(embed=err_embed("Account not linked. Use `/economy link` first."), ephemeral=True)
        return
    session = await get_http_session()
    async with session.get(f"{BASE_URL}/balance/{mc_uuid}") as r:
            if r.status != 200:
                await interaction.followup.send(embed=err_embed("Error fetching balance."), ephemeral=True)
                return
            data = await r.json()

    embed = discord.Embed(title="💰  Vault Balance", color=BLUE)
    embed.add_field(name="Netherite Ingots",    value=f"**{data['netherite']}**",                              inline=True)
    embed.add_field(name="Diamonds",             value=f"**{data['diamond']}**",                               inline=True)
    embed.add_field(name="\u200b",               value="\u200b",                                               inline=True)
    embed.add_field(name="🐉 Vault",             value=f"**🐉 {int(data['mdragons']):,}**",                    inline=True)
    embed.add_field(name="🐉 Locked in orders",  value=f"**🐉 {int(data.get('mdragons_locked', 0)):,}**",      inline=True)
    embed.add_field(name="🐉 Total",             value=f"**🐉 {int(data.get('mdragons_total', data['mdragons'])):,}**", inline=True)
    embed.set_footer(text="Locked = 🐉 reserved in open buy orders  ·  Use /market sell or /market buy to trade")
    await interaction.followup.send(embed=embed, ephemeral=True)


@economy.command(name="leaderboard", description="Show an item or dragon leaderboard")
@app_commands.describe(item="Item key, e.g. dragons, netherite, diamond, iron, dirt, wool", page="Page number, 10 entries per page")
async def economy_leaderboard_cmd(interaction: discord.Interaction, item: str, page: int = 1):
    await interaction.response.defer()
    page = max(1, page)
    offset = (page - 1) * 10
    session = await get_http_session()
    item_key = item.lower().replace("-", "_").replace(" ", "_")
    is_dragons = item_key in {"dragon", "dragons", "mdragons", "total", "total_dragons", "dragon_total"}
    endpoint = "leaderboard_dragons" if is_dragons else f"leaderboard/{item}"
    async with session.get(f"{BASE_URL}/{endpoint}?limit=10&offset={offset}") as r:
        if r.status != 200:
            detail = (await r.json()).get("detail", "Error fetching leaderboard.")
            await interaction.followup.send(embed=err_embed(detail))
            return
        data = await r.json()

    entries = data.get("entries", [])
    item_label = "Total Dragons" if is_dragons else item_display(data.get("item", item))
    if not entries:
        await interaction.followup.send(embed=discord.Embed(description=f"No holders found for **{item_label}**.", color=BLUE))
        return

    rows = []
    for e in entries:
        holder = await resolve_mc_mention(session, e["mc_uuid"])
        total_txt = format_decimal(e["total"], decimals=4)
        if is_dragons:
            locked = float(e.get("locked") or 0)
            locked_txt = f" (locked: 🐉 {int(locked):,})" if locked > 0 else ""
            rows.append(f"**#{e['rank']}**  {holder}  -  **🐉 {total_txt}**{locked_txt}")
        else:
            rows.append(f"**#{e['rank']}**  {holder}  -  **{total_txt}**")

    embed = discord.Embed(
        title=f"{item_label} Leaderboard",
        description="\n".join(rows),
        color=BLUE,
    )
    embed.set_footer(text=f"Page {page}")
    await interaction.followup.send(embed=embed)


@economy.command(name="bounties", description="Show active dragon bounties")
@app_commands.describe(page="Page number, 10 entries per page")
async def economy_bounties_cmd(interaction: discord.Interaction, page: int = 1):
    await interaction.response.defer()
    page = max(1, page)
    offset = (page - 1) * 10
    session = await get_http_session()
    async with session.get(f"{BASE_URL}/bounties?limit=10&offset={offset}") as r:
        if r.status != 200:
            detail = (await r.json()).get("detail", "Error fetching bounties.")
            await interaction.followup.send(embed=err_embed(detail))
            return
        data = await r.json()

    entries = data.get("entries", [])
    if not entries:
        await interaction.followup.send(embed=discord.Embed(description="No active bounties.", color=BLUE))
        return

    rows = []
    for e in entries:
        target_uuid = str(e.get("target_uuid") or "")
        target_name = str(e.get("target_name") or target_uuid[:8] or "unknown")
        target = f"**{target_name}**"
        if target_uuid:
            mention = await resolve_mc_mention(session, target_uuid)
            target = f"{mention} ({target})" if not mention.startswith("`") else f"{target} ({mention})"
        amount_txt = format_decimal(e.get("amount"), decimals=2)
        rows.append(f"**#{e['rank']}**  {target}  -  **{amount_txt} dragons**")

    embed = discord.Embed(
        title="Active Bounties",
        description="\n".join(rows),
        color=0xFFB300,
    )
    embed.set_footer(text=f"Page {page}")
    await interaction.followup.send(embed=embed)


@economy.command(name="health", description="[Admin] Check Economy++ backend and bot task health")
async def economy_health_cmd(interaction: discord.Interaction):
    if not is_economy_admin(interaction.user.id):
        await interaction.response.send_message(embed=err_embed("You cannot use this command."), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    backend_line = "Backend: unavailable"
    session = await get_http_session()
    started = time.monotonic()
    try:
        async with session.get(f"{BASE_URL}/health") as r:
            latency_ms = int((time.monotonic() - started) * 1000)
            if r.status == 200:
                data = await r.json()
                backend_line = (
                    f"Backend: {data.get('status', 'unknown')} ({latency_ms} ms)\n"
                    f"MDragons DB: `{data.get('db_path', 'unknown')}`\n"
                    f"Balance rows: `{data.get('balances', 'unknown')}`"
                )
            else:
                backend_line = f"Backend: HTTP {r.status} ({latency_ms} ms)"
    except Exception as exc:
        backend_line = f"Backend: error `{type(exc).__name__}`"

    task_rows = [
        ("command sync", "done" if _commands_synced else "pending"),
        ("mechanics", loop_state(mechanics_loop)),
        ("deposits", loop_state(deposit_log_loop)),
        ("orders", loop_state(order_event_log_loop)),
        ("trades", loop_state(trade_event_log_loop)),
        ("bounties", loop_state(bounty_event_log_loop)),
        ("external give", loop_state(external_give_loop)),
        ("holder roles", loop_state(holder_role_loop)),
        ("arena", loop_state(arena_cycle)),
    ]
    daemon_path = os.path.abspath(DB_PATH)
    daemon_exists = os.path.exists(daemon_path)
    daemon_size = os.path.getsize(daemon_path) if daemon_exists else 0

    embed = discord.Embed(title="Economy++ Health", color=BLUE)
    embed.add_field(name="Backend", value=backend_line, inline=False)
    embed.add_field(
        name="Bot",
        value=(
            f"Daemon DB: `{'present' if daemon_exists else 'missing'}`\n"
            f"Daemon DB path: `{daemon_path}`\n"
            f"Daemon DB bytes: `{daemon_size}`"
        ),
        inline=False,
    )
    embed.add_field(name="Loops", value="\n".join(f"`{name}`: {state}" for name, state in task_rows), inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)


@economy.command(name="backup", description="[Admin] Create backend and DAEMON database backups")
@app_commands.describe(reason="Optional audit reason for the backup")
async def economy_backup_cmd(interaction: discord.Interaction, reason: Optional[str] = None):
    if not is_economy_admin(interaction.user.id):
        await interaction.response.send_message(embed=err_embed("You cannot use this command."), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    reason_text = normalize_audit_reason(reason)
    session = await get_http_session()
    backend_line = "Backend backup: not attempted"
    try:
        async with session.post(
            f"{BASE_URL}/admin/backup",
            json={"requested_by": str(interaction.user.id), "reason": reason_text},
        ) as r:
            if r.status == 200:
                backend = await r.json()
                backend_line = f"Backend backup: `{backend.get('path')}` ({int(backend.get('bytes') or 0):,} bytes)"
            else:
                detail = (await r.json()).get("detail", f"HTTP {r.status}")
                backend_line = f"Backend backup failed: {detail}"
    except Exception as exc:
        backend_line = f"Backend backup failed: {type(exc).__name__}"

    daemon_path = os.path.abspath(DB_PATH)
    daemon_backup = await asyncio.to_thread(create_daemon_backup)
    daemon_line = format_backup_result("DAEMON", daemon_backup, daemon_path)

    embed = discord.Embed(
        title="Backup Complete",
        description=f"{backend_line}\n{daemon_line}\nReason: {reason_text}",
        color=BLUE,
    )
    await interaction.followup.send(embed=embed, ephemeral=True)
    await log_transaction(
        interaction.guild,
        "Admin Backup",
        f"Admin: <@{interaction.user.id}>\nReason: {reason_text}\n{backend_line}\n{daemon_line}",
        color=BLUE,
    )


@economy.command(name="give", description="Send 🐉 from your Economy++ vault to another user")
@app_commands.describe(user="Recipient", amount="Amount of 🐉 to send")
async def economy_give_cmd(interaction: discord.Interaction, user: discord.Member, amount: int):
    await interaction.response.defer()
    if amount <= 0:
        await interaction.followup.send(embed=err_embed("Amount must be positive."))
        return
    if user.id == interaction.user.id:
        await interaction.followup.send(embed=err_embed("Cannot give to yourself."))
        return
    if user.bot:
        await interaction.followup.send(embed=err_embed("Cannot give to a bot."))
        return

    sender_uuid = await get_mc_uuid(str(interaction.user.id))
    if not sender_uuid:
        await interaction.followup.send(embed=err_embed("Account not linked. Use `/economy link` first."))
        return
    recipient_uuid = await get_mc_uuid(str(user.id))
    if not recipient_uuid:
        await interaction.followup.send(embed=err_embed("Recipient has not linked their Minecraft account."))
        return

    session = await get_http_session()
    async with session.post(
        f"{BASE_URL}/give",
        json={"sender_uuid": sender_uuid, "recipient_uuid": recipient_uuid, "amount": amount},
    ) as r:
        if r.status != 200:
            detail = (await r.json()).get("detail", "Transfer failed.")
            await interaction.followup.send(embed=err_embed(detail))
            return
        await r.json()

    await interaction.followup.send(
        embed=ok_embed(f"<@{interaction.user.id}> sent **🐉 {amount:,}** to <@{user.id}>.")
    )
    await log_transaction(
        interaction.guild,
        "🐉 Economy++ Give",
        f"<@{interaction.user.id}> sent **🐉 {amount:,}** to <@{user.id}>.",
        color=GREEN,
    )


    await update_mansa_musa(interaction.guild)


@bot.tree.command(name="add-dragons", description="[Admin] Add dragons to a user's Economy++ vault")
@app_commands.describe(user="User to credit", amount="Amount of dragons to add", reason="Optional audit reason")
async def add_dragons_cmd(interaction: discord.Interaction, user: discord.Member, amount: int, reason: Optional[str] = None):
    await handle_admin_dragon_adjustment(interaction, user, amount, 1, reason, GREEN)


@bot.tree.command(name="remove-dragons", description="[Admin] Remove dragons from a user's Economy++ vault")
@app_commands.describe(user="User to debit", amount="Amount of dragons to remove", reason="Optional audit reason")
async def remove_dragons_cmd(interaction: discord.Interaction, user: discord.Member, amount: int, reason: Optional[str] = None):
    await handle_admin_dragon_adjustment(interaction, user, amount, -1, reason, RED)


@economy.command(name="inventory", description="Check how much of any item you own (vault + in market)")
@app_commands.describe(item="Item key, e.g. netherite, diamond, iron, overworld_log, wool…")
async def inventory_cmd(interaction: discord.Interaction, item: str):
    await interaction.response.defer(ephemeral=True)
    mc_uuid = await get_mc_uuid(str(interaction.user.id))
    if not mc_uuid:
        await interaction.followup.send(embed=err_embed("Account not linked."), ephemeral=True)
        return
    session = await get_http_session()
    async with session.get(f"{BASE_URL}/inventory/{mc_uuid}/{item}") as r:
            if r.status != 200:
                detail = (await r.json()).get("detail", "Error fetching inventory.")
                await interaction.followup.send(embed=err_embed(detail), ephemeral=True)
                return
            data = await r.json()

    label  = item_display(item)
    embed  = discord.Embed(
        title=f"🎒  Inventory  —  {label}",
        description=(f"```\n  Total           {data['total']:>6}\n"
                     f"  ─────────────────────\n"
                     f"  Available       {data['vault']:>6}\n"
                     f"  In market       {data['in_orders']:>6}\n```"),
        color=BLUE
    )
    embed.set_footer(text=f"You have {data['vault']} {label.lower()} available and {data['in_orders']} listed for sale.")
    await interaction.followup.send(embed=embed, ephemeral=True)


@economy.command(name="cashout", description=f"Unlock cashout roles by spending 🐉 (Tier 1: {CASHOUT_THRESHOLD_1:,} 🐉 | Tier 2: {CASHOUT_THRESHOLD_2:,} 🐉)")
async def cashout_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    user_id = interaction.user.id
    guild   = interaction.guild

    if not guild:
        await interaction.followup.send(embed=err_embed("This command must be used inside the server."), ephemeral=True)
        return

    now          = time.monotonic()
    remaining_cd = CASHOUT_COOLDOWN - (now - _cashout_cooldowns.get(user_id, 0))
    if remaining_cd > 0:
        await interaction.followup.send(
            embed=err_embed(f"Cooldown active. Try again in **{math.ceil(remaining_cd)}s**."), ephemeral=True)
        return

    member = guild.get_member(user_id)
    if not member:
        await interaction.followup.send(embed=err_embed("Could not find you in this server."), ephemeral=True)
        return

    mc_uuid = await get_mc_uuid(str(user_id))
    if not mc_uuid:
        await interaction.followup.send(embed=err_embed("Account not linked. Use `/economy link` first."), ephemeral=True)
        return

    session = await get_http_session()
    async with session.get(f"{BASE_URL}/balance/{mc_uuid}") as r:
            if r.status != 200:
                await interaction.followup.send(embed=err_embed("Error fetching balance."), ephemeral=True)
                return
            bal = await r.json()

    vault_dragons = bal.get("mdragons", 0)
    tiers = [
        (CASHOUT_THRESHOLD_2, TEN_CASHOUT_ROLE_ID, "Tier 2 Cashout"),
        (CASHOUT_THRESHOLD_1, CASHOUT_ROLE_ID,     "Tier 1 Cashout"),
    ]
    target_threshold, target_role_id, target_label = None, None, None
    for threshold, role_id, label in tiers:
        role = guild.get_role(role_id)
        if role and role not in member.roles:
            target_threshold, target_role_id, target_label = threshold, role_id, label
            break

    if target_threshold is None:
        await interaction.followup.send(embed=err_embed("You already have all cashout roles!"), ephemeral=True)
        return

    role = guild.get_role(target_role_id)
    if not role:
        await interaction.followup.send(embed=err_embed("Role not found. Please contact an admin."), ephemeral=True)
        return

    if vault_dragons < target_threshold:
        short = target_threshold - vault_dragons
        await interaction.followup.send(embed=err_embed(
            f"Not enough 🐉 for **{target_label}**.\n"
            f"Have: **🐉 {int(vault_dragons):,}**  ·  Need: **🐉 {target_threshold:,}**\n"
            f"You're **{int(short):,} 🐉** short.\n\n"
            f"Tiers:\n• Tier 1 — 🐉 {CASHOUT_THRESHOLD_1:,}\n• Tier 2 — 🐉 {CASHOUT_THRESHOLD_2:,}"
        ), ephemeral=True)
        return

    _cashout_cooldowns[user_id] = now
    session = await get_http_session()
    async with session.post(f"{BASE_URL}/convert/to_ub",
                          json={"mc_uuid": mc_uuid, "amount": target_threshold}) as r:
            if r.status != 200:
                await interaction.followup.send(
                    embed=err_embed("Failed to deduct 🐉. Please try again or contact support."), ephemeral=True)
                return

    try:
        await member.add_roles(role, reason=f"{target_label} threshold reached via /economy cashout")
    except discord.Forbidden:
        await interaction.followup.send(
            embed=err_embed("I don't have permission to assign that role. Contact an admin."), ephemeral=True)
        return

    embed = discord.Embed(
        title=f"💸  {target_label} Unlocked!",
        description=f"🎉 You've unlocked the **{target_label}** role!\n\n**🐉 {target_threshold:,}** deducted from your vault.",
        color=GREEN
    )
    await interaction.followup.send(embed=embed, ephemeral=True)
    await log_transaction(guild, "💸 Cashout",
        f"<@{user_id}> unlocked **{target_label}** — spent **🐉 {target_threshold:,}**", color=GREEN)


@economy.command(name="mdragons", description=f"Redeem the special role for {MDRAGONS_REWARD:,} 🐉 added to your vault")
async def mdragons_cmd(interaction: discord.Interaction):
    await _mdragons_impl(interaction, required_role_id=MDRAGONS_ROLE_ID, reward=MDRAGONS_REWARD, label="mdragons")


@economy.command(name="10mdragons", description=f"Redeem the 10× special role for {TEN_MDRAGONS_REWARD:,} 🐉 added to your vault")
async def ten_mdragons_cmd(interaction: discord.Interaction):
    await _mdragons_impl(interaction, required_role_id=TEN_MDRAGONS_ROLE_ID, reward=TEN_MDRAGONS_REWARD, label="10mdragons")


async def _mdragons_impl(interaction: discord.Interaction, required_role_id: int, reward: int, label: str):
    await interaction.response.defer(ephemeral=True)
    user_id = interaction.user.id
    guild   = interaction.guild

    if not guild:
        await interaction.followup.send(embed=err_embed("This command must be used inside the server."), ephemeral=True)
        return

    role = guild.get_role(required_role_id)
    if not role:
        await interaction.followup.send(embed=err_embed("Required role not found. Contact an administrator."), ephemeral=True)
        return

    member = guild.get_member(user_id)
    if not member:
        await interaction.followup.send(embed=err_embed("Could not locate your member record."), ephemeral=True)
        return

    if role not in member.roles:
        await interaction.followup.send(embed=err_embed("You do not possess the required role to redeem."), ephemeral=True)
        return

    try:
        await member.remove_roles(role, reason=f"Redeemed for {reward:,} 🐉 via /{label}")
    except discord.Forbidden:
        await interaction.followup.send(embed=err_embed("Insufficient permissions to remove the role."), ephemeral=True)
        return

    mc_uuid = await get_mc_uuid(str(user_id))
    if not mc_uuid:
        await interaction.followup.send(embed=err_embed("Account not linked. Use `/economy link` first."), ephemeral=True)
        return

    session = await get_http_session()
    async with session.post(f"{BASE_URL}/convert/to_vault",
                          json={"mc_uuid": mc_uuid, "amount": reward}) as r:
            if r.status != 200:
                await interaction.followup.send(embed=err_embed("Failed to credit vault. Please contact support."), ephemeral=True)
                return

    await interaction.followup.send(embed=ok_embed(
        f"Role successfully redeemed. **🐉 {reward:,}** have been added to your vault."
    ), ephemeral=True)
    await log_transaction(interaction.guild, "🎁 Role Redeemed",
        f"<@{user_id}> redeemed the **{label}** role → **🐉 {reward:,}** added to vault", color=GREEN)


@market_group.command(name="view", description="View the live order book for any item")
@app_commands.describe(item="Item key, e.g. netherite, diamond, iron, coal, overworld_log, wool…",
                       spread="Optional: group orders within this price range (e.g. 10) for wider view")
async def market_cmd(interaction: discord.Interaction, item: str, spread: Optional[float] = None):
    await interaction.response.defer()
    params = f"?item={item}"
    if spread is not None:
        params += f"&spread={spread}"
    session = await get_http_session()
    async with session.get(f"{BASE_URL}/market{params}") as r:
            if r.status != 200:
                await interaction.followup.send(embed=err_embed("Error fetching market data."))
                return
            data = await r.json()

    asks: list[dict] = data.get("asks", [])
    bids: list[dict] = data.get("bids", [])
    item_label = item_display(item)
    PW, AW, CW = 12, 10, 10
    BAR_W = PW + AW + CW + 10

    def fmt_row(price: float, amt: int, cum: int, prefix: str) -> str:
        return f"{prefix} {price:>{PW},.2f}   {str(amt):>{AW}}   {str(cum):>{CW}}"

    header  = f"  {'PRICE (🐉)':>{PW}}   {'AMOUNT':>{AW}}   {'CUMUL':>{CW}}"
    divider = "  " + "─" * (BAR_W - 2)
    lines: list[str] = []

    if asks:
        for level in reversed(asks):
            lines.append(fmt_row(level["price"], level["amount"], level["cumulative"], "-"))
    else:
        lines.append(f"-  {'— no sell orders —':^{BAR_W - 4}}")

    if asks and bids:
        sp      = asks[0]["price"] - bids[0]["price"]
        sp_pct  = (sp / asks[0]["price"]) * 100
        mid_txt = f"Spread: {sp:,.2f}  ({sp_pct:.4f}%)"
    elif spread:
        mid_txt = f"Spread grouping: {spread}"
    else:
        mid_txt = "No bids"
    lines.append(f"  {mid_txt:^{BAR_W - 2}}")

    if bids:
        for level in bids:
            lines.append(fmt_row(level["price"], level["amount"], level["cumulative"], "+"))
    else:
        lines.append(f"+  {'— no buy orders —':^{BAR_W - 4}}")

    spread_tag = f"  ·  spread: {spread}" if spread else ""
    book_block = (f"```diff\n{header}\n{divider}\n" + "\n".join(lines) + f"\n{divider}\n```")
    embed = discord.Embed(title=f"📊  {item_label} Order Book{spread_tag}", description=book_block, color=BLUE)
    embed.set_footer(text="Red = asks (sell)  ·  Green = bids (buy)  ·  /market buy to bid  ·  prices in 🐉")
    await interaction.followup.send(embed=embed)


@market_group.command(name="sell", description="List vaulted items for sale")
@app_commands.describe(item_type="Item key, e.g. netherite, diamond, iron, overworld_log, wool…",
                       amount="Quantity to list", price_per="Price per unit in 🐉")
async def place_sell_order(interaction: discord.Interaction, item_type: str, amount: int, price_per: float):
    await interaction.response.defer()
    if amount <= 0 or price_per <= 0:
        await interaction.followup.send(embed=err_embed("Amount and price must be positive."))
        return
    mc_uuid = await get_mc_uuid(str(interaction.user.id))
    if not mc_uuid:
        await interaction.followup.send(embed=err_embed("Account not linked."))
        return
    session = await get_http_session()
    async with session.post(f"{BASE_URL}/order/place",
                          json={"mc_uuid": mc_uuid, "item": item_type,
                                "amount": amount, "price_per": price_per}) as r:
            if r.status == 200:
                data = await r.json()
                await interaction.followup.send(embed=ok_embed(
                    f"Sell order placed! Order ID: **#{data['order_id']}**\n"
                    f"{amount}× {item_display(item_type)} @ **🐉 {price_per:.2f}** each"
                ))
                await update_netherite_overlord(interaction.guild)
                await update_mansa_musa(interaction.guild)
            else:
                await interaction.followup.send(embed=err_embed((await r.json()).get("detail", "Failed to place order.")))


@market_group.command(name="buy", description="Place a limit buy order for any item (money is reserved)")
@app_commands.describe(item_type="Item key, e.g. netherite, diamond, iron, overworld_log, wool…",
                       amount="Quantity to buy",
                       price_per="Maximum price per unit in 🐉 you're willing to pay")
async def place_buy_order(interaction: discord.Interaction, item_type: str, amount: int, price_per: float):
    await interaction.response.defer()
    if amount <= 0 or price_per <= 0:
        await interaction.followup.send(embed=err_embed("Amount and price must be positive."))
        return
    mc_uuid = await get_mc_uuid(str(interaction.user.id))
    if not mc_uuid:
        await interaction.followup.send(embed=err_embed("Account not linked."))
        return
    session = await get_http_session()
    async with session.post(f"{BASE_URL}/order/place_buy",
                          json={"mc_uuid": mc_uuid, "item": item_type,
                                "amount": amount, "price_per": price_per}) as r:
            if r.status == 200:
                data = await r.json()
                await interaction.followup.send(embed=ok_embed(
                    f"Buy order placed! Order ID: **#{data['order_id']}**\n"
                    f"{amount}× {item_display(item_type)} @ up to **🐉 {price_per:.2f}** each"
                ))
                await update_netherite_overlord(interaction.guild)
                await update_mansa_musa(interaction.guild)
            else:
                await interaction.followup.send(embed=err_embed((await r.json()).get("detail", "Failed to place order.")))


@market_group.command(name="orders", description="View your active buy & sell orders")
@app_commands.describe(page="Page number (10 orders per page)")
async def list_orders(interaction: discord.Interaction, page: int = 1):
    await interaction.response.defer(ephemeral=True)
    mc_uuid = await get_mc_uuid(str(interaction.user.id))
    if not mc_uuid:
        await interaction.followup.send(embed=err_embed("Account not linked."), ephemeral=True)
        return
    session = await get_http_session()
    async with session.get(f"{BASE_URL}/orders/user/{mc_uuid}") as r:
            if r.status != 200:
                await interaction.followup.send(embed=err_embed("Error fetching orders."), ephemeral=True)
                return
            data = await r.json()

    orders = data.get("orders", [])
    if not orders:
        await interaction.followup.send(
            embed=discord.Embed(description="📭  You have no active orders.", color=BLUE), ephemeral=True)
        return

    per_page    = 10
    total_pages = math.ceil(len(orders) / per_page)
    page        = max(1, min(page, total_pages))
    chunk       = orders[(page - 1) * per_page : page * per_page]

    W   = {"side": 6, "id": 5, "item": 9, "qty": 7, "price": 10, "total": 12}
    sep = "  " + "─" * (sum(W.values()) + len(W) * 3 + 1)
    hdr = (f"  {'SIDE':<{W['side']}}   {'#':>{W['id']}}   "
           f"{'ITEM':<{W['item']}}   {'QTY':>{W['qty']}}   "
           f"{'PRICE/U':>{W['price']}}   {'TOTAL 🐉':>{W['total']}}")
    rows = []
    for o in chunk:
        iname    = item_display(o["item"])[:9]
        side_str = "BUY" if o["side"] == "buy" else "SELL"
        total    = o["remaining"] * o["price_per"]
        rows.append(
            f"  {side_str:<{W['side']}}   {o['id']:>{W['id']}}   {iname:<{W['item']}}   "
            f"{o['remaining']:>{W['qty']}}   {o['price_per']:>{W['price']}.2f}   {total:>{W['total']}.2f}"
        )
    block = "```\n" + hdr + "\n" + sep + "\n" + "\n".join(rows) + "\n" + sep + "\n```"
    embed = discord.Embed(title="📋  Your Active Orders", description=block, color=BLUE)
    embed.set_footer(text=f"Page {page} / {total_pages}  ·  {len(orders)} total  ·  prices in 🐉")
    await interaction.followup.send(embed=embed, ephemeral=True)


@market_group.command(name="cancel", description="Cancel one of your orders")
@app_commands.describe(order_id="Order ID to cancel")
async def cancel_order_cmd(interaction: discord.Interaction, order_id: int):
    await interaction.response.defer()
    mc_uuid = await get_mc_uuid(str(interaction.user.id))
    if not mc_uuid:
        await interaction.followup.send(embed=err_embed("Account not linked."))
        return
    session = await get_http_session()
    async with session.post(f"{BASE_URL}/order/cancel",
                          json={"order_id": order_id, "mc_uuid": mc_uuid}) as r:
            if r.status == 200:
                await interaction.followup.send(embed=ok_embed("Order cancelled. Items/money returned to your vault."))
                await update_netherite_overlord(interaction.guild)
            else:
                await interaction.followup.send(embed=err_embed((await r.json()).get("detail", "Failed to cancel.")))


@market_group.command(name="cancel_all", description="Cancel all (or all of one item's) orders")
@app_commands.describe(item="Optional: item key to cancel (e.g. netherite, iron, wool) — leave blank for all")
async def cancel_all_orders_cmd(interaction: discord.Interaction, item: Optional[str] = None):
    await interaction.response.defer()
    mc_uuid = await get_mc_uuid(str(interaction.user.id))
    if not mc_uuid:
        await interaction.followup.send(embed=err_embed("Account not linked."))
        return
    payload: dict = {"mc_uuid": mc_uuid}
    if item:
        payload["item"] = item
    session = await get_http_session()
    async with session.post(f"{BASE_URL}/order/cancel_all", json=payload) as r:
            if r.status == 200:
                data     = await r.json()
                count    = data.get("cancelled", 0)
                item_str = f" {item}" if item else ""
                await interaction.followup.send(embed=ok_embed(
                    f"Cancelled **{count}**{item_str} order(s). All items/money returned to your vault."))
                await update_netherite_overlord(interaction.guild)
            else:
                await interaction.followup.send(embed=err_embed((await r.json()).get("detail", "Failed to cancel.")))


@market_group.command(name="status", description="View the current state of the special market mechanics")
async def mechanics_status_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    session = await get_http_session()
    async with session.get(f"{BASE_URL}/mechanics/status") as r:
            if r.status != 200:
                await interaction.followup.send(embed=err_embed("Failed to fetch mechanics status."))
                return
            data = await r.json()

    embed = discord.Embed(title="⚙️  Market Mechanics Status", color=BLUE)
    embed.add_field(
        name="🗓️ Weekly Reset",
        value=f"Last: `{data['last_reset'][:16]}`\nNext: `{data['next_reset'][:16]}`",
        inline=False
    )
    if data.get("injection_paused"):
        embed.add_field(name="⏸️ Injection Paused", value="Liquidity injection is currently **paused** by an admin.", inline=False)

    for m in data.get("mechanisms", []):
        role_label  = "👑 Mansa Musa" if m["role"] == "MANSA_MUSA" else "🐉 Netherite Overlord"
        status      = m["status"]
        item        = m.get("current_item") or "—"
        pending     = m.get("pending_item")
        filled      = int(m.get("weekly_value_filled", 0))
        target      = int(m.get("weekly_target", 0))
        pct         = m.get("threshold_pct", 0)
        price_line  = f"\nCurrent price: **🐉 {m['current_price']:,.2f}**" if "current_price" in m else ""
        prog_line   = f"\nProgress: **🐉 {filled:,} / {target:,}** ({pct}%)" if status == "active" else ""
        pending_line = f"\n⏳ Pending: **{pending}** (next reset)" if pending else ""
        status_emoji = {"active": "🟢", "frozen": "🔴", "inactive": "⚫", "announced": "🟡"}.get(status, "❔")
        embed.add_field(
            name=f"{role_label}  {status_emoji} {status.upper()}",
            value=f"Market: **{item}**{price_line}{prog_line}{pending_line}",
            inline=False
        )

    embed.set_footer(text=f"Injection cap: 🐉 {int(data.get('weekly_target', 0)):,}  ·  Resets every Saturday 21:00 CET")
    await interaction.followup.send(embed=embed)


@market_group.command(name="choose", description="(Mansa Musa / Netherite Overlord) Lock in your market for the next weekly reset")
@app_commands.describe(item="diamond  or  netherite")
async def choose_market(interaction: discord.Interaction, item: str):
    await interaction.response.defer(ephemeral=True)
    guild  = interaction.guild
    member = guild.get_member(interaction.user.id) if guild else None
    if not member:
        await interaction.followup.send(embed=err_embed("Must be used inside the server."), ephemeral=True)
        return

    mech_role = _role_for_market_chooser(member)
    if not mech_role:
        await interaction.followup.send(
            embed=err_embed("You do not hold the Mansa Musa or Netherite Overlord role."), ephemeral=True)
        return

    if item.lower() not in ("diamond", "netherite"):
        await interaction.followup.send(embed=err_embed("Item must be `diamond` or `netherite`."), ephemeral=True)
        return

    session = await get_http_session()
    async with session.post(f"{BASE_URL}/mechanics/announce",
                          json={"role": mech_role, "item": item}) as r:
            data = await r.json()
            if r.status != 200:
                await interaction.followup.send(
                    embed=err_embed(data.get("detail", "Failed to lock in market.")), ephemeral=True)
                return

    role_name   = "Mansa Musa" if mech_role == "MANSA_MUSA" else "Netherite Overlord"
    item_label  = "Diamonds" if item.lower() == "diamond" else "Netherite Ingots"
    activates_str = data['activates_at'][:10]

    embed = discord.Embed(
        title="🔒  Liquidity Injection Locked In",
        description=(
            f"**{role_name}** has chosen to inject liquidity from the server into the **{item_label}** market.\n\n"
            f"Activates: **{activates_str} at 21:00 German time**\n\n"
            f"⚠️ This choice is **permanent** — it cannot be changed until after that reset fires."
        ),
        color=GREEN
    )
    await interaction.followup.send(embed=embed, ephemeral=True)

    public_embed = discord.Embed(
        title="📢  Liquidity Injection Announced",
        description=(
            f"**{role_name}** will be injecting liquidity from the server into the **{item_label}** market.\n\n"
            f"🗓️ Activates: **{activates_str} at 21:00 German time**\n"
            f"🔒 This choice is locked in and cannot be changed before the reset."
        ),
        color=GREEN
    )
    if guild:
        for ch_id in (ANNOUNCEMENT_CHANNEL_ID, TRANSACTION_LOG_CHANNEL_ID):
            ch = guild.get_channel(ch_id)
            if ch:
                try:
                    await ch.send(embed=public_embed)
                except discord.Forbidden:
                    pass


@market_group.command(name="set_injection_cap", description="(Chairman) Raise or lower the weekly liquidity injection cap by 50,000 🐉")
@app_commands.describe(direction="raise or lower")
async def set_injection_cap(interaction: discord.Interaction, direction: str):
    await interaction.response.defer(ephemeral=True)
    guild  = interaction.guild
    member = guild.get_member(interaction.user.id) if guild else None

    if not member or not any(r.id == CHAIRMAN_ROLE_ID for r in member.roles):
        await interaction.followup.send(embed=err_embed("You do not hold the Chairman role."), ephemeral=True)
        return

    if direction.lower() not in ("raise", "lower"):
        await interaction.followup.send(embed=err_embed("Direction must be `raise` or `lower`."), ephemeral=True)
        return

    delta = 50_000 if direction.lower() == "raise" else -50_000
    session = await get_http_session()
    async with session.post(f"{BASE_URL}/chairman/set_injection_cap", json={"delta": delta}) as r:
            data = await r.json()
            if r.status != 200:
                await interaction.followup.send(embed=err_embed(data.get("detail", "Failed.")), ephemeral=True)
                return

    arrow = "📈" if delta > 0 else "📉"
    embed = discord.Embed(
        title=f"{arrow}  Injection Cap Updated",
        description=(
            f"Current live cap: **🐉 {int(data['current_active_target']):,}**\n"
            f"Previous pending cap: **🐉 {int(data['previous_pending_target']):,}**\n"
            f"New pending cap: **🐉 {int(data['new_pending_target']):,}**\n\n"
            f"Allowed range: 🐉 {int(data['min']):,} – 🐉 {int(data['max']):,}\n"
            f"Activates at next reset: **{data['activates_at'][:16]}**\n"
            f"You may adjust again after the next weekly reset (Saturday 21:00 German time)."
        ),
        color=GREEN
    )
    await interaction.followup.send(embed=embed, ephemeral=True)
    await log_transaction(interaction.guild, f"{arrow} Injection Cap Adjusted",
        f"Chairman <@{interaction.user.id}> changed the pending weekly injection cap: "
        f"**🐉 {int(data['previous_pending_target']):,}** → **🐉 {int(data['new_pending_target']):,}**"
        f" (live cap remains **🐉 {int(data['current_active_target']):,}** until reset)",
        color=GREEN if delta > 0 else RED)



@market_group.command(name="injectioncaprange", description="(Chairman) Set the allowed weekly injection-cap range")
@app_commands.describe(minimum="Minimum weekly cap", maximum="Maximum weekly cap")
async def injection_cap_range_cmd(interaction: discord.Interaction, minimum: int, maximum: int):
    await interaction.response.defer(ephemeral=True)
    guild  = interaction.guild
    member = guild.get_member(interaction.user.id) if guild else None

    if not member or not any(r.id == CHAIRMAN_ROLE_ID for r in member.roles):
        await interaction.followup.send(embed=err_embed("You do not hold the Chairman role."), ephemeral=True)
        return

    session = await get_http_session()
    async with session.post(f"{BASE_URL}/chairman/injection_cap_range", json={"minimum": minimum, "maximum": maximum}) as r:
        data = await r.json()
        if r.status != 200:
            await interaction.followup.send(embed=err_embed(data.get("detail", "Failed to update injection cap range.")), ephemeral=True)
            return

    embed = discord.Embed(
        title="📏  Injection Cap Range Updated",
        description=(
            f"New allowed range: **🐉 {int(data['min']):,} – {int(data['max']):,}**\n"
            f"Current live cap: **🐉 {int(data['weekly_target']):,}**\n"
            f"Pending next cap: **🐉 {int(data.get('weekly_target_pending', data['weekly_target'])):,}**"
        ),
        color=GREEN,
    )
    await interaction.followup.send(embed=embed, ephemeral=True)
    await log_transaction(
        interaction.guild,
        "📏 Injection Cap Range Updated",
        f"Chairman <@{interaction.user.id}> set the weekly injection cap range to **🐉 {int(data['min']):,} – {int(data['max']):,}**. Current live cap: **🐉 {int(data['weekly_target']):,}**. Pending next cap: **🐉 {int(data.get('weekly_target_pending', data['weekly_target'])):,}**",
        color=GREEN,
    )


@market_group.command(name="pause_injection", description="(Admin only) Pause or resume liquidity injection")
@app_commands.describe(action="pause  or  resume")
async def pause_injection_cmd(interaction: discord.Interaction, action: str):
    await interaction.response.defer(ephemeral=True)
    if interaction.user.id != PAUSE_INJECTION_USER_ID:
        await interaction.followup.send(embed=err_embed("You are not authorised to use this command."), ephemeral=True)
        return

    if action.lower() not in ("pause", "resume"):
        await interaction.followup.send(embed=err_embed("Action must be `pause` or `resume`."), ephemeral=True)
        return

    should_pause = action.lower() == "pause"
    session = await get_http_session()
    async with session.post(f"{BASE_URL}/mechanics/set_pause",
                          json={"paused": should_pause, "paused_by": str(interaction.user.id)}) as r:
            data = await r.json()
            if r.status != 200:
                await interaction.followup.send(embed=err_embed(data.get("detail", "Failed.")), ephemeral=True)
                return

    status_word = "⏸️ PAUSED" if should_pause else "▶️ RESUMED"
    color       = RED if should_pause else GREEN
    embed = discord.Embed(
        title=f"Liquidity Injection {status_word}",
        description=(
            "All liquidity injection price movements and order matching are **paused**.\n"
            "Pending injection transactions are cancelled until resumed."
            if should_pause else
            "Liquidity injection has been **resumed**. Price curve will continue from where it left off."
        ),
        color=color
    )
    await interaction.followup.send(embed=embed, ephemeral=True)
    guild = interaction.guild
    if guild:
        for ch_id in (ANNOUNCEMENT_CHANNEL_ID, TRANSACTION_LOG_CHANNEL_ID):
            ch = guild.get_channel(ch_id)
            if ch:
                try:
                    await ch.send(embed=discord.Embed(
                        title=f"⚠️ Liquidity Injection {status_word}",
                        description=f"Liquidity injection has been **{'paused' if should_pause else 'resumed'}** by an administrator.",
                        color=color
                    ))
                except discord.Forbidden:
                    pass


# ─────────────────────────────────────────────────────────────────
# SLASH COMMANDS — MARKET CATEGORY
# ─────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────
# SLASH COMMANDS — PURCHASE LISTS CATEGORY
# ─────────────────────────────────────────────────────────────────


@lists_group.command(name="create", description="Create a purchase list (up to 5). Others can instantly sell you all items at your price.")
@app_commands.describe(name="A short label for this list (e.g. 'Diamond bulk')",
                       price="Total 🐉 you will pay when someone fills this list",
                       items="Items and quantities, e.g: diamond:100,netherite:5")
async def create_purchase_list(interaction: discord.Interaction, name: str, price: float, items: str):
    await interaction.response.defer(ephemeral=True)
    mc_uuid = await get_mc_uuid(str(interaction.user.id))
    if not mc_uuid:
        await interaction.followup.send(embed=err_embed("Account not linked. Use `/economy link` first."), ephemeral=True)
        return

    session = await get_http_session()
    async with session.post(f"{BASE_URL}/purchase_list/create",
                          json={"mc_uuid": mc_uuid, "name": name, "price": price, "items": items}) as r:
            data = await r.json()
            if r.status != 200:
                await interaction.followup.send(embed=err_embed(data.get("detail", "Failed to create list.")), ephemeral=True)
                return

    item_lines = "\n".join(
        f"  • {qty}× {'Netherite Ingots' if k == 'NETHERITE_INGOT' else 'Diamonds'}"
        for k, qty in data["items"].items()
    )
    embed = discord.Embed(
        title="📋  Purchase List Created",
        description=(
            f"**{data['name']}**  ·  ID `#{data['list_id']}`\n\n"
            f"{item_lines}\n\n"
            f"Total payout: **🐉 {int(data['price']):,}**\n\n"
            f"Anyone with all these items can use `/lists fill {data['list_id']}` to instantly sell them to you."
        ),
        color=GREEN
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


@lists_group.command(name="mine", description="View your active purchase lists")
async def my_purchase_lists(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    mc_uuid = await get_mc_uuid(str(interaction.user.id))
    if not mc_uuid:
        await interaction.followup.send(embed=err_embed("Account not linked."), ephemeral=True)
        return

    session = await get_http_session()
    async with session.get(f"{BASE_URL}/purchase_list/user/{mc_uuid}") as r:
            data = await r.json()

    lists = data.get("lists", [])
    if not lists:
        await interaction.followup.send(
            embed=discord.Embed(description="📭  You have no active purchase lists.", color=BLUE), ephemeral=True)
        return

    embed = discord.Embed(title="📋  Your Purchase Lists", color=BLUE)
    for pl in lists:
        item_str = ", ".join(
            f"{qty}× {'Netherite' if k == 'NETHERITE_INGOT' else 'Diamond'}"
            for k, qty in pl["items"].items()
        )
        embed.add_field(
            name=f"#{pl['id']}  {pl['name']}",
            value=f"{item_str}\n💰 **🐉 {int(pl['price']):,}** total payout",
            inline=False
        )
    embed.set_footer(text=f"{len(lists)}/5 slots used  ·  /lists delete <id> to remove one")
    await interaction.followup.send(embed=embed, ephemeral=True)


@lists_group.command(name="all", description="Browse all open purchase lists from all players")
async def all_purchase_lists(interaction: discord.Interaction):
    await interaction.response.defer()
    session = await get_http_session()
    async with session.get(f"{BASE_URL}/purchase_list/all") as r:
            data = await r.json()

    lists = data.get("lists", [])
    if not lists:
        await interaction.followup.send(embed=discord.Embed(description="📭  No purchase lists open right now.", color=BLUE))
        return

    embed = discord.Embed(
        title="🛒  Open Purchase Lists",
        description="Use `/lists fill <id>` to instantly sell all items and collect the 🐉 payout.",
        color=BLUE
    )
    for pl in lists:
        item_str = ", ".join(
            f"{qty}× {'Netherite' if k == 'NETHERITE_INGOT' else 'Diamond'}"
            for k, qty in pl["items"].items()
        )
        embed.add_field(
            name=f"#{pl['id']}  {pl['name']}",
            value=f"{item_str}\n💰 **🐉 {int(pl['price']):,}**  ·  buyer: <@!{pl['owner_uuid']}>",
            inline=False
        )
    embed.set_footer(text=f"{len(lists)} list(s) open")
    await interaction.followup.send(embed=embed)


@lists_group.command(name="delete", description="Delete one of your purchase lists")
@app_commands.describe(list_id="The ID of the list to delete")
async def delete_purchase_list(interaction: discord.Interaction, list_id: int):
    await interaction.response.defer(ephemeral=True)
    mc_uuid = await get_mc_uuid(str(interaction.user.id))
    if not mc_uuid:
        await interaction.followup.send(embed=err_embed("Account not linked."), ephemeral=True)
        return

    session = await get_http_session()
    async with session.request("DELETE", f"{BASE_URL}/purchase_list/delete",
                             json={"mc_uuid": mc_uuid, "list_id": list_id}) as r:
            if r.status == 200:
                await interaction.followup.send(embed=ok_embed(f"Purchase list **#{list_id}** deleted."), ephemeral=True)
            else:
                detail = (await r.json()).get("detail", "Failed to delete.")
                await interaction.followup.send(embed=err_embed(detail), ephemeral=True)


@lists_group.command(name="fill", description="Instantly sell all items on a purchase list and receive the 🐉 payout")
@app_commands.describe(list_id="ID of the purchase list to fill")
async def fill_purchase_list(interaction: discord.Interaction, list_id: int):
    await interaction.response.defer()
    mc_uuid = await get_mc_uuid(str(interaction.user.id))
    if not mc_uuid:
        await interaction.followup.send(embed=err_embed("Account not linked."))
        return

    session = await get_http_session()
    async with session.post(f"{BASE_URL}/purchase_list/fill",
                          json={"mc_uuid": mc_uuid, "list_id": list_id}) as r:
            data = await r.json()
            if r.status != 200:
                await interaction.followup.send(embed=err_embed(data.get("detail", "Failed to fill list.")))
                return

    item_lines = "\n".join(
        f"  • {qty}× {'Netherite Ingots' if k == 'NETHERITE_INGOT' else 'Diamonds'}"
        for k, qty in data["items"].items()
    )
    embed = discord.Embed(
        title="✅  Purchase List Filled!",
        description=(
            f"**{data['list_name']}** has been filled.\n\n"
            f"{item_lines}\n\n"
            f"You received: **🐉 {int(data['price_paid']):,}**"
        ),
        color=GREEN
    )
    await interaction.followup.send(embed=embed)
    await update_netherite_overlord(interaction.guild)
    await update_mansa_musa(interaction.guild)
    await log_transaction(interaction.guild, "🛒 Purchase List Filled",
        f"<@{interaction.user.id}> filled **{data['list_name']}** and received **🐉 {int(data['price_paid']):,}**",
        color=GREEN)


# ─────────────────────────────────────────────────────────────────
# SLASH COMMANDS — DAEMON CATEGORY
# ─────────────────────────────────────────────────────────────────
daemon_group = app_commands.Group(name="daemon", description="Daemon cryptocurrency commands")


@bot.tree.command(name="iamsatoshinakamoto", description="Toggle the Satoshi Nakamoto role if you are the top DAEMON holder")
@app_commands.describe(answer="yes to claim the role, no to remove your claim")
@app_commands.choices(answer=[
    app_commands.Choice(name="yes", value="yes"),
    app_commands.Choice(name="no", value="no"),
])
async def i_am_satoshi_nakamoto_cmd(interaction: discord.Interaction, answer: str):
    await interaction.response.defer(ephemeral=True)
    role = interaction.guild.get_role(SATOSHI_NAKAMOTO_ROLE_ID) if interaction.guild else None
    if not role:
        await interaction.followup.send("❌ Satoshi role not found on this server.", ephemeral=True)
        return

    if answer == "no":
        claimed_raw = get_daemon_market_meta("satoshi_claim_user_id")
        if claimed_raw == str(interaction.user.id):
            set_daemon_market_meta("satoshi_claim_user_id", "")
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member and role in member.roles:
            await member.remove_roles(role, reason="Satoshi Nakamoto claim toggled off")
        await update_satoshi_role(interaction.guild)
        await interaction.followup.send("✅ Satoshi Nakamoto claim removed.", ephemeral=True)
        return

    top_id = get_top_daemon_holder()
    if top_id != interaction.user.id:
        await update_satoshi_role(interaction.guild)
        await interaction.followup.send("❌ Only the current top DAEMON holder can claim this role.", ephemeral=True)
        return

    set_daemon_market_meta("satoshi_claim_user_id", str(interaction.user.id))
    await update_satoshi_role(interaction.guild)
    await interaction.followup.send("✅ Satoshi Nakamoto claim active while you remain the top DAEMON holder.", ephemeral=True)


@daemon_group.command(name="balance", description="Check your Daemon balance (private)")
async def daemon_balance_cmd(interaction: discord.Interaction):
    bal   = get_balance(interaction.user.id)
    embed = build_balance_embed(interaction.user, bal)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@daemon_group.command(name="send", description="Transfer Daemon to another user")
@app_commands.describe(
    user="Recipient",
    amount="Amount to send",
    hidden="Send anonymously (true = anonymous, false = public)",
    message="Optional message to include with the transfer",
)
async def daemon_send_cmd(
    interaction: discord.Interaction,
    user: discord.Member,
    amount: int,
    hidden: bool = False,
    message: Optional[str] = None,
):
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be positive.", ephemeral=True)
        return
    if user.id == interaction.user.id:
        await interaction.response.send_message("❌ Cannot send to yourself.", ephemeral=True)
        return

    bal = get_balance(interaction.user.id)
    if bal < amount:
        await interaction.response.send_message(
            f"❌ Insufficient balance: `{bal:,}` {DAEMON_EMOJI}", ephemeral=True)
        return

    txid   = gen_txid()

    add_balance(interaction.user.id, -amount)
    add_balance(user.id, amount)

    sender_bal = get_balance(interaction.user.id)
    recip_bal  = get_balance(user.id)

    sender_embed = build_tx_sender_embed(user, amount, txid, hidden, sender_bal, message)
    await interaction.response.send_message(embed=sender_embed, ephemeral=True)

    try:
        recip_embed = build_tx_recipient_embed(interaction.user, amount, txid, hidden, recip_bal, message)
        await user.send(embed=recip_embed)
    except Exception:
        pass

    log_ch = bot.get_channel(TRANSACTION_CHANNEL)
    if log_ch and not hidden:
        log_embed = build_tx_log_embed(interaction.user, user, amount, txid, hidden, message)
        await log_ch.send(embed=log_embed)
    await update_satoshi_role(interaction.guild)


@daemon_group.command(name="stats", description="View the Daemon economy stats (public)")
async def daemon_stats_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(embed=build_daemon_info_embed())


@daemon_group.command(name="info", description="Learn how Daemon works — full guide")
async def daemon_info_cmd(interaction: discord.Interaction):
    G = "\u001b[1;32m"; g = "\u001b[0;32m"; C = "\u001b[1;36m"
    W = "\u001b[0;37m"; Y = "\u001b[0;33m"; D = "\u001b[0;90m"
    P = "\u001b[0;35m"; R = "\u001b[0m"

    lines = [
        "```ansi",
        f"{G}╔══════════════════════════════════════════╗",
        f"║     D A E M O N   —   H O W   T O        ║",
        f"╚══════════════════════════════════════════╝{R}",
        "",
        f"{C}  ┌─ WHAT IS DAEMON? ──────────────────────┐{R}",
        f"{W}  │  DAEMON is a capped in-server currency  │{R}",
        f"{W}  │  (max {MAX_SUPPLY:,}) inspired by Bitcoin. │{R}",
        f"{W}  │  You earn it, send it, and pledge it.   │{R}",
        f"{C}  └────────────────────────────────────────┘{R}",
        "",
        f"{C}  ┌─ THE ARENA ─────────────────────────────┐{R}",
        f"{W}  │  Each day a convergence opens. Commit DAEMON    │{R}",
        f"{W}  │  to Rock, Paper, or Scissors.            │{R}",
        f"{W}  │  Convergences resolve once daily at an unpredictable time  │{R}",
        f"{W}  │  (the exact moment is unknown until it arrives).      │{R}",
        f"{W}  │  • Underdog pool wins half the emission  │{R}",
        f"{W}  │  • Biggest pool fights 2nd place (RPS)   │{R}",
        f"{W}  │  • Winner of that fight wins other half  │{R}",
        f"{W}  │  • Winners also earn a share of mined    │{R}",
        f"{W}  │    DAEMON from that day's emission       │{R}",
        f"{W}  │  • Min pledge: 1 DAEMON                  │{R}",
        f"{W}  │  • Pool sizes hidden until reveal        │{R}",
        f"{C}  └────────────────────────────────────────┘{R}",
        "",
        f"{C}  ┌─ PAYOUT DISTRIBUTION ───────────────────┐{R}",
        f"{W}  │  Within a winning pool, rewards are NOT  │{R}",
        f"{W}  │  split evenly. Your share is proportional│{R}",
        f"{W}  │  to your stake^1.5 (power 1.5).          │{R}",
        f"{C}  └────────────────────────────────────────┘{R}",
        "",
        f"{C}  ┌─ EMISSION & HALVING ────────────────────┐{R}",
        f"{W}  │  Starts at {INITIAL_DAILY:,}/day. After {HALVING_THRESHOLD:,}      │{R}",
        f"{W}  │  DAEMON minted the rate drops by 10%.   │{R}",
        f"{W}  │  Then drops 10% every year until cap.   │{R}",
        f"{C}  └────────────────────────────────────────┘{R}",
        "",
        f"{C}  ┌─ COMMANDS ──────────────────────────────┐{R}",
        f"{g}  │  /daemon balance         — check wallet  │{R}",
        f"{g}  │  /daemon send            — transfer       │{R}",
        f"{g}  │  /daemon stats           — economy stats  │{R}",
        f"{g}  │  /daemon info            — this guide     │{R}",
        f"{g}  │  /arena rock/paper/scissors — commit      │{R}",
        f"{g}  │  /arena gamefix          — restore board  │{R}",
        f"{g}  │  /governance proposal    — submit vote    │{R}",
        f"{g}  │  /governance vote        — cast a vote    │{R}",
        f"{g}  │  /governance bid         — bid for slot   │{R}",
        f"{C}  └────────────────────────────────────────┘{R}",
        "",
        f"{D}  {ts_now()}{R}",
        "```",
    ]
    embed = discord.Embed(description="\n".join(lines), color=0x00FF41)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ─────────────────────────────────────────────────────────────────
# SLASH COMMANDS — ARENA CATEGORY
# ─────────────────────────────────────────────────────────────────
arena_group = app_commands.Group(name="arena", description="Daemon Arena — Rock Paper Scissors")


@arena_group.command(name="rock", description="Commit to Rock for the current arena")
@app_commands.describe(amount="Amount to commit (min 1)")
async def rock_cmd(interaction: discord.Interaction, amount: int):
    await _invest_slash(interaction, "rock", amount)


@arena_group.command(name="paper", description="Commit to Paper for the current arena")
@app_commands.describe(amount="Amount to commit (min 1)")
async def paper_cmd(interaction: discord.Interaction, amount: int):
    await _invest_slash(interaction, "paper", amount)


@arena_group.command(name="scissors", description="Commit to Scissors for the current arena")
@app_commands.describe(amount="Amount to commit (min 1)")
async def scissors_cmd(interaction: discord.Interaction, amount: int):
    await _invest_slash(interaction, "scissors", amount)


@arena_group.command(name="spread", description="Commit equally to Rock, Paper & Scissors in the current convergence")
@app_commands.describe(amount="Total DAEMON to commit — must be divisible by 3")
async def arena_spread_cmd(interaction: discord.Interaction, amount: int):
    await daemon_split_cmd(interaction, amount)


@arena_group.command(name="autospread", description="Auto-commit equally to all three sides each convergence")
@app_commands.describe(amount="Total DAEMON per game (divisible by 3). Set to 0 to cancel.")
async def arena_autospread_cmd(interaction: discord.Interaction, amount: int):
    if amount != 0 and get_autosplit_expiration() == 0:
        await interaction.response.send_message(
            "❌ Arena autospread is currently disabled by admin.", ephemeral=True
        )
        return
    await daemon_dailysplit_cmd(interaction, amount)


@arena_group.command(name="autospreadadjust", description="[ADMIN] Set the maximum number of days arena autospread may run")
@app_commands.describe(days="Number of days autospread may cover (0-14). Set to 0 to disable autospread for everyone.")
async def arena_autospread_adjust_cmd(interaction: discord.Interaction, days: int):
    if interaction.user.id != GAMEFIX_USER_ID:
        await interaction.response.send_message("❌ Unauthorized.", ephemeral=True)
        return
    if days < 0 or days > 14:
        await interaction.response.send_message("❌ Days must be between **0** and **14**.", ephemeral=True)
        return
    set_autosplit_expiration(days)
    msg = (
        f"✅ Arena autospread maximum set to **{days}** day(s)."
        if days > 0 else
        "✅ Arena autospread is now **disabled globally**. Existing autospreads will no longer execute."
    )
    await interaction.response.send_message(msg, ephemeral=True)


async def _invest_slash(interaction: discord.Interaction, choice: str, amount: int):
    if amount < 1:
        await interaction.response.send_message("❌ Minimum commit is **1** DAEMON.", ephemeral=True)
        return
    arena = get_arena()
    if not arena["game_open"]:
        await interaction.response.send_message("❌ No convergence is currently active.", ephemeral=True)
        return
    uid = interaction.user.id
    bal = get_balance(uid)
    if bal < amount:
        await interaction.response.send_message(
            f"❌ Insufficient balance: `{bal:,}` {DAEMON_EMOJI}", ephemeral=True)
        return

    add_balance(uid, -amount)
    add_investment(uid, choice, amount, arena["game_date"], arena["game_id"])

    D = "\u001b[0;90m"; G = "\u001b[1;32m"; W = "\u001b[0;37m"; Y = "\u001b[0;33m"; R = "\u001b[0m"
    choice_emoji = {"rock": "🪨 ROCK", "paper": "📄 PAPER", "scissors": "✂️  SCISSORS"}
    lines = [
        "```ansi",
        f"{G}  COMMIT CONFIRMED{R}",
        f"{D}  Arena Date   ▸ {arena['game_date'][:10]}{R}",
        f"{W}  Amount   ▸ {amount:,} DAEMON{R}",
        f"{Y}  Pledged  ▸ {choice_emoji[choice]}{R}",
        f"{W}  Bal      ▸ {get_balance(uid):,} DAEMON{R}",
        "```",
    ]
    await interaction.response.send_message(
        embed=discord.Embed(description="\n".join(lines), color=0x00FF41), ephemeral=True)

    ch = get_channel_safe(bot, COMMIT_LOG_CHANNEL_ID)
    if ch:
        await ch.send(f"```diff\n+ {interaction.user.display_name} ({interaction.user.id}) committed {amount:,} DAEMON to the arena\n```")

    await update_dashboard(get_arena())


@arena_group.command(name="gamefix", description="[RESTRICTED] Resend the active arena dashboard")
async def daemon_gamefix_cmd(interaction: discord.Interaction):
    if interaction.user.id != GAMEFIX_USER_ID:
        await interaction.response.send_message("❌ Unauthorized.", ephemeral=True)
        return

    arena = get_arena()
    if not arena["game_open"]:
        await interaction.response.send_message(
            "❌ No convergence is currently active. A new one will open shortly.", ephemeral=True)
        return

    ch = bot.get_channel(ARENA_CHANNEL)
    if not ch:
        await interaction.response.send_message("❌ Arena channel not found.", ephemeral=True)
        return

    view   = ArenaView()
    embed  = build_dashboard_embed(
        arena["game_id"], arena["game_date"], arena["pot"],
        arena.get("next_end_ts", 0)
    )
    header = f"```ansi\n\u001b[1;32m▶ DAEMON ARENA {format_arena_display_id(arena['game_date'])} (RESTORED)\u001b[0m\n```"
    msg    = await ch.send(content=header, embed=embed, view=view)
    set_arena(dashboard_msg=msg.id)

    G = "\u001b[1;32m"; D = "\u001b[0;90m"; R = "\u001b[0m"
    lines = [
        "```ansi",
        f"{G}  ARENA BOARD RESTORED{R}",
        f"{D}  Convergence  ▸ {format_arena_display_id(arena['game_date'])}{R}",
        f"{D}  Date  ▸ {arena['game_date']}{R}",
        f"{D}  {ts_now()}{R}",
        "```",
    ]
    await interaction.response.send_message(
        embed=discord.Embed(description="\n".join(lines), color=0x00FF41), ephemeral=True)


# ─────────────────────────────────────────────────────────────────
# SLASH COMMANDS — GOVERNANCE CATEGORY
# ─────────────────────────────────────────────────────────────────
governance_group = app_commands.Group(name="governance", description="Daemon governance proposals and voting")


@governance_group.command(name="proposal", description="Submit a governance proposal (7 days + random hours; 17-day cooldown)")
@app_commands.describe(
    percentage="% of circulating supply (YES weight) required to pass (1–100)",
    proposal_text="The proposal text (max 500 chars)"
)
async def proposal_cmd(interaction: discord.Interaction, percentage: int, proposal_text: str):
    if not (1 <= percentage <= 100):
        await interaction.response.send_message("❌ Percentage must be 1–100.", ephemeral=True)
        return
    if len(proposal_text) > 500:
        await interaction.response.send_message("❌ Max 500 characters.", ephemeral=True)
        return

    pid, err = create_proposal(interaction.user.id, percentage, proposal_text)
    if err:
        await interaction.response.send_message(f"❌ {err}", ephemeral=True)
        return

    prop = get_proposal(pid)
    view = ProposalView(pid)
    await interaction.response.send_message(embed=build_proposal_embed(prop), view=view)


@governance_group.command(name="vote", description="Vote on an active proposal")
@app_commands.describe(proposal_id="ID of the proposal", vote="Your vote: yes or no")
@app_commands.choices(vote=[
    app_commands.Choice(name="Yes", value="yes"),
    app_commands.Choice(name="No",  value="no"),
])
async def vote_cmd(interaction: discord.Interaction, proposal_id: int, vote: str):
    ok, msg = vote_proposal(proposal_id, interaction.user.id, vote)
    if not ok:
        await interaction.response.send_message(f"❌ {msg}", ephemeral=True)
        return
    prop = get_proposal(proposal_id)
    view = ProposalView(proposal_id)
    await interaction.response.send_message(embed=build_proposal_embed(prop), view=view)


@governance_group.command(name="bid", description="Bid for the next biddable proposal slot (every 2nd slot)")
@app_commands.describe(amount="Amount of DAEMON to bid (must exceed current top bid)")
async def proposal_bidding_cmd(interaction: discord.Interaction, amount: int):
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be positive.", ephemeral=True)
        return

    cycle = next_proposal_cycle()
    if not is_biddable_slot():
        await interaction.response.send_message(
            f"❌ Slot #{cycle} is not biddable. The next biddable slot is #{cycle + (PROPOSAL_BID_EVERY - cycle % PROPOSAL_BID_EVERY)}.",
            ephemeral=True)
        return

    ok, msg = place_bid(interaction.user.id, amount)
    if not ok:
        await interaction.response.send_message(f"❌ {msg}", ephemeral=True)
        return

    G = "\u001b[1;32m"; Y = "\u001b[0;33m"; W = "\u001b[0;37m"; D = "\u001b[0;90m"; R = "\u001b[0m"
    lines = [
        "```ansi",
        f"{G}╔══════════════════════════════════════════╗",
        f"║       P R O P O S A L   B I D D I N G    ║",
        f"╚══════════════════════════════════════════╝{R}",
        "",
        f"{Y}  Slot     ▸ #{cycle}{R}",
        f"{G}  Your bid ▸ {amount:,} DAEMON{R}",
        f"{W}  Balance  ▸ {get_balance(interaction.user.id):,} DAEMON{R}",
        "",
        f"{D}  Highest bidder wins the right to post the{R}",
        f"{D}  next proposal, bypassing the 17-day cooldown.{R}",
        f"{D}  Outbid = full refund. Winning bid is consumed.{R}",
        "",
        f"{D}  {ts_now()}{R}",
        "```",
    ]
    embed = discord.Embed(description="\n".join(lines), color=0xF7931A)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@daemon_group.command(name="split", description="Commit equally to Rock, Paper & Scissors in the current convergence")
@app_commands.describe(amount="Total DAEMON to commit — must be divisible by 3")
async def daemon_split_cmd(interaction: discord.Interaction, amount: int):
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be positive.", ephemeral=True)
        return
    if amount % 3 != 0:
        await interaction.response.send_message(f"❌ Amount must be divisible by 3 (you entered **{amount:,}**).", ephemeral=True)
        return
    arena = get_arena()
    if not arena["game_open"]:
        await interaction.response.send_message("❌ No convergence is currently active.", ephemeral=True)
        return
    uid = interaction.user.id
    bal = get_balance(uid)
    if bal < amount:
        await interaction.response.send_message(f"❌ Insufficient balance: `{bal:,}` {DAEMON_EMOJI}  (need {amount:,})", ephemeral=True)
        return
    arena = get_arena()
    if not arena["game_open"]:
        await interaction.response.send_message("❌ The convergence closed before your commit landed.", ephemeral=True)
        return
    share = amount // 3
    add_balance(uid, -amount)
    for choice in ("rock", "paper", "scissors"):
        add_investment(uid, choice, share, arena["game_date"], arena["game_id"])

    await interaction.response.send_message(
        embed=ok_embed(f"Split **{amount:,} DAEMON** evenly across Rock, Paper & Scissors."),
        ephemeral=True,
    )

    ch = get_channel_safe(bot, ARENA_CHANNEL)
    if ch:
        await ch.send(f"```diff\n+ {interaction.user.display_name} ({interaction.user.id}) split {amount:,} DAEMON evenly across Rock, Paper & Scissors\n```")
    await update_dashboard(get_arena())
    await update_satoshi_role(interaction.guild)


@daemon_group.command(name="dailysplit", description="Auto-commit equally to all three sides each convergence")
@app_commands.describe(amount="Total DAEMON per game (divisible by 3). Set to 0 to cancel.")
async def daemon_dailysplit_cmd(interaction: discord.Interaction, amount: int):
    uid = interaction.user.id
    if amount < 0:
        await interaction.response.send_message("❌ Amount cannot be negative.", ephemeral=True)
        return
    if amount == 0:
        expire_autosplit(uid)
        await interaction.response.send_message("✅ Daily auto-split **disabled**. No further automatic commits will be made.", ephemeral=True)
        return
    if amount % 3 != 0:
        await interaction.response.send_message(f"❌ Amount must be divisible by 3 (you entered **{amount:,}**).", ephemeral=True)
        return
    bal = get_balance(uid)
    if bal < amount:
        await interaction.response.send_message(f"❌ Insufficient balance for the first auto-split: `{bal:,}` {DAEMON_EMOJI}  (need {amount:,})", ephemeral=True)
        return
    expiry = get_autosplit_expiration()
    if expiry <= 0:
        await interaction.response.send_message(
            "❌ Auto-spread is currently disabled by admin.", ephemeral=True
        )
        return
    arena = get_arena()
    set_autosplit(uid, amount, expiry, arena["game_id"])
    await interaction.response.send_message(embed=ok_embed(f"Daily auto-split activated: **{amount:,} DAEMON** per game for **{expiry}** day(s)."), ephemeral=True)


@daemon_group.command(name="market", description="View the live Daemon buy/sell order book")
async def daemon_market_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    asks, bids = get_daemon_orderbook()
    PW, AW, CW = 12, 10, 10
    BAR_W = PW + AW + CW + 10

    def fmt_row(price, amt, cum, prefix):
        return f"{prefix} {price:>{PW},.4f}   {str(int(amt)):>{AW}}   {str(int(cum)):>{CW}}"

    header = f"  {'PRICE (🐉)':>{PW}}   {'AMOUNT':>{AW}}   {'CUMUL':>{CW}}"
    divider = "  " + "─" * (BAR_W - 2)
    lines = []
    if asks:
        for lvl in reversed(asks):
            lines.append(fmt_row(lvl["price"], lvl["amount"], lvl["cumulative"], "-"))
    else:
        lines.append(f"-  {'— no sell orders —':^{BAR_W - 4}}")
    if asks and bids:
        sp = asks[0]["price"] - bids[0]["price"]
        sp_pct = (sp / asks[0]["price"]) * 100 if asks[0]["price"] else 0
        mid_txt = f"Spread: {sp:,.4f}  ({sp_pct:.4f}%)"
    else:
        mid_txt = "No spread data"
    lines.append(f"  {mid_txt:^{BAR_W - 2}}")
    if bids:
        for lvl in bids:
            lines.append(fmt_row(lvl["price"], lvl["amount"], lvl["cumulative"], "+"))
    else:
        lines.append(f"+  {'— no buy orders —':^{BAR_W - 4}}")
    book_block = f"```diff\n{header}\n{divider}\n" + "\n".join(lines) + f"\n{divider}\n```"
    embed = discord.Embed(title="📊  Daemon Order Book", description=book_block, color=0x1E90FF)
    embed.set_footer(text="Red = asks (sell)  ·  Green = bids (buy)  ·  /daemon sell | /daemon buy  ·  prices in 🐉")
    await interaction.followup.send(embed=embed)


@daemon_group.command(name="sell", description="List Daemon for sale at a set price")
@app_commands.describe(amount="Amount of DAEMON to list", price_per="Price per DAEMON in 🐉")
async def daemon_sell_cmd(interaction: discord.Interaction, amount: int, price_per: float):
    await interaction.response.defer(ephemeral=True)
    if amount <= 0 or price_per <= 0:
        await interaction.followup.send(embed=err_embed("Amount and price must be positive."), ephemeral=True)
        return
    mc_uuid = await get_mc_uuid(str(interaction.user.id))
    if not mc_uuid:
        await interaction.followup.send(embed=err_embed("Link your Minecraft account first with `/economy link`."), ephemeral=True)
        return

    async with _daemon_market_lock:
        bal = get_balance(interaction.user.id)
        if bal < amount:
            await interaction.followup.send(embed=err_embed(f"Insufficient DAEMON balance: {bal:,}."), ephemeral=True)
            return

        order_id: int | None = None
        add_balance(interaction.user.id, -amount)
        try:
            order_id = insert_daemon_order(interaction.user.id, mc_uuid, "sell", amount, float(price_per))
            trades = await match_daemon_orders()
        except Exception as e:
            if order_id is not None:
                delete_daemon_order(order_id)
            add_balance(interaction.user.id, amount)
            await interaction.followup.send(embed=err_embed(f"Failed to place sell order: {e}"), ephemeral=True)
            return

    msg = (
        f"Sell order placed! **{amount:,}** DAEMON @ **🐉 {price_per:.4f}** each.\n"
        f"Order ID: **#{order_id}**"
    )
    if trades:
        own_fills = [t for t in trades if t["seller_id"] == interaction.user.id]
        if own_fills:
            sold = sum(t["amount"] for t in own_fills)
            value = sum(t["value"] for t in own_fills)
            msg += f"\nFilled immediately: **{sold:,}** DAEMON for **🐉 {value:,.4f}**."
    await interaction.followup.send(embed=ok_embed(msg), ephemeral=True)
    await log_transaction(
        interaction.guild,
        "📋 DAEMON Sell Order Placed",
        f"<@{interaction.user.id}> listed **{amount:,} DAEMON** @ **🐉 {price_per:.8f}** each · Order **#{order_id}**",
        color=BLUE,
    )
    await log_daemon_trades(interaction.guild, trades)
    await update_satoshi_role(interaction.guild)


@daemon_group.command(name="buy", description="Place a limit buy order for Daemon")
@app_commands.describe(amount="Amount of DAEMON to buy", price_per="Max price per DAEMON in 🐉 you'll pay")
async def daemon_buy_cmd(interaction: discord.Interaction, amount: int, price_per: float):
    await interaction.response.defer(ephemeral=True)
    if amount <= 0 or price_per <= 0:
        await interaction.followup.send(embed=err_embed("Amount and price must be positive."), ephemeral=True)
        return
    mc_uuid = await get_mc_uuid(str(interaction.user.id))
    if not mc_uuid:
        await interaction.followup.send(embed=err_embed("Link your Minecraft account first with `/economy link`."), ephemeral=True)
        return

    reserve = amount * float(price_per)
    try:
        vault_dragons = await get_vault_dragons(mc_uuid)
    except Exception as e:
        await interaction.followup.send(embed=err_embed(str(e)), ephemeral=True)
        return
    if vault_dragons < reserve:
        await interaction.followup.send(embed=err_embed(f"Insufficient 🐉 in vault: have {vault_dragons:,.4f}, need {reserve:,.4f}."), ephemeral=True)
        return

    async with _daemon_market_lock:
        order_id: int | None = None
        reservation_made = False
        try:
            await reserve_vault_dragons(mc_uuid, reserve)
            reservation_made = True
            order_id = insert_daemon_order(interaction.user.id, mc_uuid, "buy", amount, float(price_per))
            trades = await match_daemon_orders()
        except Exception as e:
            if order_id is not None:
                delete_daemon_order(order_id)
            if reservation_made:
                try:
                    await refund_vault_dragons(mc_uuid, reserve, f"daemon-buy-place-refund:{interaction.user.id}:{amount}:{price_per:.8f}:{uuid.uuid4().hex}")
                except Exception:
                    log.exception("[daemon market] failed to unwind buy reservation for user %s", interaction.user.id)
            await interaction.followup.send(embed=err_embed(f"Failed to place buy order: {e}"), ephemeral=True)
            return

    msg = (
        f"Buy order placed! Up to **{amount:,}** DAEMON @ **🐉 {price_per:.4f}** each.\n"
        f"Order ID: **#{order_id}**"
    )
    if trades:
        own_fills = [t for t in trades if t["buyer_id"] == interaction.user.id]
        if own_fills:
            bought = sum(t["amount"] for t in own_fills)
            value = sum(t["value"] for t in own_fills)
            msg += f"\nFilled immediately: **{bought:,}** DAEMON for **🐉 {value:,.4f}**."
    await interaction.followup.send(embed=ok_embed(msg), ephemeral=True)
    await log_transaction(
        interaction.guild,
        "📋 DAEMON Buy Order Placed",
        f"<@{interaction.user.id}> bid for **{amount:,} DAEMON** @ up to **🐉 {price_per:.8f}** each · Order **#{order_id}**",
        color=BLUE,
    )
    await log_daemon_trades(interaction.guild, trades)
    await update_satoshi_role(interaction.guild)


@daemon_group.command(name="orders", description="View your open Daemon buy/sell orders")
async def daemon_orders_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    orders = get_user_daemon_orders(interaction.user.id)
    if not orders:
        await interaction.followup.send(embed=discord.Embed(description="📭  You have no open Daemon orders.", color=0x1E90FF), ephemeral=True)
        return
    lines = []
    for o in orders:
        side_icon = "📤" if o["side"] == "sell" else "📥"
        lines.append(f"{side_icon} **#{o['id']}** {o['side'].upper()} {int(o['remaining']):,}/{int(o['amount']):,} DAEMON @ 🐉 {float(o['price_per']):.4f}")
    embed = discord.Embed(title="📋  Your Daemon Orders", description="\n".join(lines), color=0x1E90FF)
    await interaction.followup.send(embed=embed, ephemeral=True)


@daemon_group.command(name="cancel_order", description="Cancel one of your open Daemon orders")
@app_commands.describe(order_id="Order ID to cancel")
async def daemon_cancel_order_cmd(interaction: discord.Interaction, order_id: int):
    await interaction.response.defer(ephemeral=True)
    async with _daemon_market_lock:
        order = get_daemon_order(order_id)
        if not order or int(order["user_id"]) != interaction.user.id or int(order.get("remaining", 0)) <= 0:
            await interaction.followup.send(embed=err_embed("Order not found or not yours."), ephemeral=True)
            return

        remaining = int(order["remaining"])
        side = str(order["side"])
        mc_uuid = str(order["mc_uuid"])
        price_per = float(order["price_per"])

        try:
            if side == "sell":
                add_balance(interaction.user.id, remaining)
            else:
                await refund_vault_dragons(mc_uuid, remaining * price_per, f"daemon-cancel:{order_id}:{remaining}:{price_per:.8f}")
            delete_daemon_order(order_id)
        except Exception as e:
            await interaction.followup.send(embed=err_embed(f"Failed to cancel order: {e}"), ephemeral=True)
            return

    await interaction.followup.send(embed=ok_embed(f"Order **#{order_id}** cancelled."), ephemeral=True)
    await log_transaction(
        interaction.guild,
        "📋 DAEMON Order Cancelled",
        f"<@{interaction.user.id}> cancelled **{side.upper()}** order **#{order_id}** with **{remaining:,} DAEMON** remaining.",
        color=0xFF6F00,
    )
    await update_satoshi_role(interaction.guild)


@arena_group.command(name="cancel", description="[RESTRICTED] Void the current convergence and refund all commits")
async def arena_cancel_cmd(interaction: discord.Interaction):
    if interaction.user.id != GAMEFIX_USER_ID:
        await interaction.response.send_message("❌ Unauthorized.", ephemeral=True)
        return
    arena = get_arena()
    if not arena["game_open"]:
        await interaction.response.send_message("❌ No convergence is currently active.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    gid = arena["game_id"]
    refunds = refund_investments(gid)
    set_arena(game_open=0, pot=7200)
    ch = get_channel_safe(bot, ARENA_CHANNEL)
    if ch:
        await ch.send(embed=discord.Embed(description=f"```ansi\n\u001b[1;31m  CONVERGENCE {format_arena_display_id(arena['game_date'])} VOIDED\u001b[0m\n```", color=0xFF3333))
    await interaction.followup.send(embed=ok_embed(f"Convergence {format_arena_display_id(arena['game_date'])} voided. {len(refunds)} refund(s) issued."), ephemeral=True)


@arena_group.command(name="forcestart", description="[RESTRICTED] Immediately open a new convergence")
async def arena_forcestart_cmd(interaction: discord.Interaction):
    if interaction.user.id != GAMEFIX_USER_ID:
        await interaction.response.send_message("❌ Unauthorized.", ephemeral=True)
        return
    arena = get_arena()
    if arena["game_open"]:
        await interaction.response.send_message("❌ A convergence is already active.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    await _boot_open_arena()
    await interaction.followup.send(embed=ok_embed("New convergence opened."), ephemeral=True)


# ─────────────────────────────────────────────────────────────────
# ADMIN PREFIX COMMANDS
# ─────────────────────────────────────────────────────────────────
@daemon_group.command(name="dailysplitexpiration", description="[ADMIN] Set how many games a daily split lasts before expiring")
@app_commands.describe(games="Number of games until expiration (0 = never expires)")
async def daemon_dailysplit_expiration_cmd(interaction: discord.Interaction, games: int):
    if interaction.user.id != GAMEFIX_USER_ID:
        await interaction.response.send_message("❌ Unauthorized.", ephemeral=True)
        return
    if games < 0:
        await interaction.response.send_message("❌ Must be 0 or greater (0 = never expires).", ephemeral=True)
        return
    set_autosplit_expiration(games)
    msg = (f"✅ Daily split expiration set to **{games}** game(s)." if games > 0 else "✅ Daily split expiration **disabled** — splits will run indefinitely.")
    await interaction.response.send_message(msg, ephemeral=True)


@bot.command(name="supplycheck", hidden=True)
async def supply_check(ctx: commands.Context):
    if ctx.author.id != ADMIN_USER_ID: return
    em     = get_emission()
    arena  = get_arena()
    holders = count_holders()
    D = "\u001b[0;90m"; W = "\u001b[0;37m"; RR = "\u001b[1;31m"; R = "\u001b[0m"
    end_ts = arena.get("next_end_ts", 0)
    lines = [
        "```ansi",
        f"{RR}  ADMIN SUPPLY REPORT{R}",
        f"{W}  Minted      ▸ {em['cumulative_minted']:,} / {MAX_SUPPLY:,}{R}",
        f"{W}  Daily       ▸ {em['current_daily']:,}{R}",
        f"{W}  Red. Year   ▸ {em['reduction_year']}{R}",
        f"{W}  Cycle Start ▸ <t:{int(em['cycle_start_ts'])}:f>{R}",
        f"{W}  Holders     ▸ {holders}{R}",
        f"{W}  Convergence  ▸ {format_arena_display_id(arena['game_date'])}  Open={bool(arena['game_open'])}{R}",
        f"{W}  Next Consensus  ▸ <t:{int(end_ts)}:f>{R}",
        f"{D}  {ts_now()}{R}",
        "```",
    ]
    await ctx.author.send("\n".join(lines))



# ─────────────────────────────────────────────────────────────────
# TOP-LEVEL QUICK ARENA COMMANDS
# ─────────────────────────────────────────────────────────────────
@bot.tree.command(name="rock", description="Commit to Rock for the current arena")
@app_commands.describe(amount="Amount to commit (min 1)")
async def quick_rock_cmd(interaction: discord.Interaction, amount: int):
    await _invest_slash(interaction, "rock", amount)


@bot.tree.command(name="paper", description="Commit to Paper for the current arena")
@app_commands.describe(amount="Amount to commit (min 1)")
async def quick_paper_cmd(interaction: discord.Interaction, amount: int):
    await _invest_slash(interaction, "paper", amount)


@bot.tree.command(name="scissors", description="Commit to Scissors for the current arena")
@app_commands.describe(amount="Amount to commit (min 1)")
async def quick_scissors_cmd(interaction: discord.Interaction, amount: int):
    await _invest_slash(interaction, "scissors", amount)


# ─────────────────────────────────────────────────────────────────
# REGISTER COMMAND GROUPS & RUN
# ─────────────────────────────────────────────────────────────────
bot.tree.add_command(economy)
bot.tree.add_command(market_group)
bot.tree.add_command(lists_group)
bot.tree.add_command(daemon_group)
bot.tree.add_command(arena_group)
bot.tree.add_command(governance_group)

if __name__ == "__main__":
    init_db()
    token = TOKEN
    if not token:
        raise RuntimeError("DISCORD_TOKEN is not set")
    if not API_KEY:
        raise RuntimeError("API_KEY is not set")
    bot.run(token)
