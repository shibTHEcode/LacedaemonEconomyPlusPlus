package com.mdragons.mdragonseconomyplugin;

import org.bukkit.GameMode;
import org.bukkit.Material;
import org.bukkit.Bukkit;
import org.bukkit.OfflinePlayer;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.Listener;
import org.bukkit.event.entity.PlayerDeathEvent;
import org.bukkit.event.player.PlayerInteractEvent;
import org.bukkit.event.player.PlayerJoinEvent;
import org.bukkit.event.player.PlayerMoveEvent;
import org.bukkit.inventory.Inventory;
import org.bukkit.inventory.ItemStack;
import org.bukkit.inventory.meta.ItemMeta;
import org.bukkit.plugin.java.JavaPlugin;

import java.io.*;
import java.net.*;
import java.util.*;

public class MDragonsEconomy extends JavaPlugin implements Listener {

    private static final String BACKEND_URL = System.getenv().getOrDefault("BACKEND_URL", "http://mdragons-backend:8000/api");
    private static final String API_KEY = System.getenv().getOrDefault("API_KEY", "").trim();
    private static final long AFK_TIMEOUT_MS = 5L * 60L * 1000L;

    // ─── Commodity registry ───────────────────────────────────────────────────
    // Maps every accepted Material → (commodityKey, baseUnitsPerItem)
    // baseUnitsPerItem: how many base units 1 of this item represents.
    //   e.g. IRON_BLOCK = 1 and IRON_INGOT = 1/9 (vault stores iron as blocks)
    //        DIAMOND_BLOCK = 9 (1 block = 9 diamonds)
    record CEntry(String commodity, double ratio) {}

    private static final Map<Material, CEntry>            REG      = new LinkedHashMap<>();
    private static final Map<String, Map<String, Material>> VARIANTS = new LinkedHashMap<>();
    private static final Map<String, Material>             DEF_OUT  = new LinkedHashMap<>();
    private static final Map<String, Material>             ALIASES  = new LinkedHashMap<>();

    private static void reg(Material m, String commodity, double ratio) {
        REG.put(m, new CEntry(commodity, ratio));
    }

