# SumoTraining: Spigot 1.8.8 ↔ Python

One arena, two real Mineflayer clients, a local TCP bridge, and real-time rounds.
The plugin manages matches; Python chooses actions. The included runner uses
scripted bots to exercise the connection. It does not update model weights.

## Build and install

Use a JDK (8 or 11) and Maven 3.9:

```sh
mvn -f plugin/pom.xml package
```

Copy `plugin/target/SumoTraining.jar` into your Spigot **1.8.8** server's
`plugins/` directory and restart the server. Gson is bundled and relocated;
you do not need another plugin. First startup creates
`plugins/SumoTraining/config.yml`.

The defaults are a Minecraft server at `127.0.0.1:25565` and a training bridge
at `127.0.0.1:8765`. Only one trainer can connect. The bridge always binds to
loopback. For a remote server, run Python there or forward port 8765 with SSH.
Configure the Minecraft server separately for these offline bot accounts;
the existing bot factory uses `auth="offline"` and version `1.8.8`.

Use dedicated training players: reset clears their inventories and armor,
removes potion effects, switches them to survival, restores health/food,
clears velocity/fire/fall distance, and teleports them. Previous inventories
and player settings are not restored.

## Prepare the arena

Build an elevated, flat platform in the configured world. The plugin does
not generate or modify terrain. A small isolated platform over empty space
works well; surrounding terrain below `loss-y` ends rounds too.

For the default configuration, place a platform at block Y=64 so the players'
feet spawn at Y=65. On an empty test area, an operator can use:

```text
/fill -8 64 -8 8 64 8 stone
```

Defaults:

| Setting | Value |
| --- | --- |
| World | `world` |
| Spawn A | `(-2.5, 65, 0.5)`, facing +X |
| Spawn B | `(3.5, 65, 0.5)`, facing -X |
| Loss | Feet Y ≤ 60 |
| Round limit | 1200 server ticks |
| Preparation | 5 server ticks |
| Ready timeout | 400 server ticks |

Spawn blocks must have air at the feet and head and a supporting block
underneath. Edit `config.yml` and restart after changing coordinates.
Keep other combat, teleport, and anticheat plugins out of the initial test.

## Run two scripted bots

From the project root, with the existing Python environment:

```sh
.venv/bin/python -m test.run_arena --rounds 10
```

Options: `--host`, `--port`, `--bridge-host`, `--bridge-port`, `--delay-ms`.
The bots join as `TrainingBot` and `OpponentBot`, fight, print the result,
and reset. The runner stops on infrastructure errors so failed episodes
cannot silently become training data. Ctrl-C aborts the match and quits both
clients. Manual chat controls are disabled for training bots; the standalone
`test/bot.py` still enables them.

## Use a model instead of the script

```python
from test.arena_client import ArenaClient
from test.bot import create_training_bot
from test.environment import SumoEnvironment, chase_policy

# Create both bots before calling env.reset().
bot_a, controls_a = create_training_bot(username="TrainingBot")
bot_b, controls_b = create_training_bot(username="OpponentBot")
with ArenaClient() as arena:
    env = SumoEnvironment(
        arena,
        {"TrainingBot": controls_a, "OpponentBot": controls_b},
        opponent_policy=chase_policy,
    )
    try:
        observation, info = env.reset()
        # In your training loop:
        # action = policy(observation)
        # next_obs, reward, terminated, truncated, info = env.step(action)
        # Store the transition; call env.reset() when the round ends.
    finally:
        env.close()
        controls_a.close()
        controls_b.close()
        bot_a.quit()
        bot_b.quit()
```

The first controller is the learner. Action dictionaries match
`SumoController.apply_action()`. Supply `opponent_policy` to choose the second
bot's actions from its own delayed observations, or pass
`env.step(action, opponent_action=...)`. Without either, it stands still.

The environment has Gymnasium-style return signatures, but returns raw
snapshot dictionaries and has no Gymnasium dependency. To use an RL library,
add a `gymnasium.Env` wrapper with numeric observations and declared spaces.
It must convert policy outputs into the controller's action dictionary.

