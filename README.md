# MDragons Economy — GitHub-safe setup

This repository contains the Minecraft plugin, Discord bot, and FastAPI backend for the MDragons Economy / Economy++ system. This copy is sanitized for GitHub: real secrets and private runtime values should live in environment variables, not in committed source code.

## Quick start

1. Copy `.env.example` to `.env`.
2. Fill in your real Discord token, API key, role IDs, channel IDs, admin IDs, bot IDs, and database paths.
3. Keep `.env` private. It is intentionally ignored by `.gitignore`.
4. Start the FastAPI backend first, then the Discord bot, then point the Minecraft plugin at the backend.

## Important files

- `bot.py` — Discord bot for Economy++ Dragons, item markets, purchase lists, Daemon, arena, and governance.
- `app.py` — FastAPI backend and SQLite ledger for vault balances, markets, links, logs, bounties, and mechanics.
- `MDragonsEconomy.java` — Paper/Spigot plugin for Minecraft vault deposit, withdrawal, balance, bounties, and Discord linking.
- `plugin.yml` — Bukkit command registration.
- `.env.example` — safe placeholder configuration template.
- `.gitignore` — prevents `.env`, databases, caches, build artifacts, and `.jar` files from being committed.

## Environment setup

### Python services

The Python services read values from environment variables. Use `.env.example` as the template:

```bash
cp .env.example .env
```

Then edit `.env` and provide the real values for your server.

### Java plugin

The Java plugin does not automatically parse `.env` by itself. Set these variables in your host, Docker container, or server startup script:

```bash
export BACKEND_URL="http://your-backend-host:8000/api"
export API_KEY="your-real-api-key"
```

The plugin sends the API key as `X-API-Key` for protected backend requests.

## Normal Dragons currency: Economy++ 🐉

The normal Dragons currency is the main Economy++ vault currency shown as `🐉`. In the backend it is stored as `mdragons`, and it is different from `DAEMON`.

Dragons are used for the normal server economy:

- buying items from the item order book;
- receiving payments from item sales and purchase lists;
- transferring vault currency to other linked users;
- placing bounties;
- redeeming role rewards;
- cashout role unlocks;
- pricing DAEMON trades.

A player's vault balance can have three useful views:

- `🐉 Vault` — spendable Dragons currently in the player vault.
- `🐉 Locked in orders` — Dragons reserved in open buy orders.
- `🐉 Total` — vault plus locked Dragons.

Important distinction:

- `🐉 Dragons / MDragons / Economy++` are the normal server currency.
- `DAEMON` is a separate capped-supply token with its own balance ledger, arena, governance, and market.
- DAEMON can be traded against Dragons, so DAEMON prices are shown in `🐉`.

## Daemon summary

Daemon is the separate scarcity and coordination token. It has a hard cap of `21,000,000` units, begins with a daily arena emission of `7,200`, and reduces daily emission by `0.9×` at defined thresholds and yearly cycles.

The Daemon arena is a recurring Rock/Paper/Scissors-style convergence game. Users commit DAEMON to one or more choices. Commitments are final. At resolution, the smallest pool receives half of that cycle's emission, while the two larger pools fight by cyclic dominance for the other half. Winning-pool payouts use power-1.5 weighting, so larger concentrated commitments receive proportionally more.

Governance lets users submit proposals with a required yes-weight threshold. It is designed so the protocol can evolve if the participating base reaches sufficient agreement.

## Command reference

### Minecraft plugin commands