    static {
        // ── Ores ─────────────────────────────────────────────────────────────
        // Coal (base = block = 1; item deposits as 1/9)
        reg(Material.COAL_BLOCK, "coal", 1.0);
        reg(Material.COAL,       "coal", 1.0 / 9);
        DEF_OUT.put("coal", Material.COAL_BLOCK);

        // Iron (base = block = 1; ingot deposits as 1/9)
        reg(Material.IRON_BLOCK, "iron", 1.0);
        reg(Material.IRON_INGOT, "iron", 1.0 / 9);
        DEF_OUT.put("iron", Material.IRON_BLOCK);

        // Gold (base = block = 1; ingots/nuggets deposit as fractions)
        reg(Material.GOLD_BLOCK,  "gold", 1.0);
        reg(Material.GOLD_INGOT,  "gold", 1.0 / 9);
        reg(Material.GOLD_NUGGET, "gold", 1.0 / 81);
        DEF_OUT.put("gold", Material.GOLD_BLOCK);

        // Copper (base = ingot = 1; block deposits as 9)
        reg(Material.COPPER_BLOCK, "copper", 9.0);
        reg(Material.COPPER_INGOT, "copper", 1.0);
        DEF_OUT.put("copper", Material.COPPER_INGOT);

        // Diamond (base = item = 1) — order-book item, routed to legacy endpoint
        reg(Material.DIAMOND,       "diamond", 1.0);
        reg(Material.DIAMOND_BLOCK, "diamond", 9.0);
        DEF_OUT.put("diamond", Material.DIAMOND);

        // Emerald (base = block = 1; item deposits as 1/9)
        reg(Material.EMERALD_BLOCK, "emerald", 1.0);
        reg(Material.EMERALD,       "emerald", 1.0 / 9);
        DEF_OUT.put("emerald", Material.EMERALD_BLOCK);

        // Redstone (base = dust = 1; block deposits as 9)
        reg(Material.REDSTONE_BLOCK, "redstone", 9.0);
        reg(Material.REDSTONE,       "redstone", 1.0);
        DEF_OUT.put("redstone", Material.REDSTONE);

        // Lapis (base = lapis lazuli = 1; block deposits as 9)
        reg(Material.LAPIS_BLOCK,  "lapis", 9.0);
        reg(Material.LAPIS_LAZULI, "lapis", 1.0);
        DEF_OUT.put("lapis", Material.LAPIS_LAZULI);

        // ── Netherite — order-book item, routed to legacy endpoint ────────────
        // Scrap is handled specially: requires multiples of 4, converts to ingots
        reg(Material.NETHERITE_INGOT, "netherite", 1.0);
        reg(Material.NETHERITE_SCRAP, "netherite", 0.25); // special path in deposit
        DEF_OUT.put("netherite", Material.NETHERITE_INGOT);

        // ── Stone (each separate, 1:1) ────────────────────────────────────────
        reg(Material.STONE,            "stone",        1.0); DEF_OUT.put("stone",        Material.STONE);
        reg(Material.COBBLESTONE,      "cobblestone",  1.0); DEF_OUT.put("cobblestone",  Material.COBBLESTONE);
        reg(Material.DEEPSLATE,        "deepslate",    1.0);
        reg(Material.COBBLED_DEEPSLATE,"deepslate",    1.0); DEF_OUT.put("deepslate",    Material.DEEPSLATE);
        reg(Material.BLACKSTONE,       "blackstone",   1.0); DEF_OUT.put("blackstone",   Material.BLACKSTONE);
        reg(Material.BASALT,           "basalt",       1.0); DEF_OUT.put("basalt",       Material.BASALT);

        // ── Wood – overworld logs (all variants → same commodity) ─────────────
        Map<String, Material> logV = new LinkedHashMap<>();
        logV.put("oak",      Material.OAK_LOG);
        logV.put("spruce",   Material.SPRUCE_LOG);
        logV.put("birch",    Material.BIRCH_LOG);
        logV.put("jungle",   Material.JUNGLE_LOG);
        logV.put("acacia",   Material.ACACIA_LOG);
        logV.put("dark_oak", Material.DARK_OAK_LOG);
        logV.put("mangrove", Material.MANGROVE_LOG);
        logV.put("cherry",   Material.CHERRY_LOG);
        for (Material m : logV.values()) reg(m, "overworld_log", 1.0);
        VARIANTS.put("overworld_log", logV);
        DEF_OUT.put("overworld_log", Material.OAK_LOG);

        // Nether logs
        Map<String, Material> netherLogV = new LinkedHashMap<>();
        netherLogV.put("crimson", Material.CRIMSON_STEM);
        netherLogV.put("warped",  Material.WARPED_STEM);
        for (Material m : netherLogV.values()) reg(m, "nether_log", 1.0);
        VARIANTS.put("nether_log", netherLogV);
        DEF_OUT.put("nether_log", Material.CRIMSON_STEM);

        // ── Farming ───────────────────────────────────────────────────────────
        reg(Material.WHEAT,        "wheat",     1.0);
        reg(Material.BREAD,        "wheat",     3.0);   // 3 wheat per bread
        DEF_OUT.put("wheat", Material.WHEAT);

        reg(Material.CARROT,       "carrot",    1.0); DEF_OUT.put("carrot",    Material.CARROT);
        reg(Material.POTATO,       "potato",    1.0); DEF_OUT.put("potato",    Material.POTATO);
        reg(Material.BEETROOT,     "beetroot",  1.0); DEF_OUT.put("beetroot",  Material.BEETROOT);
        reg(Material.PUMPKIN,      "pumpkin",   1.0); DEF_OUT.put("pumpkin",   Material.PUMPKIN);

        reg(Material.MELON_SLICE,  "melon",     1.0);
        reg(Material.MELON,        "melon",     9.0);   // melon block = 9 slices
        DEF_OUT.put("melon", Material.MELON_SLICE);

        reg(Material.SUGAR_CANE,   "sugar_cane", 1.0);
        reg(Material.PAPER,        "sugar_cane", 1.0);  // 1 paper = 1 sugar cane
        DEF_OUT.put("sugar_cane", Material.SUGAR_CANE);

        reg(Material.BAMBOO,       "bamboo",    1.0); DEF_OUT.put("bamboo",    Material.BAMBOO);
        reg(Material.CACTUS,       "cactus",    1.0); DEF_OUT.put("cactus",    Material.CACTUS);
        reg(Material.COCOA_BEANS,  "cocoa_bean",1.0); DEF_OUT.put("cocoa_bean",Material.COCOA_BEANS);

        // ── Mob drops ─────────────────────────────────────────────────────────
        for (Material m : new Material[]{
                Material.ROTTEN_FLESH, Material.BONE, Material.STRING,
                Material.GUNPOWDER, Material.SPIDER_EYE, Material.ENDER_PEARL,
                Material.SLIME_BALL, Material.LEATHER, Material.ARROW
        }) { reg(m, m.name().toLowerCase(), 1.0); DEF_OUT.put(m.name().toLowerCase(), m); }

        // ── Mob rare ──────────────────────────────────────────────────────────
        for (Material m : new Material[]{
                Material.BLAZE_ROD, Material.GHAST_TEAR, Material.MAGMA_CREAM,
                Material.SHULKER_SHELL, Material.TOTEM_OF_UNDYING,
                Material.WITHER_SKELETON_SKULL, Material.NETHER_STAR
        }) { reg(m, m.name().toLowerCase(), 1.0); DEF_OUT.put(m.name().toLowerCase(), m); }

        // ── Nether ────────────────────────────────────────────────────────────
        reg(Material.NETHERRACK,     "netherrack",       1.0); DEF_OUT.put("netherrack",      Material.NETHERRACK);
        reg(Material.SOUL_SAND,      "soul_sand",        1.0); DEF_OUT.put("soul_sand",       Material.SOUL_SAND);
        reg(Material.SOUL_SOIL,      "soul_soil",        1.0); DEF_OUT.put("soul_soil",       Material.SOUL_SOIL);
        reg(Material.NETHER_BRICKS,  "nether_brick_block", 1.0);
        reg(Material.NETHER_BRICK,   "nether_brick_block", 0.25); // item = 1/4 block
        DEF_OUT.put("nether_brick_block", Material.NETHER_BRICKS);
        reg(Material.QUARTZ,         "quartz",           1.0); DEF_OUT.put("quartz",          Material.QUARTZ);
        reg(Material.GLOWSTONE,      "glowstone",        1.0); // base = block
        reg(Material.GLOWSTONE_DUST, "glowstone",        0.25);// 4 dust = 1 block
        DEF_OUT.put("glowstone", Material.GLOWSTONE);
        reg(Material.NETHER_WART,    "nether_wart",      1.0); DEF_OUT.put("nether_wart",     Material.NETHER_WART);

        // ── End ───────────────────────────────────────────────────────────────
        reg(Material.END_STONE,          "end_stone",    1.0); DEF_OUT.put("end_stone",    Material.END_STONE);
        reg(Material.CHORUS_FRUIT,       "chorus_fruit", 1.0); DEF_OUT.put("chorus_fruit", Material.CHORUS_FRUIT);
        reg(Material.POPPED_CHORUS_FRUIT,"popped_chorus",1.0); DEF_OUT.put("popped_chorus",Material.POPPED_CHORUS_FRUIT);
        reg(Material.DRAGON_BREATH,      "dragon_breath",1.0); DEF_OUT.put("dragon_breath",Material.DRAGON_BREATH);

        // ── Utility blocks ────────────────────────────────────────────────────
        reg(Material.SAND,             "sand",    1.0);
        reg(Material.RED_SAND,         "sand",    1.0);  // same commodity
        DEF_OUT.put("sand", Material.SAND);
        reg(Material.GRAVEL,           "gravel",  1.0); DEF_OUT.put("gravel",  Material.GRAVEL);
        reg(Material.CLAY,             "clay",    4.0); // clay block = 4 balls
        reg(Material.CLAY_BALL,        "clay",    1.0);
        reg(Material.BRICK,            "clay",    1.0); // smelted clay ball = 1 brick
        reg(Material.BRICKS,           "clay",    4.0); // brick block = 4 bricks
        DEF_OUT.put("clay", Material.CLAY_BALL);
        reg(Material.GLASS,            "glass",   1.0); DEF_OUT.put("glass",   Material.GLASS);
        reg(Material.OBSIDIAN,         "obsidian",1.0); DEF_OUT.put("obsidian",Material.OBSIDIAN);
        reg(Material.ICE,              "ice",     1.0);
        reg(Material.PACKED_ICE,       "ice",     9.0);
        reg(Material.BLUE_ICE,         "ice",    81.0);
        DEF_OUT.put("ice", Material.ICE);

        // ── Colorables – wool ─────────────────────────────────────────────────
        Map<String, Material> woolV = new LinkedHashMap<>();
        woolV.put("white",      Material.WHITE_WOOL);       woolV.put("orange",     Material.ORANGE_WOOL);
        woolV.put("magenta",    Material.MAGENTA_WOOL);     woolV.put("light_blue", Material.LIGHT_BLUE_WOOL);
        woolV.put("yellow",     Material.YELLOW_WOOL);      woolV.put("lime",       Material.LIME_WOOL);
        woolV.put("pink",       Material.PINK_WOOL);        woolV.put("gray",       Material.GRAY_WOOL);
        woolV.put("light_gray", Material.LIGHT_GRAY_WOOL);  woolV.put("cyan",       Material.CYAN_WOOL);
        woolV.put("purple",     Material.PURPLE_WOOL);      woolV.put("blue",       Material.BLUE_WOOL);
        woolV.put("brown",      Material.BROWN_WOOL);       woolV.put("green",      Material.GREEN_WOOL);
        woolV.put("red",        Material.RED_WOOL);         woolV.put("black",      Material.BLACK_WOOL);
        for (Material m : woolV.values()) reg(m, "wool", 1.0);
        VARIANTS.put("wool", woolV);
        DEF_OUT.put("wool", Material.WHITE_WOOL);

        // ── Colorables – concrete powder ──────────────────────────────────────
        Map<String, Material> cpV = new LinkedHashMap<>();
        cpV.put("white",      Material.WHITE_CONCRETE_POWDER);   cpV.put("orange",     Material.ORANGE_CONCRETE_POWDER);
        cpV.put("magenta",    Material.MAGENTA_CONCRETE_POWDER); cpV.put("light_blue", Material.LIGHT_BLUE_CONCRETE_POWDER);
        cpV.put("yellow",     Material.YELLOW_CONCRETE_POWDER);  cpV.put("lime",       Material.LIME_CONCRETE_POWDER);
        cpV.put("pink",       Material.PINK_CONCRETE_POWDER);    cpV.put("gray",       Material.GRAY_CONCRETE_POWDER);
        cpV.put("light_gray", Material.LIGHT_GRAY_CONCRETE_POWDER); cpV.put("cyan",    Material.CYAN_CONCRETE_POWDER);
        cpV.put("purple",     Material.PURPLE_CONCRETE_POWDER);  cpV.put("blue",       Material.BLUE_CONCRETE_POWDER);
        cpV.put("brown",      Material.BROWN_CONCRETE_POWDER);   cpV.put("green",      Material.GREEN_CONCRETE_POWDER);
        cpV.put("red",        Material.RED_CONCRETE_POWDER);     cpV.put("black",      Material.BLACK_CONCRETE_POWDER);
        for (Material m : cpV.values()) reg(m, "concrete_powder", 1.0);
        VARIANTS.put("concrete_powder", cpV);
        DEF_OUT.put("concrete_powder", Material.WHITE_CONCRETE_POWDER);

        // ── Colorables – concrete ─────────────────────────────────────────────
        Map<String, Material> conV = new LinkedHashMap<>();
        conV.put("white",      Material.WHITE_CONCRETE);   conV.put("orange",     Material.ORANGE_CONCRETE);
        conV.put("magenta",    Material.MAGENTA_CONCRETE); conV.put("light_blue", Material.LIGHT_BLUE_CONCRETE);
        conV.put("yellow",     Material.YELLOW_CONCRETE);  conV.put("lime",       Material.LIME_CONCRETE);
        conV.put("pink",       Material.PINK_CONCRETE);    conV.put("gray",       Material.GRAY_CONCRETE);
        conV.put("light_gray", Material.LIGHT_GRAY_CONCRETE); conV.put("cyan",    Material.CYAN_CONCRETE);
        conV.put("purple",     Material.PURPLE_CONCRETE);  conV.put("blue",       Material.BLUE_CONCRETE);
        conV.put("brown",      Material.BROWN_CONCRETE);   conV.put("green",      Material.GREEN_CONCRETE);
        conV.put("red",        Material.RED_CONCRETE);     conV.put("black",      Material.BLACK_CONCRETE);
        for (Material m : conV.values()) reg(m, "concrete", 1.0);
        VARIANTS.put("concrete", conV);
        DEF_OUT.put("concrete", Material.WHITE_CONCRETE);

        // ── Dyes (each color is its own commodity) ────────────────────────────
        for (Material m : new Material[]{
                Material.WHITE_DYE, Material.ORANGE_DYE, Material.MAGENTA_DYE, Material.LIGHT_BLUE_DYE,
                Material.YELLOW_DYE, Material.LIME_DYE, Material.PINK_DYE, Material.GRAY_DYE,
                Material.LIGHT_GRAY_DYE, Material.CYAN_DYE, Material.PURPLE_DYE, Material.BLUE_DYE,
                Material.BROWN_DYE, Material.GREEN_DYE, Material.RED_DYE, Material.BLACK_DYE
        }) { reg(m, m.name().toLowerCase(), 1.0); DEF_OUT.put(m.name().toLowerCase(), m); }

        // ── Misc ──────────────────────────────────────────────────────────────
        reg(Material.FEATHER,      "feather",     1.0); DEF_OUT.put("feather",     Material.FEATHER);
        reg(Material.INK_SAC,      "ink_sac",     1.0); DEF_OUT.put("ink_sac",     Material.INK_SAC);
        reg(Material.GLOW_INK_SAC, "glow_ink_sac",1.0); DEF_OUT.put("glow_ink_sac",Material.GLOW_INK_SAC);

        // ── Basic blocks ──────────────────────────────────────────────────────
        reg(Material.DIRT,         "dirt",        1.0); DEF_OUT.put("dirt",        Material.DIRT);

        // ── Aliases (shorthand → canonical Material) ──────────────────────────
        ALIASES.put("coal",            Material.COAL_BLOCK);
        ALIASES.put("coal_block",      Material.COAL_BLOCK);
        ALIASES.put("iron",            Material.IRON_BLOCK);
        ALIASES.put("iron_block",      Material.IRON_BLOCK);
        ALIASES.put("iron_ingot",      Material.IRON_INGOT);
        ALIASES.put("gold",            Material.GOLD_BLOCK);
        ALIASES.put("gold_block",      Material.GOLD_BLOCK);
        ALIASES.put("gold_ingot",      Material.GOLD_INGOT);
        ALIASES.put("copper",          Material.COPPER_INGOT);
        ALIASES.put("copper_ingot",    Material.COPPER_INGOT);
        ALIASES.put("emerald",         Material.EMERALD_BLOCK);
        ALIASES.put("emerald_block",   Material.EMERALD_BLOCK);
        ALIASES.put("lapis",           Material.LAPIS_LAZULI);
        ALIASES.put("lapis_lazuli",    Material.LAPIS_LAZULI);
        ALIASES.put("netherite",       Material.NETHERITE_INGOT);
        ALIASES.put("scrap",           Material.NETHERITE_SCRAP);
        ALIASES.put("log",             Material.OAK_LOG);
        ALIASES.put("wood",            Material.OAK_LOG);
        ALIASES.put("stem",            Material.CRIMSON_STEM);
        ALIASES.put("nether_log",      Material.CRIMSON_STEM);
        ALIASES.put("sugarcane",       Material.SUGAR_CANE);
        ALIASES.put("cocoa",           Material.COCOA_BEANS);
        ALIASES.put("flesh",           Material.ROTTEN_FLESH);
        ALIASES.put("slime",           Material.SLIME_BALL);
        ALIASES.put("pearl",           Material.ENDER_PEARL);
        ALIASES.put("totem",           Material.TOTEM_OF_UNDYING);
        ALIASES.put("quartz",          Material.QUARTZ);
        ALIASES.put("glow_dust",       Material.GLOWSTONE_DUST);
        ALIASES.put("nether_brick",    Material.NETHER_BRICKS);
        ALIASES.put("melon_slice",     Material.MELON_SLICE);
        ALIASES.put("chorus",          Material.CHORUS_FRUIT);
        ALIASES.put("popped_chorus",   Material.POPPED_CHORUS_FRUIT);
        ALIASES.put("glow_ink",        Material.GLOW_INK_SAC);
        ALIASES.put("obsidian",        Material.OBSIDIAN);
        ALIASES.put("redstone",        Material.REDSTONE);
        ALIASES.put("glowstone",       Material.GLOWSTONE);
        ALIASES.put("dirt",            Material.DIRT);
    }

