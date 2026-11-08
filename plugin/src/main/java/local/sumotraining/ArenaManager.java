package local.sumotraining;

import com.google.gson.*;
import java.util.*;
import java.util.function.Consumer;
import org.bukkit.*;
import org.bukkit.configuration.ConfigurationSection;
import org.bukkit.entity.Player;
import org.bukkit.event.*;
import org.bukkit.event.entity.*;
import org.bukkit.event.player.*;
import org.bukkit.inventory.ItemStack;
import org.bukkit.potion.PotionEffect;
import org.bukkit.util.Vector;

/** All methods and event handlers execute on the server thread. One arena. */
final class ArenaManager implements Listener {
    enum State { IDLE, PREPARING, READY, RUNNING, FINISHED }
    private final ConfigurationSection config;
    private final Consumer<JsonObject> events;
    private final List<Player> players = new ArrayList<>();
    private final List<Location> spawns = new ArrayList<>();
    private State state = State.IDLE;
    private long episode = 0, serverTick = 0, phaseTick = 0;
    private JsonObject result;

    ArenaManager(ConfigurationSection config, Consumer<JsonObject> events) {
        this.config = config;
        this.events = events;
        for (String key : Arrays.asList("max-round-ticks", "prepare-ticks", "ready-timeout-ticks")) {
            if (config.getInt(key) <= 0) throw new IllegalArgumentException(key + " must be positive");
        }
    }
    boolean isActive() { return state != State.IDLE && state != State.FINISHED; }
    boolean participant(Player p) { return players.contains(p); }
    private boolean frozen(Player p) { return participant(p) && state != State.RUNNING; }

    JsonObject status() {
        JsonObject message = new JsonObject();
        message.addProperty("episode_id", episode);
        message.addProperty("server_tick", serverTick);
        message.addProperty("status", state.name().toLowerCase(Locale.ROOT));
        if (result != null) message.add("result", result.deepCopy());
        return message;
    }

