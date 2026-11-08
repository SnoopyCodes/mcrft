package local.sumotraining;

import com.google.gson.JsonObject;
import java.util.*;
import java.util.concurrent.atomic.AtomicReference;
import org.bukkit.*;
import org.bukkit.block.Block;
import org.bukkit.configuration.MemoryConfiguration;
import org.bukkit.entity.Player;
import org.bukkit.event.entity.*;
import org.bukkit.event.player.PlayerMoveEvent;
import org.bukkit.inventory.PlayerInventory;
import org.junit.*;
import static org.junit.Assert.*;
import static org.mockito.Mockito.*;

public class ArenaManagerTest {
    private ArenaManager arena;
    private World world;
    private Player a, b;
    private AtomicReference<Location> posA, posB;
    private List<JsonObject> events;

    @Before public void setup() {
        MemoryConfiguration config = new MemoryConfiguration();
        config.set("name", "sumo"); config.set("loss-y", 60.0);
        config.set("prepare-ticks", 2); config.set("ready-timeout-ticks", 5);
        config.set("max-round-ticks", 4);
        for (String key : Arrays.asList("spawn-a", "spawn-b")) {
            config.set(key + ".x", key.equals("spawn-a") ? -2.5 : 3.5);
            config.set(key + ".y", 65.0); config.set(key + ".z", 0.5);
        }
        world = mock(World.class);
        Block air = mock(Block.class), solid = mock(Block.class);
        when(air.isEmpty()).thenReturn(true);
        when(world.getBlockAt(any(Location.class))).thenAnswer(invocation ->
            ((Location)invocation.getArgument(0)).getY() >= 65 ? air : solid);
        posA = new AtomicReference<>(new Location(world, 0, 65, 0));
        posB = new AtomicReference<>(new Location(world, 0, 65, 0));
        a = player("A", posA); b = player("B", posB);
        events = new ArrayList<>();
        arena = new ArenaManager(config, events::add);
    }
    private Player player(String name, AtomicReference<Location> pos) {
        Player p = mock(Player.class);
        when(p.getName()).thenReturn(name); when(p.isOnline()).thenReturn(true);
        when(p.getWorld()).thenReturn(world); when(p.getMaxHealth()).thenReturn(20.0);
        when(p.getHealth()).thenReturn(20.0);
        when(p.getInventory()).thenReturn(mock(PlayerInventory.class));
        when(p.getActivePotionEffects()).thenReturn(Collections.emptyList());
        when(p.getLocation()).thenAnswer(invocation -> pos.get().clone());
        when(p.teleport(any(Location.class))).thenAnswer(invocation -> {
            pos.set(((Location)invocation.getArgument(0)).clone()); return true;
        });
        return p;
    }
    private long reset() {
        return arena.reset("sumo", Arrays.asList(a, b), world).get("episode_id").getAsLong();
    }
    private long start() {
        long id = reset(); arena.tick(); arena.tick(); arena.start(id); return id;
    }

    @Test public void handshakeAndOldEpisodeRejection() {
        long first = reset();
        assertThrows(IllegalStateException.class, () -> arena.start(first));
        arena.tick(); arena.tick();
        assertEquals("ready", events.get(0).get("event").getAsString());
        assertEquals(2, events.get(0).getAsJsonArray("spawns").size());
        arena.start(first);
        assertThrows(IllegalStateException.class, this::reset);
        arena.abort("test");
        long second = reset();
        arena.tick(); arena.tick();
        assertTrue(second > first);
        assertThrows(IllegalArgumentException.class, () -> arena.start(first));
        arena.start(second);
    }
    @Test public void fallIsReportedExactlyOnceAndFreezesMovement() {
        start();
        posA.set(new Location(world, -2, 59, 0));
        arena.tick(); arena.tick();
        assertEquals(2, events.size());
        JsonObject result = events.get(1);
        assertEquals("B", result.get("winner").getAsString());
        assertFalse(result.get("aborted").getAsBoolean());
        PlayerMoveEvent movement = new PlayerMoveEvent(b, posB.get(), posB.get().clone().add(1, 0, 0));
        arena.onMove(movement);
        assertEquals(posB.get().getX(), movement.getTo().getX(), 0);
    }
    @Test public void simultaneousFallsAreDraws() {
        start(); posA.get().setY(59); posB.get().setY(59); arena.tick();
        assertTrue(events.get(1).get("winner").isJsonNull());
        assertEquals("fall", events.get(1).get("reason").getAsString());
    }
    @Test public void timeoutAndReadinessTimeoutAreDistinct() {
        start(); for (int i = 0; i < 4; i++) arena.tick();
        assertEquals("timeout", events.get(1).get("reason").getAsString());
        assertFalse(events.get(1).get("aborted").getAsBoolean());
        reset(); for (int i = 0; i < 7; i++) arena.tick();
        assertEquals("ready_timeout", events.get(3).get("reason").getAsString());
        assertTrue(events.get(3).get("aborted").getAsBoolean());
    }
    @Test public void damageIsBlockedWhilePreparingAndNormalHitsRemainUncancelled() {
        long id = reset();
        EntityDamageByEntityEvent hit = new EntityDamageByEntityEvent(a, b, EntityDamageEvent.DamageCause.ENTITY_ATTACK, 1);
        arena.onDamage(hit); assertTrue(hit.isCancelled());
        arena.tick(); arena.tick(); arena.start(id);
        hit = new EntityDamageByEntityEvent(a, b, EntityDamageEvent.DamageCause.ENTITY_ATTACK, 1);
        arena.onDamage(hit); assertFalse(hit.isCancelled());
    }
    @Test public void missingPlayersAbortAndReleaseRemovesFreeze() {
        start(); when(a.isOnline()).thenReturn(false); arena.tick();
        assertTrue(events.get(1).get("aborted").getAsBoolean());
        arena.release(); assertFalse(arena.participant(b));
    }
    @Test public void invalidPlatformFailsBeforeModifyingPlayers() {
        Block air = mock(Block.class); when(air.isEmpty()).thenReturn(true);
        when(world.getBlockAt(any(Location.class))).thenReturn(air);
        assertThrows(IllegalArgumentException.class, this::reset);
        verify(a, never()).teleport(any(Location.class));
    }
}