| Command | Purpose |
|---|---|
| `/discord` | Generates a 6-digit code for linking Minecraft to Discord. Use the code with `/economy link`. |
| `/deposit <item> <amount\|all>` | Deposits a supported item from inventory into the vault. Supports item names like `iron_ingot`, `oak_log`, `red_wool`, `diamond_block`, and `xp`. |
| `/deposit inv` | Deposits every supported item in your inventory. |
| `/withdraw <item> <amount> [variant]` | Withdraws a vaulted item back into Minecraft. Variant is used for color/wood commodities, such as `overworld_log 10 oak` or `wool 16 red`. |
| `/balance` | Shows full vault balance across Dragons, legacy items, and commodities. |
| `/balance <item>` | Opens the item balance GUI for one item. |
| `/itemgui <item>` | Opens a GUI for one item balance. Alias: `/itembalance`. |
| `/alive [page]` | Shows the Minecraft-days-alive leaderboard. |
| `/bounty <playername> <dragons>` | Places a Dragon bounty on a Minecraft player. Alias: `/bouny`. |
| `/helpmc [category]` | Shows deposit/withdraw item help. Categories: `ores`, `wood`, `farming`, `mob`, `blocks`, `nether`, `end`, `color`, `misc`. |

### Discord Economy++ commands

| Command | Purpose |
|---|---|
| `/economy link <code>` | Links Discord to Minecraft using the code from `/discord`. |
| `/economy balance` | Shows vault balance, including items, spendable Dragons, locked Dragons, and total Dragons. |
| `/economy leaderboard <item> [page]` | Shows a leaderboard for a non-dragon item such as `netherite`, `diamond`, `iron`, `dirt`, or `wool`. |
| `/economy give <user> <amount>` | Sends `🐉` from your Economy++ vault to another linked user. |
| `/economy inventory <item>` | Shows your total, available, and in-market amount for one item. |
| `/economy cashout` | Spends Dragons to unlock the configured cashout role tier, if eligible. |
| `/economy mdragons` | Redeems the configured MDragons role for Dragons added to your vault. |
| `/economy 10mdragons` | Redeems the configured 10× MDragons role for Dragons added to your vault. |

### Discord market commands

These commands trade vaulted Minecraft items for normal Dragons (`🐉`).

| Command | Purpose |
|---|---|
| `/market view <item> [spread]` | Shows the live order book for an item. Optional `spread` groups prices into wider levels. |
| `/market sell <item_type> <amount> <price_per>` | Lists vaulted items for sale at a Dragon price per unit. |
| `/market buy <item_type> <amount> <price_per>` | Places a limit buy order. Dragons are reserved until filled or cancelled. |
| `/market orders [page]` | Shows your active buy and sell orders. |
| `/market cancel <order_id>` | Cancels one open order and returns reserved items or Dragons. |
| `/market cancel_all [item]` | Cancels all your orders, or all orders for one item if `item` is supplied. |
| `/market status` | Shows the current special market mechanics and liquidity injection state. |
| `/market choose <diamond\|netherite>` | Mansa Musa / Netherite Overlord command to lock the next weekly market target. |
| `/market set_injection_cap <raise\|lower>` | Chairman command to raise or lower the weekly liquidity injection cap by `50,000 🐉`. |
| `/market injectioncaprange <minimum> <maximum>` | Chairman command to set the allowed weekly injection-cap range. |
| `/market pause_injection <pause\|resume>` | Admin command to pause or resume liquidity injection. |

### Discord purchase-list commands

Purchase lists let one player offer a total Dragon payout for a bundle of items, and another player instantly fills the list if they have all required items.

| Command | Purpose |
|---|---|
| `/lists create <name> <price> <items>` | Creates a purchase list. Example items format: `diamond:100,netherite:5`. |
| `/lists mine` | Shows your active purchase lists. |
| `/lists all` | Shows all open purchase lists. |
| `/lists delete <list_id>` | Deletes one of your purchase lists. |
| `/lists fill <list_id>` | Instantly sells all required items into a purchase list and receives the Dragon payout. |

### Discord Daemon commands

These commands use the separate DAEMON token.