Rewards are +1 for a win, -1 for a loss, and 0 for a draw or time limit.
Falls (including simultaneous-fall draws) terminate; time limits truncate.
`EpisodeAborted` and bridge errors are infrastructure failures: discard that
episode's rollout. Reconnect/recreate bots if needed before resetting.

## Reset and timing contract

`IDLE → PREPARING → READY → RUNNING → FINISHED`

1. Python pauses both controllers and records their teleport counters.
2. The plugin resets players, blocks movement/combat, and sends `ready`.
3. Python waits for a new Mineflayer `forcedMove` and a matching position for
   each bot. Immediate lifecycle telemetry is used only for this setup barrier.
4. Python tags the new episode, clears observation buffers, and resumes sampling.
5. After fresh delayed observations arrive, Python requests `start`.

Snapshots stay delayed by at least 250 ms by default, including at startup.
`closest_entity` is restricted to the assigned opponent during arena use.
Episode IDs and a local controller generation prevent previous snapshots and
pending post-aim attacks from crossing resets. During play, `get_lifecycle()`
must never be fed into the policy.

Decisions default to 100 ms apart. This is a real-time adapter: the server
continues ticking while Python runs inference or updates weights. Stop at
round boundaries for lengthy model updates, or use separate inference and
learning processes. There is no accelerated or exact-N-tick stepping.
`server_tick` is the plugin's per-tick counter since enable, separate from
the controller's client-physics `tick`; neither counter aligns client actions
to exact server ticks. At episode end the returned delayed observation may
precede the fall; the plugin's result supplies the authoritative reward.

Rounds preserve ordinary hit handling and knockback. Health is replenished
before an otherwise lethal ordinary hit; noncombat damage is cancelled.
Disconnects, dead/unavailable players, world changes, reset failures, and
ready timeouts abort rounds. A fall is checked before the round timeout.
Players remain frozen after completion until reset or trainer disconnect.
Reset rejects an active round; abort it first. The environment does this.

## Wire protocol

UTF-8 JSON objects separated by newlines, with a 16 KiB line limit.
The socket reader/writer threads never call Bukkit. The main thread drains
a bounded request queue once per server tick and emits copied JSON messages
through a bounded outgoing queue. Overflow disconnects the session.

Requests use a unique integer `id`; replies carry `reply_to`, `ok`, and an
`error` string on failure. Server events can arrive before request replies.

```json
{"id":1,"op":"hello","protocol":1}
{"id":2,"op":"reset","arena":"sumo","players":["TrainingBot","OpponentBot"]}
{"id":3,"op":"start","episode_id":1}
{"id":4,"op":"status"}
{"id":5,"op":"abort","episode_id":1}
```

`hello` is a version handshake and grants ownership of the single arena.
It is not authentication; the interface is limited to local processes.
`reset` returns a new episode ID. `ready` supplies spawn coordinates for the
setup barrier. `start` returns its server tick. `finished` supplies the winner
(or null), reason, aborted flag, episode ID, and server tick. `status` includes
the latest result. `ping` behaves like status.

Python sends a heartbeat every five seconds. The server disconnects silent
clients after 30 seconds by default; EOF is detected sooner. Losing the
trainer connection aborts the current round and releases participants.
Request timeouts close Python's socket because the request may already have
executed. Stale episode IDs cannot start or abort a newer round.

## Checks

```sh
node --test test/controller.test.js
.venv/bin/python -m unittest -v test.test_arena
mvn -f plugin/pom.xml package
```

The Python protocol tests and Java socket tests bind temporary localhost
ports. Arena unit tests use mocked Bukkit players/worlds. Run the scripted
runner above to verify actual server physics, teleports, and knockback.

References: [Spigot Maven setup](https://www.spigotmc.org/wiki/spigot-maven/),
[Bukkit scheduler/threading](https://hub.spigotmc.org/javadocs/spigot/org/bukkit/scheduler/BukkitScheduler.html),
[Mineflayer API](https://github.com/PrismarineJS/mineflayer/blob/master/docs/api.md).