    // ─── State ───────────────────────────────────────────────────────────────
    private final Map<UUID, Long> lastCodeRequest = new HashMap<>();
    private final Map<UUID, Long> lastActive = new HashMap<>();
    private final Map<UUID, Long> lastAliveReport = new HashMap<>();

    @Override
    public void onEnable() {
        getServer().getPluginManager().registerEvents(this, this);
        getServer().getScheduler().runTaskTimer(this, this::reportAlivePlayers, 20L * 60L, 20L * 60L);
        getLogger().info("MDragonsEconomy loaded. Backend: " + BACKEND_URL + (API_KEY.isEmpty() ? " (no API key)" : " (API key configured)"));
        if (API_KEY.isEmpty()) {
            getLogger().severe("API_KEY is not set. Backend requests will be rejected by a secured backend.");
        }
    }

    @EventHandler
    public void onPlayerJoin(PlayerJoinEvent event) {
        Player player = event.getPlayer();
        long now = System.currentTimeMillis();
        lastActive.put(player.getUniqueId(), now);
        lastAliveReport.put(player.getUniqueId(), now);
        getServer().getScheduler().runTaskAsynchronously(this, () -> {
            String json = loginRewardPost(player.getUniqueId());
            if (json == null) return;
            int amount = parseJsonInt(json, "amount");
            if (amount <= 0) return;
            getServer().getScheduler().runTask(this, () ->
                    player.sendMessage("§aWeekly linked-login reward: §e🐉 " + amount + "§a added to your vault."));
        });
    }

    // ─── Command dispatcher ───────────────────────────────────────────────────
    @EventHandler
    public void onPlayerMove(PlayerMoveEvent event) {
        if (event.getTo() != null) {
            lastActive.put(event.getPlayer().getUniqueId(), System.currentTimeMillis());
        }
    }

    @EventHandler
    public void onPlayerInteract(PlayerInteractEvent event) {
        lastActive.put(event.getPlayer().getUniqueId(), System.currentTimeMillis());
    }

    @EventHandler
    public void onPlayerDeath(PlayerDeathEvent event) {
        Player victim = event.getEntity();
        Player killer = victim.getKiller();
        UUID victimId = victim.getUniqueId();
        String victimName = victim.getName();
        UUID killerId = killer != null ? killer.getUniqueId() : null;
        String killerName = killer != null ? killer.getName() : null;
        lastAliveReport.put(victimId, System.currentTimeMillis());

        getServer().getScheduler().runTaskAsynchronously(this, () -> {
            aliveDeathPost(victimId, victimName);
            if (killerId == null || killerId.equals(victimId)) return;
            String json = bountyClaimPost(victimId, killerId, victimName, killerName);
            if (json == null) return;
            int amount = parseJsonInt(json, "amount");
            if (amount <= 0) return;
            getServer().getScheduler().runTask(this, () ->
                    Bukkit.broadcastMessage("§6§lBounty claimed! §e" + killerName + " §7earned §6🐉 "
                            + amount + " §7for killing §e" + victimName + "§7."));
        });
    }