| Command | Purpose |
|---|---|
| `/daemon balance` | Shows your DAEMON balance privately. |
| `/daemon send <user> <amount> [hidden] [message]` | Transfers DAEMON to another user. `hidden=true` sends anonymously. |
| `/daemon stats` | Shows public DAEMON supply, emission, phase, and holder stats. |
| `/daemon info` | Shows the in-bot DAEMON guide. |
| `/daemon split <amount>` | Commits DAEMON equally to Rock, Paper, and Scissors. Amount must be divisible by 3. |
| `/daemon dailysplit <amount>` | Auto-commits DAEMON equally each convergence. Use `0` to cancel. |
| `/daemon market` | Shows the DAEMON/Dragon order book. |
| `/daemon sell <amount> <price_per>` | Lists DAEMON for sale at a Dragon price per DAEMON. |
| `/daemon buy <amount> <price_per>` | Places a DAEMON buy order using Dragons from the linked vault. |
| `/daemon orders` | Shows your open DAEMON buy and sell orders. |
| `/daemon cancel_order <order_id>` | Cancels one DAEMON order. |
| `/daemon dailysplitexpiration <games>` | Admin command to set how many games a daily split lasts. `0` means no expiration. |

### Discord arena commands

These are DAEMON arena commands. The arena dashboard also has Rock/Paper/Scissors buttons that commit `111 DAEMON`.

| Command | Purpose |
|---|---|
| `/arena rock <amount>` | Commits DAEMON to Rock. |
| `/arena paper <amount>` | Commits DAEMON to Paper. |
| `/arena scissors <amount>` | Commits DAEMON to Scissors. |
| `/arena spread <amount>` | Splits DAEMON equally across Rock, Paper, and Scissors. Amount must be divisible by 3. |
| `/arena autospread <amount>` | Auto-splits DAEMON each convergence. Use `0` to cancel. |
| `/arena autospreadadjust <days>` | Admin command to limit how many days autospread may cover. |
| `/arena gamefix` | Restricted command to resend the active arena dashboard. |
| `/arena cancel` | Restricted command to void the current convergence and refund commitments. |
| `/arena forcestart` | Restricted command to immediately open a new convergence. |

### Top-level quick arena commands

| Command | Purpose |
|---|---|
| `/rock <amount>` | Shortcut for committing DAEMON to Rock. |
| `/paper <amount>` | Shortcut for committing DAEMON to Paper. |
| `/scissors <amount>` | Shortcut for committing DAEMON to Scissors. |

### Discord governance commands

| Command | Purpose |
|---|---|
| `/governance proposal <percentage> <proposal_text>` | Submits a governance proposal. `percentage` is the required yes weight of circulating supply, from 1 to 100. |
| `/governance vote <proposal_id> <yes\|no>` | Votes yes or no on an active proposal. |
| `/governance bid <amount>` | Bids DAEMON for the next biddable proposal slot. |

### Special / admin / role commands

| Command | Purpose |
|---|---|
| `/iamsatoshinakamoto <yes\|no>` | Toggles the Satoshi Nakamoto role claim if you are the current top DAEMON holder. |
| `/add-dragons <user> <amount>` | Admin command to credit Dragons to a user's Economy++ vault. |
| `/remove-dragons <user> <amount>` | Admin command to remove Dragons from a user's Economy++ vault. |
| `!supplycheck` | Hidden admin prefix command that DMs a DAEMON supply report. |

### Prefix economy commands

The bot also supports legacy `!` commands. For normal users:

| Command | Purpose |
|---|---|
| `!give <user> <amount>` | Sends Dragons to another linked user. |
| `!sell <item> <amount> <price_per>` | Places a sell order. |
| `!buy <item> <amount> <price_per>` | Places a buy order. |
| `!cancel <order_id>` | Cancels one order. |
| `!cancel_all [item]` | Cancels all orders, optionally filtered by item. |
| `!cancel-all [item]` | Same as `!cancel_all`. |
| `!market sell <item> <amount> <price_per>` | Alternate prefix form for `!sell`. |
| `!market buy <item> <amount> <price_per>` | Alternate prefix form for `!buy`. |
| `!market cancel <order_id>` | Alternate prefix form for `!cancel`. |
| `!market cancel_all [item]` | Alternate prefix form for `!cancel_all`. |

Configured relay bot accounts may also use admin-style prefix forms:

| Command | Purpose |
|---|---|
| `!give <sender_id> <recipient_id> <amount>` | Relay bot form of Dragon transfer. |
| `!sell <user_id> <item> <amount> <price_per>` | Relay bot form of sell order. |
| `!buy <user_id> <item> <amount> <price_per>` | Relay bot form of buy order. |
| `!cancel <user_id> <order_id>` | Relay bot form of order cancellation. |
| `!cancel_all <user_id> [item]` | Relay bot form of cancel-all. |

## Security checklist before pushing

Run these commands before committing:

```bash
git status
python -m compileall bot.py app.py
grep -R "DISCORD_TOKEN\|API_KEY\|change-me\|[A-Za-z0-9_-]\{50,\}" . --exclude-dir=.git
```

Do not commit:

- `.env`
- SQLite databases such as `*.db`, `*.sqlite`, `*.sqlite3`
- Discord tokens
- API keys
- real role/channel/admin IDs if your server is private
- built `.jar` files
- Python cache folders

If any real token or API key was ever committed or shared, rotate it before publishing the repository.

## Daemon whitepaper

Daemon: A Decentralized Convergence Protocol for Digital Scarcity and Collective Coordination
Abstract
Daemon is a capped-supply digital asset engineered to enable direct, trust-minimized value transfers and high-stakes collective coordination within a defined network. It integrates a straightforward peer-to-peer transfer system with a daily convergence arena that leverages combinatorial choice, ranked pool dynamics, and superlinear reward distribution. Through these mechanisms, participants allocate scarce resources toward uncertain outcomes, surface collective preferences via market-style signaling, and earn freshly issued units in proportion to the conviction and scale of their commitments.
The protocol enforces a strict maximum supply coupled with a diminishing emission schedule, guaranteeing progressive scarcity. Arena commitments are irreversible by design, pools are ranked and resolved using deterministic rules incorporating cyclic dominance, and intra-pool distributions apply a power-1.5 weighting to favor meaningful concentration of stake. These features promote substantive participation while penalizing low-conviction or overly dispersed allocations.
At its foundation, Daemon functions without external intermediaries for core operations: transfers execute atomically on a lightweight ledger, arena resolutions occur transparently, and emission follows algorithmic rules. Importantly, the protocol’s consensus and coordination mechanisms are not immutable. Should a sufficient majority of the network reach agreement through its governance process, the underlying rules—including the consensus mechanism itself—may be amended or supplanted. This evolutionary capacity allows Daemon to adapt over time, potentially expanding its utility to support a wide array of valuable applications, provided such changes are carefully formulated, rigorously justified, and attentive to the long-term interests of the participating base that secures and sustains the network.
In this way, Daemon aspires to serve as a flexible yet disciplined framework for coordination, risk-sharing, and value creation in digital environments.
1. Introduction
Many digital assets and community-driven economies struggle with persistent challenges: unconstrained issuance that erodes scarcity, over-reliance on centralized gatekeepers, and inadequate tools for expressing genuine conviction or coordinating around shared outcomes. Conventional transfer systems offer liquidity but little opportunity for participants to demonstrate asymmetric belief or to benefit from collective resolution in a non-linear, merit-based manner.
Daemon was conceived to overcome these shortcomings by establishing a self-contained digital scarcity token supported by auditable accounting and recurring community convergence events. Its core capabilities include:
Irreversible, direct transfers between participants.
A daily arena mechanism for committing tokens to discrete alternatives, resolved through aggregate behavior and combinatorial logic.
Algorithmically governed emission that rewards active, conviction-driven participation while respecting a hard supply ceiling.
A governance framework that not only adjusts operational parameters but empowers the network to evolve or replace its consensus mechanisms when broad consensus is achieved.
By design, Daemon balances rigidity in scarcity and commitment rules with flexibility in its evolutionary path. This duality ensures the protocol can remain relevant and useful across changing circumstances, always prioritizing the integrity and sustained participation of its holder and miner-like base—the community members whose commitments and holdings underpin the system’s value and security.
2. The Problem of Digital Coordination and Scarcity
Digital environments frequently fail to establish credible, long-term scarcity. Unlimited or poorly governed issuance dilutes incentives, while centralized control introduces single points of failure and incentive misalignment. Effective coordination—whether allocating resources among competing priorities or surfacing collective judgment—demands mechanisms that reward skin-in-the-game, penalize cheap signaling, and resist capture by low-effort or sybil actors.
Linear reward models exacerbate fragmentation, as participants are incentivized to spread commitments thinly rather than concentrate them where conviction is strongest. Irreversible allocation mechanisms are rare, limiting the informational value of observed behavior. Moreover, rigid consensus rules risk obsolescence; a protocol that cannot evolve intelligently may stagnate even when superior designs emerge that better serve its users.
A principled solution therefore requires:
Strict supply discipline enforced through diminishing emission toward a hard cap.
Irreversible commitments coupled with non-linear (superlinear) reward functions that amplify the voice of substantial, concentrated stakes.
Transparent, deterministic resolution logic resistant to post-commitment manipulation.
Governance that is powerful enough to refine or overhaul the consensus mechanism itself, yet sufficiently guarded to demand genuine majority support and thoughtful proposal design.
Such a system must remain mindful of its foundational participants—the “miner base” in the broader sense of those who actively secure, use, and hold the asset—ensuring that any evolution demonstrably advances collective utility without undermining the incentives that brought the network into existence.
3. The Daemon Protocol
Daemon comprises three tightly integrated layers: the token economy and transfer system, the daily convergence arena, and the emission-governance framework.
3.1 Token and Transfers
Balances are maintained in a verifiable ledger tied to participant identifiers. Transfers are atomic and final: the sender’s balance decreases while the recipient’s increases, with optional transparent logging. This design mirrors the irreversibility emphasized throughout the protocol and supports seamless integration with external bridged assets via reservation and refund primitives that preserve ledger consistency.
3.2 The Daily Convergence Arena
The arena functions as the primary engine of coordination and reward distribution. Each cycle opens with three mutually exclusive options (traditionally denoted Rock, Paper, and Scissors). Participants may commit any integer quantity of Daemon (minimum one unit) to any combination of options. Individual choices remain private at submission time, though aggregate participant totals are visible via a live leaderboard. Commitments are binding and cannot be modified or withdrawn.
Upon cycle termination:
Three pools are formed from total commitments per option.
Pools are ranked by size, with tiebreakers based on the timestamp of each pool’s first commitment (earliest timestamps favored for higher ranks, latest for the underdog position).
Resolution proceeds as follows:
The underdog (smallest) pool automatically claims half the daily emission.
The largest and second-largest pools engage in a cyclic dominance contest; the victor claims the remaining half.
Commitments in losing pools are forfeited.
Within each winning pool, the emission tranche is distributed according to a power-1.5 weighting formula: an individual’s share equals their commitment raised to the 1.5 power, divided by the sum of all such weighted commitments in the pool, multiplied by the allocated reward.
Special handling ensures fairness in low-participation or tied scenarios. The randomized resolution timestamp within each daily window adds strategic depth and discourages last-moment gaming.
This structure transforms the arena into a dynamic preference-revelation mechanism, where participants’ allocations reflect beliefs about relative strengths, and outcomes richly reward accurate foresight and decisive positioning.
3.3 Emission Schedule and Scarcity
Daemon observes a hard cap of 21,000,000 units. Daily emission commences at 7,200 units and is released exclusively through successful arena resolutions (supplemented by modest initial distributions). Upon reaching defined cumulative thresholds and at annual intervals thereafter, the daily rate contracts by a factor of 0.9. This geometrically diminishing schedule drives the circulating supply toward the cap, embedding long-term scarcity into the protocol’s DNA.
3.4 Optional Market Layer
A limit-order book against linked external assets provides additional liquidity and price discovery. Orders match atomically when bid-ask conditions are satisfied, maintaining the integrity of both the arena and transfer layers.
3.5 Governance and Evolutionary Consensus
Governance is conducted through formalized proposals that specify a required approval threshold expressed as a percentage of circulating supply. Voting employs weighted commitments, supported by cooldowns and priority-bidding mechanisms to elevate signal quality and deter noise.
A defining feature of Daemon is its recognition that no consensus mechanism is eternal. If a sufficiently strong majority of the network concurs via the governance process, the protocol’s consensus rules—including arena resolution logic, weighting functions, emission mechanics, or the governance system itself—may be modified or entirely replaced. Such changes are expected to occur judiciously. Proposals must be meticulously drafted, clearly articulated, and demonstrably oriented toward enhancing the protocol’s long-term utility. Particular care is given to preserving and strengthening the position of the active participant base—the holders and committed users whose ongoing engagement constitutes the true security and vitality of the network.
In this manner, Daemon is engineered not as a static artifact but as a living protocol capable of intelligent self-improvement. Evolution remains subordinate to consensus, ensuring that any transformation serves the collective interest rather than narrow or transient agendas.
4. Incentives and Game-Theoretic Properties
The combination of irreversible commitments, superlinear (power-1.5) rewards, and the underdog bonus cultivates an environment that values conviction, concentration, and informed risk-taking. Cyclic dominance between leading pools maintains competitive tension, while hidden individual choices and randomized timing mitigate collusion and front-running.
The governance layer, including its capacity to evolve consensus, introduces a meta-incentive for thoughtful stewardship. Participants are encouraged to propose upgrades only when they can credibly argue that the changes will benefit the broader ecosystem and its foundational users. This design aligns short-term actions with long-term network health.
5. Security and Implementation Considerations
Daemon is realized through a robust, lightweight implementation centered on a transactional SQLite ledger, augmented with concurrency controls for critical sections such as arena resolution and order matching. External asset bridges employ careful reservation patterns to avoid double-spending or desynchronization risks.
While technical safeguards are essential, the protocol ultimately relies on the integrity and engagement of its participant majority. Governance upgrades to the consensus mechanism demand high thresholds and transparent deliberation, reducing the likelihood of hasty or exploitative alterations. Ongoing community review, periodic technical assessments, and gradual rollout of significant changes are strongly encouraged.
6. Conclusion
Daemon represents a coherent synthesis of digital scarcity, irreversible economic signaling, and adaptive collective coordination. Its capped supply, diminishing emission, daily convergence arena with power-weighted outcomes, and flexible governance framework together create a powerful instrument for value expression and discovery.
By embedding the possibility of consensus-driven evolution—including fundamental changes to the consensus mechanism itself—Daemon positions itself as a protocol that can grow in sophistication and applicability while remaining anchored to the interests of its active base. Any future modifications must be well-considered, clearly communicated, and oriented toward producing genuine, sustained utility for the network that mines, holds, and stewards it.
Ultimately, Daemon transcends the role of a simple token. It constitutes a foundational protocol for coordinated belief and shared upside in digital space—one that invites thoughtful participation today and intelligent adaptation tomorrow.

Appendix: Mathematical Notes
Power Weighting: For commitments c1,c2,…,cn c_1, c_2, \dots, c_n c1​,c2​,…,cn​ in a winning pool, an individual’s reward share is ci1.5∑jcj1.5×R \frac{c_i^{1.5}}{\sum_j c_j^{1.5}} \times R ∑j​cj1.5​ci1.5​​×R, where R R R denotes the pool’s allocated emission.
Tiebreaker Rule: Equal-sized pools are ranked according to the timestamp of their first commitment (earliest for higher ranks, latest for underdog).
Emission Reduction: Daily rate updates follow dt+1=⌊0.9×dt⌋ d_{t+1} = \lfloor 0.9 \times d_t \rfloor dt+1​=⌊0.9×dt​⌋ at predefined thresholds and yearly cycles.
This whitepaper describes the principles and mechanics of the Daemon protocol in its present form. As a living system, Daemon will continue to develop through the informed will of its participant majority, always with careful regard for the long-term health and incentives of its foundational community.
