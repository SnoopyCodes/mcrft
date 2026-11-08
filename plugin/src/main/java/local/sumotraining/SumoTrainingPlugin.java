package local.sumotraining;

import com.google.gson.*;
import java.util.*;
import org.bukkit.*;
import org.bukkit.entity.Player;
import org.bukkit.plugin.java.JavaPlugin;

public final class SumoTrainingPlugin extends JavaPlugin {
    private TrainingSocketServer bridge;
    private TrainingSocketServer.Peer owner;
    private ArenaManager arena;

    @Override public void onEnable() {
        saveDefaultConfig();
        try {
            arena = new ArenaManager(getConfig().getConfigurationSection("arena"), message -> {
                if (owner != null) owner.send(message);
            });
            bridge = new TrainingSocketServer(getConfig().getInt("bridge-port", 8765),
                getConfig().getInt("bridge-read-timeout-seconds", 30));
            getServer().getPluginManager().registerEvents(arena, this);
            getServer().getScheduler().runTaskTimer(this, this::tick, 1L, 1L);
            getLogger().info("Training bridge listening on 127.0.0.1:" + bridge.port());
        } catch (Exception error) {
            getLogger().severe("Could not start training bridge: " + error.getMessage());
            getServer().getPluginManager().disablePlugin(this);
        }
    }
    private void tick() {
        if (owner != null && !owner.open) { arena.release(); owner = null; }
        // Bounded work: socket peers can never monopolize the game thread.
        for (int i = 0; i < 64; i++) {
            TrainingSocketServer.Request request = bridge.poll();
            if (request == null) break;
            if (request.peer.open) dispatch(request);
        }
        arena.tick();
    }
    private void dispatch(TrainingSocketServer.Request request) {
        JsonObject input = request.message;
        JsonObject reply = new JsonObject();
        if (input.has("id")) reply.add("reply_to", input.get("id"));
        try {
            String op = input.get("op").getAsString();
            if (op.equals("hello")) {
                if (input.get("protocol").getAsInt() != 1) throw new IllegalArgumentException("Unsupported protocol");
                if (owner != null && owner != request.peer) throw new IllegalStateException("Trainer already connected");
                owner = request.peer;
                owner.authenticated = true;
                reply.addProperty("protocol", 1);
            } else {
                if (!request.peer.authenticated || owner != request.peer)
                    throw new IllegalStateException("Send hello first");
                switch (op) {
                    case "ping": case "status": reply = arena.status(); break;
                    case "reset":
                        List<Player> players = new ArrayList<>();
                        for (JsonElement name : input.getAsJsonArray("players")) {
                            Player player = getServer().getPlayerExact(name.getAsString());
                            if (player == null) throw new IllegalArgumentException("Player is not online: " + name.getAsString());
                            players.add(player);
                        }
                        World world = getServer().getWorld(getConfig().getString("arena.world", "world"));
                        if (world == null) throw new IllegalArgumentException("Configured arena world is not loaded");
                        reply = arena.reset(input.get("arena").getAsString(), players, world);
                        break;
                    case "start": reply = arena.start(input.get("episode_id").getAsLong()); break;
                    case "abort":
                        arena.checkEpisode(input.get("episode_id").getAsLong());
                        arena.abort("trainer_abort"); reply = arena.status(); break;
                    default: throw new IllegalArgumentException("Unknown operation: " + op);
                }
            }
            reply.addProperty("ok", true);
        } catch (RuntimeException error) {
            reply.addProperty("ok", false);
            reply.addProperty("error", error.getMessage() == null ? "Invalid request" : error.getMessage());
        }
        if (input.has("id")) reply.add("reply_to", input.get("id"));
        request.peer.send(reply);
    }
    @Override public void onDisable() {
        if (arena != null) arena.release();
        if (bridge != null) bridge.close();
    }
}