    @Override
    public boolean onCommand(CommandSender sender, Command cmd, String label, String[] args) {
        if (!(sender instanceof Player player)) {
            sender.sendMessage("§cOnly players can use this command!");
            return true;
        }

        switch (cmd.getName().toLowerCase()) {
            case "discord" -> { handleDiscordLink(player); return true; }
            case "deposit" -> {
                if (player.getGameMode() == GameMode.CREATIVE) {
                    player.sendMessage("§cDepositing is disabled in Creative mode.");
                    return true;
                }
                if (args.length == 1 && args[0].equalsIgnoreCase("inv")) {
                    handleDepositInventory(player);
                    return true;
                }
                if (args.length < 2) {
                    player.sendMessage("§cUsage: §e/deposit <item> <amount|all> §7or §e/deposit inv");
                    player.sendMessage("§7Examples: §f/deposit iron all  §7| §f/deposit overworld_log 32 oak  §7| §f/deposit xp all");
                    return true;
                }
                handleDeposit(player, args);
                return true;
            }
            case "withdraw" -> {
                if (player.getGameMode() == GameMode.CREATIVE) {
                    player.sendMessage("§cWithdrawing is disabled in Creative mode.");
                    return true;
                }
                if (args.length < 2) {
                    player.sendMessage("§cUsage: §e/withdraw <item> <amount> [variant]");
                    player.sendMessage("§7Examples: §f/withdraw iron 64  §7| §f/withdraw overworld_log 10 oak  §7| §f/withdraw xp 500");
                    return true;
                }
                handleWithdraw(player, args);
                return true;
            }
            case "balance" -> {
                if (args.length >= 1) handleItemGui(player, args[0]);
                else handleBalance(player);
                return true;
            }
            case "itemgui" -> {
                if (args.length < 1) {
                    player.sendMessage("§cUsage: §e/itemgui <item>");
                    return true;
                }
                handleItemGui(player, args[0]);
                return true;
            }
            case "alive" -> {
                Integer page = parsePageArg(player, args);
                if (page == null) return true;
                handleAliveLeaderboard(player, page);
                return true;
            }
            case "bounties" -> {
                Integer page = parsePageArg(player, args);
                if (page == null) return true;
                handleBounties(player, page);
                return true;
            }
            case "bounty", "bouny" -> {
                if (args.length < 2) {
                    player.sendMessage("§cUsage: §e/bounty <playername> <dragons>");
                    return true;
                }
                handleBounty(player, args[0], args[1]);
                return true;
            }
            case "helpmc"  -> { handleHelpMC(player, args); return true; }
            default -> { return false; }
        }
    }

    // ─── Material resolver ────────────────────────────────────────────────────
    private Material resolveMaterial(String input) {
        String normalized = input.toLowerCase().replace("-", "_");
        Material m = ALIASES.get(normalized);
        if (m != null) return m;
        m = Material.matchMaterial(normalized.toUpperCase());
        if (m != null && REG.containsKey(m)) return m;
        return null;
    }

    // ─── /deposit ─────────────────────────────────────────────────────────────
    private void reportAlivePlayers() {
        long now = System.currentTimeMillis();
        for (Player player : getServer().getOnlinePlayers()) {
            if (player.isDead() || player.getGameMode() == GameMode.CREATIVE || player.getGameMode() == GameMode.SPECTATOR) {
                lastAliveReport.put(player.getUniqueId(), now);
                continue;
            }

            UUID uuid = player.getUniqueId();
            long activeAt = lastActive.getOrDefault(uuid, now);
            long previousReport = lastAliveReport.getOrDefault(uuid, now);
            if (now - activeAt > AFK_TIMEOUT_MS) {
                lastAliveReport.put(uuid, now);
                continue;
            }

            double seconds = (now - previousReport) / 1000.0;
            if (seconds < 5.0) continue;
            lastAliveReport.put(uuid, now);
            String name = player.getName();
            getServer().getScheduler().runTaskAsynchronously(this, () ->
                    aliveReportPost(uuid, name, seconds));
        }
    }

    private void handleDeposit(Player player, String[] args) {
        String itemKey = args[0].toLowerCase().replace("-", "_");
        if (itemKey.equals("xp") || itemKey.equals("experience")) {
            handleDepositXp(player, args[1]);
            return;
        }

        Material mat;
        if (VARIANTS.containsKey(itemKey)) {
            Map<String, Material> variantMap = VARIANTS.get(itemKey);
            if (args.length < 3) {
                player.sendMessage("§cSpecify a variant. Options: §e" + String.join(", ", variantMap.keySet()));
                return;
            }
            String variantArg = args[2].toLowerCase().replace("-", "_");
            mat = variantMap.get(variantArg);
            if (mat == null) {
                player.sendMessage("§cInvalid variant §e" + variantArg + "§c. Options: §e" + String.join(", ", variantMap.keySet()));
                return;
            }
        } else {
            mat = resolveMaterial(args[0]);
        }
        if (mat == null || !REG.containsKey(mat)) {
            player.sendMessage("§cUnknown item: §e" + args[0]);
            player.sendMessage("§7Use Minecraft item names like §firon_ingot§7, §foak_log§7, §fred_wool§7, or variants like §fwool 16 red§7.");
            return;
        }

        int amount;
        if (args[1].equalsIgnoreCase("all")) {
            amount = countItem(player, mat);
            if (amount <= 0) {
                player.sendMessage("§cYou do not have any §e" + fmtMat(mat) + "§c to deposit.");
                return;
            }
        } else {
            try {
                amount = Integer.parseInt(args[1]);
                if (amount <= 0) throw new NumberFormatException();
            } catch (NumberFormatException e) {
                player.sendMessage("§cAmount must be a positive integer or §eall§c.");
                return;
            }
        }

        depositResolved(player, mat, amount);
    }

    private void depositResolved(Player player, Material mat, int amount) {
        CEntry entry = REG.get(mat);
        if (entry == null) {
            player.sendMessage("§cInternal error resolving deposit item.");
            return;
        }

        int held = countItem(player, mat);
        if (held < amount) {
            player.sendMessage("§cNot enough §e" + fmtMat(mat) + "§c. Have §e" + held + "§c, need §e" + amount + "§c.");
            return;
        }

        boolean isLegacy = entry.commodity().equals("diamond") || entry.commodity().equals("netherite");
        if (isLegacy) depositLegacy(player, mat, amount, entry);
        else depositCommodity(player, mat, amount, entry);
    }

    private void handleDepositInventory(Player player) {
        Map<Material, Integer> totals = new LinkedHashMap<>();
        for (ItemStack stack : player.getInventory().getContents()) {
            if (stack == null || stack.getAmount() <= 0) continue;
            Material mat = stack.getType();
            if (!REG.containsKey(mat)) continue;
            totals.put(mat, totals.getOrDefault(mat, 0) + stack.getAmount());
        }

        if (totals.isEmpty()) {
            player.sendMessage("§cNo depositable items found in your inventory.");
            return;
        }

        player.sendMessage("§7Depositing every supported item in your inventory...");
        for (Map.Entry<Material, Integer> e : totals.entrySet()) {
            depositResolved(player, e.getKey(), e.getValue());
        }
    }

    private void handleDepositXp(Player player, String amountArg) {
        int available = getCurrentExperience(player);
        int amount;
        if (amountArg.equalsIgnoreCase("all")) {
            amount = available;
        } else {
            try {
                amount = Integer.parseInt(amountArg);
                if (amount <= 0) throw new NumberFormatException();
            } catch (NumberFormatException e) {
                player.sendMessage("§cXP amount must be a positive integer or §eall§c.");
                return;
            }
        }
        if (amount <= 0 || available < amount) {
            player.sendMessage("§cNot enough XP. Have §e" + available + "§c, need §e" + amount + "§c.");
            return;
        }

        setTotalExperience(player, available - amount);
        getServer().getScheduler().runTaskAsynchronously(this, () -> {
            boolean ok = commodityPost("deposit", player.getUniqueId(), "xp", amount);
            if (ok) logDepositWithdraw(player.getUniqueId(), "deposit", "xp", amount, amount);
            getServer().getScheduler().runTask(this, () -> {
                if (ok) {
                    player.sendMessage("§aDeposited §e" + amount + "§a XP.");
                } else {
                    player.sendMessage("§cXP deposit failed — returning XP.");
                    player.giveExp(amount);
                }
            });
        });
    }