    JsonObject reset(String arena, List<Player> requested, World world) {
        if (!config.getString("name", "sumo").equals(arena))
            throw new IllegalArgumentException("Unknown arena");
        if (requested.size() != 2 || requested.get(0).equals(requested.get(1)))
            throw new IllegalArgumentException("Two different players are required");
        if (isActive()) throw new IllegalStateException("Abort the current episode before reset");
        List<Location> targets = Arrays.asList(spawn(world, "spawn-a"), spawn(world, "spawn-b"));
        for (int i = 0; i < 2; i++) {
            Player player = requested.get(i);
            Location target = targets.get(i);
            if (!player.isOnline() || player.isDead())
                throw new IllegalArgumentException("Player must be online and alive: " + player.getName());
            if (target.getY() <= config.getDouble("loss-y") ||
                target.clone().subtract(0, 1, 0).getBlock().isEmpty() ||
                !target.getBlock().isEmpty() || !target.clone().add(0, 1, 0).getBlock().isEmpty())
                throw new IllegalArgumentException("Spawn needs a platform, two air blocks, and y > loss-y");
        }
        players.clear(); players.addAll(requested);
        spawns.clear(); spawns.addAll(targets);
        episode++;
        result = null;
        state = State.PREPARING;
        phaseTick = serverTick;
        try {
            for (int i = 0; i < 2; i++) {
                Player player = players.get(i);
                player.setGameMode(GameMode.SURVIVAL);
                player.setAllowFlight(false);
                player.setFlying(false);
                player.getInventory().clear();
                player.getInventory().setArmorContents(new ItemStack[4]);
                for (PotionEffect effect : player.getActivePotionEffects())
                    player.removePotionEffect(effect.getType());
                player.setHealth(player.getMaxHealth());
                player.setFoodLevel(20); player.setSaturation(20);
                player.setFireTicks(0); player.setFallDistance(0);
                player.setNoDamageTicks(0); player.setSprinting(false);
                player.setVelocity(new Vector());
                if (!player.teleport(spawns.get(i))) throw new IllegalStateException("Teleport was cancelled");
                player.setVelocity(new Vector());
            }
        } catch (RuntimeException error) {
            finish(null, "reset_failed", true);
            throw error;
        }
        return status();
    }
    private Location spawn(World world, String key) {
        ConfigurationSection s = config.getConfigurationSection(key);
        if (s == null) throw new IllegalArgumentException("Missing " + key);
        double x = s.getDouble("x"), y = s.getDouble("y"), z = s.getDouble("z");
        if (!Double.isFinite(x) || !Double.isFinite(y) || !Double.isFinite(z))
            throw new IllegalArgumentException("Spawn coordinates must be finite");
        return new Location(world, x, y, z, (float)s.getDouble("yaw"), (float)s.getDouble("pitch"));
    }
    JsonObject start(long expectedEpisode) {
        checkEpisode(expectedEpisode);
        if (state != State.READY) throw new IllegalStateException("Arena is not ready");
        for (Player player : players) player.setNoDamageTicks(0);
        state = State.RUNNING;
        phaseTick = serverTick;
        return status();
    }
    void checkEpisode(long expected) {
        if (expected != episode) throw new IllegalArgumentException("Stale episode_id");
    }
    void abort(String reason) { if (isActive()) finish(null, reason, true); }
    void release() {
        abort("trainer_disconnected");
        players.clear(); spawns.clear(); state = State.IDLE;
    }
    void tick() {
        serverTick++;
        if (!isActive()) return;
        for (Player player : players) {
            if (!player.isOnline() || player.isDead()) { finish(null, "player_unavailable", true); return; }
            if (!player.getWorld().equals(spawns.get(0).getWorld())) {
                finish(null, "world_changed", true); return;
            }
        }
        if (state == State.PREPARING && serverTick - phaseTick >= config.getInt("prepare-ticks")) {
            state = State.READY;
            phaseTick = serverTick;
            JsonObject event = status();
            event.addProperty("event", "ready");
            JsonArray targets = new JsonArray();
            for (int i = 0; i < 2; i++) {
                JsonObject target = new JsonObject();
                target.addProperty("username", players.get(i).getName());
                target.addProperty("x", spawns.get(i).getX());
                target.addProperty("y", spawns.get(i).getY());
                target.addProperty("z", spawns.get(i).getZ());
                targets.add(target);
            }
            event.add("spawns", targets);
            events.accept(event);
        } else if (state == State.READY && serverTick - phaseTick >= config.getInt("ready-timeout-ticks")) {
            finish(null, "ready_timeout", true);
        } else if (state == State.RUNNING) {
            double lossY = config.getDouble("loss-y");
            boolean a = players.get(0).getLocation().getY() <= lossY;
            boolean b = players.get(1).getLocation().getY() <= lossY;
            if (a || b) finish(a && b ? null : players.get(a ? 1 : 0).getName(), "fall", false);
            else if (serverTick - phaseTick >= config.getInt("max-round-ticks")) finish(null, "timeout", false);
        }
    }
    private void finish(String winner, String reason, boolean aborted) {
        state = State.FINISHED;
        result = new JsonObject();
        result.addProperty("event", "finished");
        result.addProperty("episode_id", episode);
        result.addProperty("server_tick", serverTick);
        result.addProperty("winner", winner);
        result.addProperty("reason", reason);
        result.addProperty("aborted", aborted);
        for (Player player : players) if (player.isOnline()) player.setVelocity(new Vector());
        events.accept(result.deepCopy());
    }

    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void onMove(PlayerMoveEvent event) {
        if (event instanceof PlayerTeleportEvent || !frozen(event.getPlayer()) || event.getTo() == null) return;
        // Keep feet fixed but allow looking while the observation buffer warms up.
        Location target = event.getFrom().clone();
        target.setYaw(event.getTo().getYaw()); target.setPitch(event.getTo().getPitch());
        event.setTo(target);
    }
    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void onDamage(EntityDamageEvent event) {
        if (!(event.getEntity() instanceof Player) || !participant((Player)event.getEntity())) return;
        Player victim = (Player)event.getEntity();
        if (state != State.RUNNING) { event.setCancelled(true); return; }
        if (event instanceof EntityDamageByEntityEvent) {
            org.bukkit.entity.Entity attacker = ((EntityDamageByEntityEvent)event).getDamager();
            if (!(attacker instanceof Player) || !participant((Player)attacker)) {
                event.setCancelled(true); return;
            }
            // Do not cancel normal hits: cancellation suppresses knockback.
            // Prevent health deaths so rounds can end only by falling/time limit.
            if (event.getFinalDamage() >= victim.getHealth()) victim.setHealth(victim.getMaxHealth());
        } else { event.setCancelled(true); }
    }
    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void onAttack(EntityDamageByEntityEvent event) {
        if (event.getDamager() instanceof Player && participant((Player)event.getDamager()) &&
            (!(event.getEntity() instanceof Player) || !participant((Player)event.getEntity()) || state != State.RUNNING))
            event.setCancelled(true);
    }
    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void onVelocity(PlayerVelocityEvent event) {
        if (frozen(event.getPlayer())) event.setCancelled(true);
    }
    @EventHandler public void onQuit(PlayerQuitEvent event) {
        if (participant(event.getPlayer())) abort("player_disconnected");
    }
}
