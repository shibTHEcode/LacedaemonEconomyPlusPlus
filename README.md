# Lacedaemon Economy++

> Minecraft vault economy + Discord markets + DAEMON convergence protocol.

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-backend-009688)
![Paper](https://img.shields.io/badge/Paper%2FSpigot-1.21-orange)
![Security](https://img.shields.io/badge/GitHub--safe-no%20secrets-success)

Lacedaemon Economy++ connects a Minecraft server, a Discord community, and a lightweight SQLite ledger into one server economy.

It has two separate economic layers:

- **Economy++ Dragons** — the normal server currency used for vault balances, item markets, bounties, role redemptions, purchase lists, and DAEMON pricing.
- **DAEMON** — a separate capped-supply coordination token with transfers, a market, governance, and the convergence arena described in `Daemon Whitepaper.pdf`.

This repository is designed to be public-safe: real tokens, API keys, Discord IDs, channel IDs, role IDs, and seed balances belong in environment variables, not committed source.

---

## What is included

| File | Purpose |
|---|---|
| `app.py` | FastAPI backend, SQLite ledger, vault balances, order books, bounties, logs, linking, mechanics, and backups. |
| `bot.py` | Discord bot for Economy++, markets, purchase lists, DAEMON, arena, governance, role automation, and admin tools. |
| `MDragonsEconomy.java` | Paper/Spigot plugin for Minecraft deposits, withdrawals, balances, linking, alive tracking, and bounties. |
| `plugin.yml` | Bukkit/Paper command registration. |
| `Daemon Whitepaper.pdf` | Long-form DAEMON protocol description. |
| `.env.example` | Safe configuration template. |
| `.gitignore` | Keeps secrets, databases, caches, and build outputs out of Git. |

---

## Security model

The backend API is internal infrastructure. Treat it like an admin service.

This GitHub-safe update makes these security assumptions explicit:

1. `API_KEY` is required for `app.py`.
2. Every `/api/...` backend route is protected with `X-API-Key`, except `/api/health`.
3. The Minecraft plugin no longer has a hardcoded fallback API key.
4. The Discord bot refuses to start without both `DISCORD_TOKEN` and `API_KEY`.
5. Discord role IDs, channel IDs, admin IDs, relay bot IDs, and DAEMON initial balances are loaded from environment variables.

Never commit:

- `.env`
- Discord bot tokens
- API keys
- SQLite files such as `*.db`, `*.sqlite`, `*.sqlite3`
- built plugin jars unless they are intentional release artifacts
- Python cache folders
- server-private role/channel/admin IDs if you do not want them public

If a real token or API key was ever shared or committed, rotate it before deploying this version.

---

## Quick start

### 1. Create your environment file

```bash
cp .env.example .env
```

Edit `.env` and fill in the real values for your server.

Generate a strong API key, for example:

```bash
python - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
```

Use the same `API_KEY` for:

- the FastAPI backend;
- the Discord bot;
- the Minecraft plugin runtime environment.

### 2. Start the backend

```bash
export API_KEY="replace-with-a-long-random-value"
export DB_PATH="/app/data/mdragons.db"
uvicorn app:app --host 0.0.0.0 --port 8000
```

Health check:

```bash
curl http://localhost:8000/api/health
```

Protected route check:

```bash
curl -H "X-API-Key: $API_KEY" http://localhost:8000/api/orders
```

### 3. Start the Discord bot

```bash
export DISCORD_TOKEN="your-discord-bot-token"
export API_KEY="same-api-key-as-backend"
export BACKEND_URL="http://mdragons-backend:8000/api"
python bot.py
```

### 4. Run the Minecraft plugin

The Java plugin reads environment variables from the server process or container. It does **not** parse `.env` by itself.

Set these in your host, Docker container, or startup script:

```bash
export BACKEND_URL="http://your-backend-host:8000/api"
export API_KEY="same-api-key-as-backend"
```

Then place the compiled plugin jar in your server's `plugins/` folder.

---

## Environment variables

### Required

| Variable | Used by | Purpose |
|---|---|---|
| `DISCORD_TOKEN` | `bot.py` | Discord bot token. |
| `API_KEY` | `app.py`, `bot.py`, Java plugin | Shared backend API key sent as `X-API-Key`. |

### Backend and database

| Variable | Default | Purpose |
|---|---:|---|
| `DB_PATH` | `/app/data/mdragons.db` | Economy++ backend SQLite database. |
| `DAEMON_DB_PATH` | `/app/data/daemon.db` | DAEMON SQLite database used by the Discord bot. |
| `BACKEND_URL` | `http://mdragons-backend:8000/api` | Backend URL used by bot/plugin. |

### Discord IDs

Set these to your real server values in `.env`.

| Variable | Purpose |
|---|---|
| `CASHOUT_ROLE_ID` | First cashout role. |
| `TEN_CASHOUT_ROLE_ID` | Higher cashout role. |
| `MDRAGONS_ROLE_ID` | MDragons redemption role. |
| `TEN_MDRAGONS_ROLE_ID` | 10× MDragons redemption role. |
| `CHAIRMAN_ROLE_ID` | Market governance/chairman role. |
| `MANSA_MUSA_ROLE_ID` | Top total Dragons role. |
| `NETHERITE_OVERLORD_ROLE_ID` | Top netherite holder role. |
| `SATOSHI_NAKAMOTO_ROLE_ID` | Claimed top DAEMON holder role. |
| `ANNOUNCEMENT_CHANNEL_ID` | Economy announcement channel. |
| `TRANSACTION_LOG_CHANNEL_ID` | General transaction log channel. |
| `DEPOSIT_LOG_CHANNEL_ID` | Deposit/withdraw log channel. |
| `TRADE_LOG_CHANNEL_ID` | Trade log channel. |
| `EXTERNAL_ECONOMY_LOG_ID` | External economy scanner/log channel. |
| `COMMIT_LOG_CHANNEL_ID` | DAEMON arena commit log channel. |
| `DAEMON_TRANSACTION_CHANNEL_ID` | DAEMON public transaction channel. |
| `ARENA_CHANNEL_ID` | DAEMON arena dashboard channel. |
| `ADMIN_USER_ID` | Hidden DAEMON supply/admin report user. |
| `GAMEFIX_USER_ID` | Restricted arena fix/cancel/force-start user. |
| `PAUSE_INJECTION_USER_ID` | User allowed to pause/resume market injection and adjust Dragons. |
| `UNBELIEVABOAT_BOT_ID` | External bot ID used for legacy exchange detection. |
| `EXTERNAL_ECONOMY_BOT_ID` | External Economy++ relay/scanner bot ID. |
| `EXTERNAL_ECONOMY_SCANNER` | External economy scanner channel/user setting used by the bot. |
| `PREFIX_COMMAND_BOT_IDS` | Comma-separated bot IDs allowed to issue relay prefix commands. |
| `COMMAND_SYNC_GUILD_IDS` | Optional comma-separated guild IDs for command sync. |
| `SYNC_COMMANDS_TO_GUILDS` | Set to `0` to disable startup guild command sync. |
| `DAEMON_EMOJI` | Optional DAEMON display string or custom Discord emoji. |

### DAEMON seed balances

Use this only when creating a fresh DAEMON database.

```bash
DAEMON_INITIAL_BALANCES='{"123":7200,"456":3600}'
```

Existing databases are not overwritten by this value.

---

## Economy++ Dragons

Dragons are the normal server economy currency. In the backend they are stored as `balances.mdragons`.

Dragons are used for:

- buying and selling vaulted Minecraft items;
- filling purchase lists;
- giving Dragons to linked users;
- placing and claiming bounties;
- role redemption and cashout flows;
- pricing DAEMON trades.

A player can have:

| Balance view | Meaning |
|---|---|
| Vault Dragons | Spendable Dragons currently in the Economy++ vault. |
| Locked Dragons | Dragons reserved in open buy orders. |
| Total Dragons | Vault Dragons + locked Dragons. |

The Mansa Musa role uses total Dragons. Netherite Overlord uses vault netherite plus netherite locked in open sell orders.

---

## Minecraft vault

The Minecraft plugin lets players deposit items into the backend vault and withdraw them later.

Some items use converted base units:

| Minecraft item | Vault unit |
|---|---|
| `iron_block` | `1 iron` |
| `iron_ingot` | `1/9 iron` |
| `gold_block` | `1 gold` |
| `gold_ingot` | `1/9 gold` |
| `gold_nugget` | `1/81 gold` |
| `diamond_block` | `9 diamond` |
| `netherite_scrap` | Converts 4 scrap to 1 netherite ingot. |
| `xp` | Stored as XP amount. |

Most other supported commodities are stored in `commodity_balances`.

---

## Markets

Markets are limit order books, not a central shop.

| Order type | What is locked |
|---|---|
| Sell order | Vaulted item amount. |
| Buy order | Dragons equal to `amount × price_per`. |

Matching is handled by the backend inside transaction-style critical sections. Filled trades are logged for Discord relay.

Prefix commands such as `!buy`, `!sell`, `!cancel`, and `!give` still exist for compatibility. Bot-authored relay prefix commands are accepted only from IDs in `PREFIX_COMMAND_BOT_IDS`.

---

## DAEMON

DAEMON is separate from Dragons.

The whitepaper defines DAEMON as a capped-supply digital asset for direct transfers and high-stakes collective coordination. The current protocol summary:

- hard maximum supply: `21,000,000 DAEMON`;
- initial daily arena emission: `7,200 DAEMON`;
- emission reduction factor: `0.9×` at defined thresholds and yearly cycles;
- direct ledger transfers;
- a DAEMON/Dragon order book;
- a recurring Rock/Paper/Scissors convergence arena;
- irreversible commitments;
- power-1.5 reward weighting;
- governance proposals and voting.

### Arena resolution

Each convergence cycle has three pools:

- Rock
- Paper
- Scissors

Resolution:

1. Rank pools by total committed DAEMON.
2. The smallest pool receives half of the cycle emission.
3. The largest and second-largest pools fight by normal Rock/Paper/Scissors dominance.
4. The winner of that fight receives the other half.
5. Winning-pool participants split their tranche by power-1.5 weighting.

Payout formula:

```text
share = commit^1.5 / sum(winning_pool_commit^1.5) * pool_reward
```

---

## Command reference

### Minecraft commands

| Command | Purpose |
|---|---|
| `/discord` | Generate a six-digit link code. |
| `/deposit <item> <amount|all>` | Deposit a supported item or XP. |
| `/deposit inv` | Deposit all supported inventory items. |
| `/withdraw <item> <amount> [variant]` | Withdraw vaulted items or XP. |
| `/balance [item]` | Show vault balance or open item GUI. |
| `/itemgui <item>` | Open a balance GUI for one item. |
| `/alive [page]` | Show Minecraft-days-alive leaderboard. |
| `/bounty <playername> <dragons>` | Place a Dragon bounty. |
| `/bounties [page]` | Show active Dragon bounties. |
| `/helpmc [category]` | Show supported item help. |

### Discord Economy++ commands

| Command | Purpose |
|---|---|
| `/economy link <code>` | Link Discord to Minecraft. |
| `/economy balance` | Show vault, locked, and total balances. |
| `/economy leaderboard <item> [page]` | Show item or Dragon leaderboards. |
| `/economy bounties [page]` | Show active bounties. |
| `/economy give <user> <amount>` | Send Dragons to another linked user. |
| `/economy inventory <item>` | Show vault and market inventory for one item. |
| `/economy cashout` | Spend Dragons for configured cashout role. |
| `/economy mdragons` | Redeem configured MDragons role. |
| `/economy 10mdragons` | Redeem configured 10× MDragons role. |
| `/economy health` | Admin health check. |
| `/economy backup [reason]` | Admin database backup. |

### Discord market commands

| Command | Purpose |
|---|---|
| `/market view <item> [spread]` | Show order book. |
| `/market sell <item_type> <amount> <price_per>` | Place sell order. |
| `/market buy <item_type> <amount> <price_per>` | Place buy order. |
| `/market orders [page]` | Show your open orders. |
| `/market cancel <order_id>` | Cancel one order. |
| `/market cancel_all [item]` | Cancel all orders, optionally for one item. |
| `/market status` | Show market mechanics. |
| `/market choose <diamond|netherite>` | Role-holder market choice. |
| `/market set_injection_cap <raise|lower>` | Chairman cap adjustment. |
| `/market injectioncaprange <minimum> <maximum>` | Chairman cap bounds. |
| `/market pause_injection <pause|resume>` | Admin pause/resume injection. |

### Discord DAEMON commands

| Command | Purpose |
|---|---|
| `/daemon balance` | Show DAEMON balance privately. |
| `/daemon send <user> <amount> [hidden] [message]` | Transfer DAEMON. |
| `/daemon stats` | Show supply and emission stats. |
| `/daemon info` | Show DAEMON guide. |
| `/daemon split <amount>` | Commit equally to all arena choices. |
| `/daemon dailysplit <amount>` | Auto-commit equally each convergence. |
| `/daemon market` | Show DAEMON/Dragon order book. |
| `/daemon sell <amount> <price_per>` | List DAEMON for Dragons. |
| `/daemon buy <amount> <price_per>` | Bid for DAEMON using Dragons. |
| `/daemon orders` | Show DAEMON orders. |
| `/daemon cancel_order <order_id>` | Cancel DAEMON order. |

### Arena and governance

| Command | Purpose |
|---|---|
| `/arena rock <amount>` | Commit DAEMON to Rock. |
| `/arena paper <amount>` | Commit DAEMON to Paper. |
| `/arena scissors <amount>` | Commit DAEMON to Scissors. |
| `/arena spread <amount>` | Split DAEMON across all choices. |
| `/arena autospread <amount>` | Auto-split each convergence. |
| `/arena gamefix` | Restricted dashboard resend. |
| `/arena cancel` | Restricted void/refund current convergence. |
| `/arena forcestart` | Restricted open new convergence. |
| `/rock <amount>` | Quick Rock commit. |
| `/paper <amount>` | Quick Paper commit. |
| `/scissors <amount>` | Quick Scissors commit. |
| `/governance proposal <percentage> <proposal_text>` | Submit proposal. |
| `/governance vote <proposal_id> <yes|no>` | Vote on proposal. |
| `/governance bid <amount>` | Bid for next biddable proposal slot. |
| `/iamsatoshinakamoto <yes|no>` | Claim/toggle top DAEMON holder role. |

---

## Validation before pushing

Run at least:

```bash
python -m py_compile app.py bot.py
grep -R "DISCORD_TOKEN\|API_KEY\|[A-Za-z0-9_-]\{50,\}" . --exclude-dir=.git --exclude-dir=__pycache__
```

For the Java plugin, compile against your Paper API jar before publishing a release jar.

Suggested Git workflow:

```bash
git status
git diff -- README.md app.py bot.py MDragonsEconomy.java plugin.yml .env.example .gitignore
git add README.md app.py bot.py MDragonsEconomy.java plugin.yml .env.example .gitignore
git commit -m "Secure public config and refresh README"
git push
```

---

## Notes for operators

- Keep `Daemon Whitepaper.pdf` as the long-form protocol document.
- Treat this README as the practical setup and operator guide.
- Do not rename existing API endpoints, slash commands, database tables, or columns without a migration plan.
- Back up both SQLite databases before deploying behavior changes.
- Rotate secrets immediately if you suspect they were exposed.