    private void depositLegacy(Player player, Material mat, int amount, CEntry entry) {
        // Special: netherite scrap converts 4:1 to ingots
        if (mat == Material.NETHERITE_SCRAP) {
            int ingots = amount / 4;
            int remainder = amount % 4;
            if (ingots == 0) {
                player.sendMessage("§cYou need at least §e4 scraps§c to deposit (you have §e" + amount + "§c). They convert at 4:1.");
                return;
            }
            int toTake = ingots * 4;
            removeItems(player, mat, toTake);
            if (remainder > 0)
                player.sendMessage("§e" + remainder + "§7 scrap(s) returned — need 4 per ingot.");
            int finalIngots = ingots;
            getServer().getScheduler().runTaskAsynchronously(this, () -> {
                boolean ok = legacyPost("deposit", player.getUniqueId(), "NETHERITE_INGOT", finalIngots);
                if (ok) logDepositWithdraw(player.getUniqueId(), "deposit", "NETHERITE_INGOT", finalIngots, finalIngots);
                getServer().getScheduler().runTask(this, () -> {
                    if (ok) {
                        player.sendMessage("§aDeposited §e" + finalIngots + "§a netherite ingot(s) (converted from §e" + toTake + "§a scraps).");
                    } else {
                        player.sendMessage("§cDeposit failed — returning scraps.");
                        player.getInventory().addItem(new ItemStack(mat, toTake));
                    }
                });
            });
            return;
        }

        // Diamond or netherite ingot (including diamond block → 9 per block)
        int baseAmount = (int)(amount * entry.ratio());
        if (baseAmount <= 0) {
            player.sendMessage("§cAmount rounds to zero base units. Try a larger quantity.");
            return;
        }
        removeItems(player, mat, amount);
        String legacyItem = entry.commodity().toUpperCase();
        getServer().getScheduler().runTaskAsynchronously(this, () -> {
            boolean ok = legacyPost("deposit", player.getUniqueId(), legacyItem, baseAmount);
            if (ok) logDepositWithdraw(player.getUniqueId(), "deposit", legacyItem, amount, baseAmount);
            getServer().getScheduler().runTask(this, () -> {
                if (ok) {
                    player.sendMessage("§aDeposited §e" + amount + "§a × " + fmtMat(mat) + "§a.");
                } else {
                    player.sendMessage("§cDeposit failed — returning items.");
                    player.getInventory().addItem(new ItemStack(mat, amount));
                }
            });
        });
    }

    private void depositCommodity(Player player, Material mat, int amount, CEntry entry) {
        double baseUnits = amount * entry.ratio();
        removeItems(player, mat, amount);
        getServer().getScheduler().runTaskAsynchronously(this, () -> {
            boolean ok = commodityPost("deposit", player.getUniqueId(), entry.commodity(), baseUnits);
            if (ok) logDepositWithdraw(player.getUniqueId(), "deposit", entry.commodity(), amount, baseUnits);
            getServer().getScheduler().runTask(this, () -> {
                if (ok) {
                    player.sendMessage("§aDeposited §e" + amount + "§a × " + fmtMat(mat)
                            + "§7 (§e+" + fmtNum(baseUnits) + "§7 " + entry.commodity() + ").");
                } else {
                    player.sendMessage("§cDeposit failed — returning items.");
                    player.getInventory().addItem(new ItemStack(mat, amount));
                }
            });
        });
    }

    // ─── /withdraw ────────────────────────────────────────────────────────────
    private void handleWithdraw(Player player, String[] args) {
        int amount;
        try {
            amount = Integer.parseInt(args[1]);
            if (amount <= 0) throw new NumberFormatException();
        } catch (NumberFormatException e) {
            player.sendMessage("§cAmount must be a positive integer.");
            return;
        }
        String variantArg = args.length >= 3 ? args[2].toLowerCase().replace("-", "_") : null;
        String inputItem  = args[0].toLowerCase().replace("-", "_");

        if (inputItem.equals("xp") || inputItem.equals("experience")) {
            executeWithdrawXp(player, amount);
            return;
        }

        // ── Path A: direct material name (e.g. iron_ingot, oak_log, red_wool) ─
        Material mat = resolveMaterial(inputItem);
        if (mat != null && REG.containsKey(mat)) {
            CEntry entry = REG.get(mat);
            executeWithdraw(player, mat, amount, entry);
            return;
        }

        // ── Path B: commodity key with variant (e.g. "overworld_log oak", "wool red") ─
        if (DEF_OUT.containsKey(inputItem)) {
            Material targetMat;
            if (VARIANTS.containsKey(inputItem)) {
                Map<String, Material> variantMap = VARIANTS.get(inputItem);
                if (variantArg == null) {
                    player.sendMessage("§cSpecify a variant. Options: §e" + String.join(", ", variantMap.keySet()));
                    return;
                }
                targetMat = variantMap.get(variantArg);
                if (targetMat == null) {
                    player.sendMessage("§cInvalid variant §e" + variantArg + "§c. Options: §e" + String.join(", ", variantMap.keySet()));
                    return;
                }
            } else {
                targetMat = DEF_OUT.get(inputItem);
            }
            CEntry entry = REG.get(targetMat);
            if (entry == null) { player.sendMessage("§cInternal error resolving commodity."); return; }
            executeWithdraw(player, targetMat, amount, entry);
            return;
        }

        player.sendMessage("§cUnknown item: §e" + inputItem);
        player.sendMessage("§7Use Minecraft item names (§firon_ingot§7, §fred_wool§7) or commodity names with a variant (§foverworld_log oak§7, §fwool red§7).");
    }

    private void executeWithdraw(Player player, Material mat, int amount, CEntry entry) {
        boolean isLegacy = entry.commodity().equals("diamond") || entry.commodity().equals("netherite");
        double baseNeeded = amount * entry.ratio();

        if (isLegacy) {
            int baseInt = (int) baseNeeded;
            if (baseInt <= 0) { player.sendMessage("§cAmount too small to withdraw."); return; }
            getServer().getScheduler().runTaskAsynchronously(this, () -> {
                boolean ok = legacyPost("withdraw", player.getUniqueId(), entry.commodity().toUpperCase(), baseInt);
                if (ok) logDepositWithdraw(player.getUniqueId(), "withdraw", entry.commodity().toUpperCase(), amount, baseInt);
                getServer().getScheduler().runTask(this, () -> {
                    if (ok) {
                        giveItems(player, mat, amount);
                        player.sendMessage("§aWithdrew §e" + amount + "§a × " + fmtMat(mat) + "§a.");
                    } else {
                        player.sendMessage("§cWithdrawal failed. Insufficient §e" + entry.commodity() + "§c balance.");
                    }
                });
            });
        } else {
            getServer().getScheduler().runTaskAsynchronously(this, () -> {
                boolean ok = commodityPost("withdraw", player.getUniqueId(), entry.commodity(), baseNeeded);
                if (ok) logDepositWithdraw(player.getUniqueId(), "withdraw", entry.commodity(), amount, baseNeeded);
                getServer().getScheduler().runTask(this, () -> {
                    if (ok) {
                        giveItems(player, mat, amount);
                        player.sendMessage("§aWithdrew §e" + amount + "§a × " + fmtMat(mat) + "§a.");
                    } else {
                        player.sendMessage("§cWithdrawal failed. Insufficient §e" + entry.commodity() + "§c balance.");
                    }
                });
            });
        }
    }

    private void executeWithdrawXp(Player player, int amount) {
        getServer().getScheduler().runTaskAsynchronously(this, () -> {
            boolean ok = commodityPost("withdraw", player.getUniqueId(), "xp", amount);
            if (ok) logDepositWithdraw(player.getUniqueId(), "withdraw", "xp", amount, amount);
            getServer().getScheduler().runTask(this, () -> {
                if (ok) {
                    player.giveExp(amount);
                    player.sendMessage("§aWithdrew §e" + amount + "§a XP.");
                } else {
                    player.sendMessage("§cXP withdrawal failed. Insufficient xp balance.");
                }
            });
        });
    }

    // ─── /balance ─────────────────────────────────────────────────────────────
    private void handleBalance(Player player) {
        player.sendMessage("§7Fetching vault balance...");
        getServer().getScheduler().runTaskAsynchronously(this, () -> {
            String legacyJson    = httpGet(BACKEND_URL + "/balance/" + player.getUniqueId());
            String commodityJson = httpGet(BACKEND_URL + "/commodity/balance/" + player.getUniqueId());
            getServer().getScheduler().runTask(this, () -> showBalance(player, legacyJson, commodityJson));
        });
    }

    private void showBalance(Player player, String legacyJson, String commodityJson) {
        player.sendMessage("§6§l══════ Vault Balance ══════");

        boolean hasAny = false;

        if (legacyJson != null) {
            int    diamond  = parseJsonInt(legacyJson, "diamond");
            int    neth     = parseJsonInt(legacyJson, "netherite");
            long   vault    = (long) parseJsonDouble(legacyJson, "mdragons");
            long   locked   = (long) parseJsonDouble(legacyJson, "mdragons_locked");
            long   total    = (long) parseJsonDouble(legacyJson, "mdragons_total");

            if (diamond > 0 || neth > 0 || vault > 0 || locked > 0) {
                player.sendMessage("§e§lTrade Items:");
                if (diamond > 0) player.sendMessage("  §7Diamond:   §f" + diamond);
                if (neth    > 0) player.sendMessage("  §7Netherite: §f" + neth);
                if (vault   > 0 || locked > 0) {
                    player.sendMessage("  §7🐉 Vault:   §f" + vault);
                    if (locked > 0) player.sendMessage("  §7🐉 Locked:  §f" + locked + " §8(in orders)");
                    player.sendMessage("  §7🐉 Total:   §f" + total);
                }
                hasAny = true;
            }
        }

        if (commodityJson != null) {
            Map<String, Double> balances = parseCommodityMap(commodityJson);
            if (!balances.isEmpty()) {
                player.sendMessage("§e§lCommodities:");
                for (Map.Entry<String, Double> e : balances.entrySet()) {
                    if (e.getValue() > 0.0001)
                        player.sendMessage("  §7" + fmtKey(e.getKey()) + ": §f" + fmtNum(e.getValue()));
                }
                hasAny = true;
            }
        }

        if (!hasAny) player.sendMessage("§7Your vault is empty.");
        player.sendMessage("§6§l═══════════════════════════");
    }

    // ─── Discord link ─────────────────────────────────────────────────────────
    private void handleItemGui(Player player, String input) {
        String itemKey = resolveBackendItemKey(input);
        if (itemKey == null) {
            player.sendMessage("§cUnknown item: §e" + input);
            return;
        }

        player.sendMessage("§7Fetching " + displayKey(itemKey) + " balance...");
        getServer().getScheduler().runTaskAsynchronously(this, () -> {
            String json = httpGet(BACKEND_URL + "/inventory/" + player.getUniqueId() + "/" + itemKey);
            getServer().getScheduler().runTask(this, () -> {
                if (json == null) {
                    player.sendMessage("§cCould not fetch item balance.");
                    return;
                }
                double vault = parseJsonDouble(json, "vault");
                double inOrders = parseJsonDouble(json, "in_orders");
                double total = parseJsonDouble(json, "total");
                openItemBalanceGui(player, itemKey, vault, inOrders, total);
            });
        });
    }

    private void openItemBalanceGui(Player player, String itemKey, double vault, double inOrders, double total) {
        Inventory inv = Bukkit.createInventory(null, 9, "Vault: " + displayKey(itemKey));
        ItemStack icon = new ItemStack(iconForItemKey(itemKey));
        ItemMeta meta = icon.getItemMeta();
        if (meta != null) {
            meta.setDisplayName("§6§l" + displayKey(itemKey));
            meta.setLore(Arrays.asList(
                    "§7Available: §f" + fmtNum(vault),
                    "§7In market: §f" + fmtNum(inOrders),
                    "§7Total: §f" + fmtNum(total)
            ));
            icon.setItemMeta(meta);
        }
        inv.setItem(4, icon);
        player.openInventory(inv);
    }

    private String resolveBackendItemKey(String input) {
        String key = input.toLowerCase().replace("-", "_");
        if (key.equals("xp") || key.equals("experience")) return "xp";
        if (key.equals("dragon") || key.equals("dragons") || key.equals("mdragons")) return "DAEMON";
        if (DEF_OUT.containsKey(key)) return key;
        Material mat = resolveMaterial(input);
        if (mat == null || !REG.containsKey(mat)) return null;
        return REG.get(mat).commodity();
    }

    private Material iconForItemKey(String itemKey) {
        if (itemKey.equals("xp")) return Material.EXPERIENCE_BOTTLE;
        if (itemKey.equals("DAEMON")) return Material.DRAGON_EGG;
        Material mat = DEF_OUT.get(itemKey);
        if (mat != null) return mat;
        return Material.PAPER;
    }

    private Integer parsePageArg(Player player, String[] args) {
        if (args.length < 1) return 1;
        try {
            return Math.max(1, Integer.parseInt(args[0]));
        } catch (NumberFormatException e) {
            player.sendMessage("§cPage must be a positive number.");
            return null;
        }
    }

    private void handleAliveLeaderboard(Player player, int page) {
        handleTextPage(player, page, "§7Fetching alive leaderboard...",
                "§6§lAlive Leaderboard", "§7No alive streaks yet.", "/alive/leaderboard_text");
    }

    private void handleBounties(Player player, int page) {
        handleTextPage(player, page, "§7Fetching active bounties...",
                "§6§lActive Bounties", "§7No active bounties.", "/bounties_text");
    }

    private void handleTextPage(Player player, int page, String loadingMessage, String title, String emptyMessage, String endpoint) {
        int offset = (page - 1) * 10;
        player.sendMessage(loadingMessage);
        getServer().getScheduler().runTaskAsynchronously(this, () -> {
            String text = httpGet(BACKEND_URL + endpoint + "?limit=10&offset=" + offset);
            getServer().getScheduler().runTask(this, () -> {
                player.sendMessage(title + " §7(Page " + page + ")");
                if (text == null || text.isBlank()) {
                    player.sendMessage(emptyMessage);
                    return;
                }
                for (String line : text.split("\\n")) {
                    player.sendMessage("§f" + line);
                }
            });
        });
    }

    private void handleBounty(Player player, String targetName, String amountArg) {
        int amount;
        try {
            amount = Integer.parseInt(amountArg);
            if (amount <= 0) throw new NumberFormatException();
        } catch (NumberFormatException e) {
            player.sendMessage("§cBounty amount must be a positive number of dragons.");
            return;
        }

        OfflinePlayer target = Bukkit.getOfflinePlayer(targetName);
        if (target.getUniqueId().equals(player.getUniqueId())) {
            player.sendMessage("§cYou cannot place a bounty on yourself.");
            return;
        }
        String cleanTargetName = target.getName() != null ? target.getName() : targetName;
        player.sendMessage("§7Placing bounty...");
        getServer().getScheduler().runTaskAsynchronously(this, () -> {
            String json = bountyPlacePost(player.getUniqueId(), target.getUniqueId(), cleanTargetName, amount);
            getServer().getScheduler().runTask(this, () -> {
                if (json == null) {
                    player.sendMessage("§cBounty failed. Check your dragon balance.");
                    return;
                }
                int total = parseJsonInt(json, "target_total");
                Bukkit.broadcastMessage("§6§lBounty placed! §e" + player.getName() + " §7put §6🐉 "
                        + amount + " §7on §e" + cleanTargetName + "§7. Total bounty: §6🐉 " + total);
            });
        });
    }

    private void handleDiscordLink(Player player) {
        UUID uuid = player.getUniqueId();
        long now  = System.currentTimeMillis();
        if (lastCodeRequest.getOrDefault(uuid, 0L) > now - 30_000L) {
            player.sendMessage("§cPlease wait 30 seconds before requesting another code.");
            return;
        }
        getServer().getScheduler().runTaskAsynchronously(this, () -> {
            String code = getLinkCode(uuid);
            getServer().getScheduler().runTask(this, () -> {
                if (code == null || code.length() != 6) {
                    player.sendMessage("§cFailed to generate code. Backend may be unreachable.");
                    return;
                }
                lastCodeRequest.put(uuid, now);
                player.sendMessage("§aYour Discord verification code is: §e§l" + code);
                player.sendMessage("§7In Discord type: §f/link " + code);
                player.sendMessage("§8Code expires in 15 minutes.");
            });
        });
    }

    // ─── /helpmc ──────────────────────────────────────────────────────────────
    private void handleHelpMC(Player player, String[] args) {
        if (args.length >= 1) {
            // /helpmc <category> — show items in that group
            String cat = args[0].toLowerCase();
            switch (cat) {
                case "ores" -> {
                    player.sendMessage("§6§lOres §7— deposit/withdraw by item or block:");
                    player.sendMessage("  §fcoal§7 → coal_block §8(base), coal §8(1/9)");
                    player.sendMessage("  §firon§7 → iron_block §8(base), iron_ingot §8(1/9)");
                    player.sendMessage("  §fgold§7 → gold_block §8(base), gold_ingot §8(1/9), gold_nugget §8(1/81)");
                    player.sendMessage("  §fcopper§7 → copper_ingot §8(base), copper_block §8(9 ingots)");
                    player.sendMessage("  §fdiamond§7 → diamond, diamond_block  §8(order-book item)");
                    player.sendMessage("  §femerald§7 → emerald_block §8(base), emerald §8(1/9)");
                    player.sendMessage("  §fredstone§7 → redstone §8(base), redstone_block §8(9 dust)");
                    player.sendMessage("  §flapis§7 → lapis_lazuli §8(base), lapis_block §8(9 lapis)");
                    player.sendMessage("  §fnetherite§7 → netherite_ingot, netherite_scrap §8(4 scraps = 1 ingot)");
                }
                case "wood" -> {
                    player.sendMessage("§6§lWood §7— use commodity name + variant:");
                    player.sendMessage("  §f/deposit overworld_log 32 oak");
                    player.sendMessage("  §7Variants: oak, spruce, birch, jungle, acacia, dark_oak, mangrove, cherry");
                    player.sendMessage("  §f/deposit nether_log 16 crimson");
                    player.sendMessage("  §7Variants: crimson, warped");
                }
                case "farming" -> {
                    player.sendMessage("§6§lFarming:");
                    player.sendMessage("  §fwheat§7 (wheat, bread — 3 wheat per bread)");
                    player.sendMessage("  §fcarrot§7, §fpotato§7, §fbeetroot§7, §fpumpkin");
                    player.sendMessage("  §fmelon§7 (melon_slice or melon block — 9 slices = 1 block)");
                    player.sendMessage("  §fsugar_cane §8(alias: sugarcane)§7, §fbamboo§7, §fcactus§7, §fcocoa_bean");
                }
                case "mob" -> {
                    player.sendMessage("§6§lMob Drops:");
                    player.sendMessage("  §frotten_flesh§7, §fbone§7, §fstring§7, §fgunpowder");
                    player.sendMessage("  §fspider_eye§7, §fender_pearl§7, §fslime_ball§7, §fleather§7, §farrow");
                    player.sendMessage("§6§lRare Drops:");
                    player.sendMessage("  §fblaze_rod§7, §fghast_tear§7, §fmagma_cream§7, §fshulker_shell");
                    player.sendMessage("  §ftotem_of_undying §8(alias: totem)");
                    player.sendMessage("  §fwither_skeleton_skull§7, §fnether_star");
                }
                case "blocks", "building" -> {
                    player.sendMessage("§6§lBuilding / Utility:");
                    player.sendMessage("  §fstone§7, §fcobblestone§7, §fdeepslate§7, §fblackstone§7, §fbasalt");
                    player.sendMessage("  §fsand§7 (regular + red), §fgravel§7, §fclay §8(clay_ball)§7, §fglass");
                    player.sendMessage("  §fobsidian§7, §fice §8(ice / packed_ice / blue_ice)§7, §fdirt");
                }
                case "nether" -> {
                    player.sendMessage("§6§lNether Items:");
                    player.sendMessage("  §fnetherrack§7, §fsoul_sand§7, §fsoul_soil");
                    player.sendMessage("  §fnether_brick_block §8(4 nether_bricks = 1 block)");
                    player.sendMessage("  §fquartz§7, §fglowstone §8(4 dust = 1 block)§7, §fnether_wart");
                }
                case "end" -> {
                    player.sendMessage("§6§lEnd Items:");
                    player.sendMessage("  §fend_stone§7, §fchorus_fruit §8(alias: chorus)");
                    player.sendMessage("  §fpopped_chorus §8(alias: popped_chorus)§7, §fdragon_breath");
                }
                case "color", "colours", "wool", "concrete" -> {
                    player.sendMessage("§6§lColorable Items §7— need a variant:");
                    player.sendMessage("  §f/deposit wool 16 red");
                    player.sendMessage("  §f/deposit concrete 8 light_blue");
                    player.sendMessage("  §f/deposit concrete_powder 32 white");
                    player.sendMessage("  §7Colors: white, orange, magenta, light_blue, yellow, lime,");
                    player.sendMessage("  §7        pink, gray, light_gray, cyan, purple, blue, brown,");
                    player.sendMessage("  §7        green, red, black");
                    player.sendMessage("§6§lDyes §7(each color separate):");
                    player.sendMessage("  §f/deposit white_dye 32  §8or any color_dye");
                }
                case "misc" -> {
                    player.sendMessage("§6§lMisc:");
                    player.sendMessage("  §fxp§7, §ffeather§7, §fink_sac§7, §fglow_ink_sac §8(alias: glow_ink)");
                }
                default -> {
                    player.sendMessage("§cUnknown category: §e" + cat);
                    player.sendMessage("§7Categories: ores, wood, farming, mob, blocks, nether, end, color, misc");
                }
            }
            return;
        }

        // No args → show overview
        player.sendMessage("§6§l══ MDragons Item Help ══");
        player.sendMessage("§eUsage: §f/helpmc <category>");
        player.sendMessage("§7Categories:");
        player.sendMessage("  §fores      §8— coal, iron, gold, diamond, netherite...");
        player.sendMessage("  §fwood      §8— overworld_log, nether_log (+ variant)");
        player.sendMessage("  §ffarming   §8— wheat, carrot, melon, sugar_cane...");
        player.sendMessage("  §fmob       §8— rotten_flesh, blaze_rod, nether_star...");
        player.sendMessage("  §fblocks    §8— stone, sand, obsidian, ice...");
        player.sendMessage("  §fnether    §8— netherrack, quartz, glowstone...");
        player.sendMessage("  §fend       §8— end_stone, chorus_fruit, dragon_breath");
        player.sendMessage("  §fcolor     §8— wool, concrete, concrete_powder, dyes");
        player.sendMessage("  §fmisc      §8— xp, feather, ink_sac, glow_ink_sac");
        player.sendMessage("§7Example: §f/helpmc wood  §7or §f/helpmc color");
        player.sendMessage("§8Tip: /balance <item> opens an item GUI; /alive shows survival streaks; /bounties shows targets");
        player.sendMessage("§8Tip: /deposit <item> all, /deposit inv, and /deposit xp all are supported");
        player.sendMessage("§6§l════════════════════════");
    }

    // ─── HTTP helpers ─────────────────────────────────────────────────────────
    private String getLinkCode(UUID uuid) {
        try {
            URL url = new URL(BACKEND_URL + "/link/generate?uuid=" + uuid);
            HttpURLConnection conn = openConn(url, "GET", false);
            if (conn.getResponseCode() == 200) {
                String json = readBody(conn);
                int s = json.indexOf("\"code\":\"") + 8;
                int e = json.indexOf("\"", s);
                if (e > s) return json.substring(s, e);
            }
        } catch (Exception e) { getLogger().severe("Link code failed: " + e.getMessage()); }
        return null;
    }

    private boolean legacyPost(String action, UUID uuid, String item, int amount) {
        try {
            URL url = new URL(BACKEND_URL + "/" + action);
            HttpURLConnection conn = openConn(url, "POST", true);
            writeBody(conn, "{\"uuid\":\"" + uuid + "\",\"item\":\"" + item + "\",\"amount\":" + amount + "}");
            return conn.getResponseCode() == 200;
        } catch (Exception e) { getLogger().severe("Legacy " + action + " failed: " + e.getMessage()); return false; }
    }

    private boolean commodityPost(String action, UUID uuid, String commodity, double amount) {
        try {
            URL url = new URL(BACKEND_URL + "/commodity/" + action);
            HttpURLConnection conn = openConn(url, "POST", true);
            writeBody(conn, "{\"mc_uuid\":\"" + uuid + "\",\"commodity\":\"" + commodity + "\",\"amount\":" + amount + "}");
            return conn.getResponseCode() == 200;
        } catch (Exception e) { getLogger().severe("Commodity " + action + " failed: " + e.getMessage()); return false; }
    }

    private void logDepositWithdraw(UUID uuid, String action, String item, double amount, double baseUnits) {
        try {
            URL url = new URL(BACKEND_URL + "/log/deposit_withdraw");
            HttpURLConnection conn = openConn(url, "POST", true);
            writeBody(conn, "{\"mc_uuid\":\"" + uuid + "\",\"action\":\"" + action
                    + "\",\"item\":\"" + item + "\",\"amount\":" + amount
                    + ",\"base_units\":" + baseUnits + "}");
            if (conn.getResponseCode() != 200) {
                getLogger().warning("Deposit/withdraw log failed with HTTP " + conn.getResponseCode());
            }
        } catch (Exception e) {
            getLogger().warning("Deposit/withdraw log failed: " + e.getMessage());
        }
    }

    private String loginRewardPost(UUID uuid) {
        try {
            URL url = new URL(BACKEND_URL + "/login/reward");
            HttpURLConnection conn = openConn(url, "POST", true);
            writeBody(conn, "{\"mc_uuid\":\"" + uuid + "\"}");
            if (conn.getResponseCode() == 200) return readBody(conn);
        } catch (Exception e) {
            getLogger().warning("Login reward failed: " + e.getMessage());
        }
        return null;
    }

    private void aliveReportPost(UUID uuid, String name, double activeSeconds) {
        try {
            URL url = new URL(BACKEND_URL + "/alive/report");
            HttpURLConnection conn = openConn(url, "POST", true);
            writeBody(conn, "{\"mc_uuid\":\"" + uuid + "\",\"name\":\"" + jsonEscape(name)
                    + "\",\"active_seconds\":" + activeSeconds + "}");
            if (conn.getResponseCode() != 200) {
                getLogger().warning("Alive report failed with HTTP " + conn.getResponseCode());
            }
        } catch (Exception e) {
            getLogger().warning("Alive report failed: " + e.getMessage());
        }
    }

    private void aliveDeathPost(UUID uuid, String name) {
        try {
            URL url = new URL(BACKEND_URL + "/alive/death");
            HttpURLConnection conn = openConn(url, "POST", true);
            writeBody(conn, "{\"mc_uuid\":\"" + uuid + "\",\"name\":\"" + jsonEscape(name) + "\"}");
            if (conn.getResponseCode() != 200) {
                getLogger().warning("Alive death failed with HTTP " + conn.getResponseCode());
            }
        } catch (Exception e) {
            getLogger().warning("Alive death failed: " + e.getMessage());
        }
    }

    private String bountyPlacePost(UUID issuer, UUID target, String targetName, int amount) {
        try {
            URL url = new URL(BACKEND_URL + "/bounty/place");
            HttpURLConnection conn = openConn(url, "POST", true);
            writeBody(conn, "{\"issuer_uuid\":\"" + issuer + "\",\"target_uuid\":\"" + target
                    + "\",\"target_name\":\"" + jsonEscape(targetName) + "\",\"amount\":" + amount + "}");
            if (conn.getResponseCode() == 200) return readBody(conn);
        } catch (Exception e) {
            getLogger().warning("Bounty place failed: " + e.getMessage());
        }
        return null;
    }

    private String bountyClaimPost(UUID target, UUID killer, String targetName, String killerName) {
        try {
            URL url = new URL(BACKEND_URL + "/bounty/claim");
            HttpURLConnection conn = openConn(url, "POST", true);
            writeBody(conn, "{\"target_uuid\":\"" + target + "\",\"target_name\":\"" + jsonEscape(targetName)
                    + "\",\"killer_uuid\":\"" + killer + "\",\"killer_name\":\"" + jsonEscape(killerName) + "\"}");
            if (conn.getResponseCode() == 200) return readBody(conn);
        } catch (Exception e) {
            getLogger().warning("Bounty claim failed: " + e.getMessage());
        }
        return null;
    }

    private String httpGet(String urlStr) {
        try {
            URL url = new URL(urlStr);
            HttpURLConnection conn = openConn(url, "GET", false);
            if (conn.getResponseCode() == 200) return readBody(conn);
        } catch (Exception e) { getLogger().warning("GET failed: " + e.getMessage()); }
        return null;
    }

    private HttpURLConnection openConn(URL url, String method, boolean doOutput) throws IOException {
        HttpURLConnection conn = (HttpURLConnection) url.openConnection();
        conn.setRequestMethod(method);
        conn.setConnectTimeout(8000);
        conn.setReadTimeout(8000);
        if (!API_KEY.isEmpty()) {
            conn.setRequestProperty("X-API-Key", API_KEY);
        }
        if (doOutput) {
            conn.setRequestProperty("Content-Type", "application/json");
            conn.setDoOutput(true);
        }
        return conn;
    }

    private void writeBody(HttpURLConnection conn, String json) throws IOException {
        try (OutputStream os = conn.getOutputStream()) {
            os.write(json.getBytes("UTF-8"));
        }
    }

    private String readBody(HttpURLConnection conn) throws IOException {
        try (BufferedReader in = new BufferedReader(new InputStreamReader(conn.getInputStream()))) {
            StringBuilder sb = new StringBuilder();
            String line;
            while ((line = in.readLine()) != null) {
                if (sb.length() > 0) sb.append("\n");
                sb.append(line);
            }
            return sb.toString();
        }
    }

    // ─── Inventory helpers ────────────────────────────────────────────────────
    private int countItem(Player player, Material mat) {
        int count = 0;
        for (ItemStack s : player.getInventory().getContents())
            if (s != null && s.getType() == mat) count += s.getAmount();
        return count;
    }

    private void removeItems(Player player, Material mat, int amount) {
        int remaining = amount;
        ItemStack[] contents = player.getInventory().getContents();
        for (int i = 0; i < contents.length && remaining > 0; i++) {
            ItemStack stack = contents[i];
            if (stack == null || stack.getType() != mat) continue;
            int take = Math.min(stack.getAmount(), remaining);
            stack.setAmount(stack.getAmount() - take);
            remaining -= take;
            if (stack.getAmount() <= 0) contents[i] = null;
        }
        player.getInventory().setContents(contents);
    }

    private void giveItems(Player player, Material mat, int amount) {
        HashMap<Integer, ItemStack> leftover = player.getInventory().addItem(new ItemStack(mat, amount));
        for (ItemStack item : leftover.values())
            player.getWorld().dropItemNaturally(player.getLocation(), item);
    }

    private int getCurrentExperience(Player player) {
        int total = 0;
        for (int level = 0; level < player.getLevel(); level++) {
            total += getExpToNextLevel(level);
        }
        total += Math.round(getExpToNextLevel(player.getLevel()) * player.getExp());
        return total;
    }

    private int getExpToNextLevel(int level) {
        if (level >= 30) return 112 + (level - 30) * 9;
        if (level >= 15) return 37 + (level - 15) * 5;
        return 7 + level * 2;
    }

    private void setTotalExperience(Player player, int amount) {
        player.setExp(0);
        player.setLevel(0);
        player.setTotalExperience(0);
        if (amount > 0) player.giveExp(amount);
    }

    // ─── JSON parsers (no external libraries needed) ──────────────────────────
    private int parseJsonInt(String json, String key) {
        return (int) parseJsonDouble(json, key);
    }

    private double parseJsonDouble(String json, String key) {
        String search = "\"" + key + "\":";
        int idx = json.indexOf(search);
        if (idx < 0) return 0.0;
        int s = idx + search.length();
        while (s < json.length() && json.charAt(s) == ' ') s++;
        int e = s;
        while (e < json.length() && (Character.isDigit(json.charAt(e)) || json.charAt(e) == '.' || json.charAt(e) == '-')) e++;
        try { return Double.parseDouble(json.substring(s, e)); } catch (Exception ex) { return 0.0; }
    }

    private Map<String, Double> parseCommodityMap(String json) {
        Map<String, Double> result = new LinkedHashMap<>();
        int start = json.indexOf("{", json.indexOf("balances") + 1);
        if (start < 0) return result;
        int i = start + 1;
        while (i < json.length() && json.charAt(i) != '}') {
            while (i < json.length() && json.charAt(i) != '"' && json.charAt(i) != '}') i++;
            if (i >= json.length() || json.charAt(i) == '}') break;
            int ks = i + 1, ke = json.indexOf('"', ks);
            String key = json.substring(ks, ke);
            i = json.indexOf(':', ke) + 1;
            while (i < json.length() && json.charAt(i) == ' ') i++;
            int vs = i;
            while (i < json.length() && json.charAt(i) != ',' && json.charAt(i) != '}') i++;
            try {
                double val = Double.parseDouble(json.substring(vs, i).trim());
                if (val > 0.0001) result.put(key, val);
            } catch (Exception ignored) {}
            if (i < json.length() && json.charAt(i) == ',') i++;
        }
        return result;
    }

    // ─── Formatting helpers ───────────────────────────────────────────────────
    private String fmtMat(Material m) {
        return "§e" + m.name().toLowerCase().replace("_", " ") + "§r";
    }

    private String fmtKey(String key) {
        // Capitalise first letter of each word
        String[] parts = key.split("_");
        StringBuilder sb = new StringBuilder();
        for (String p : parts) {
            if (!p.isEmpty()) sb.append(Character.toUpperCase(p.charAt(0))).append(p.substring(1)).append(" ");
        }
        return sb.toString().trim();
    }

    private String displayKey(String key) {
        if (key.equals("DAEMON")) return "Dragons";
        if (key.equals("xp")) return "XP";
        return fmtKey(key);
    }

    private String jsonEscape(String text) {
        if (text == null) return "";
        return text.replace("\\", "\\\\").replace("\"", "\\\"");
    }

    private String fmtNum(double v) {
        if (v == Math.floor(v) && v < 1e15) return String.valueOf((long) v);
        return String.format("%.4g", v).replaceAll("0+$", "").replaceAll("\\.$", "");
    }
}
