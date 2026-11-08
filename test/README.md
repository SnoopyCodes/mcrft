# Sumo controller

For automated Spigot rounds and the Python arena bridge, see
[the plugin setup guide](../plugin/README.md). Run two scripted bots with
`.venv/bin/python -m test.run_arena --rounds 10` after installing the plugin
and configuring the arena.

Run the existing local Minecraft 1.8.8 offline bot:

```sh
.venv/bin/python test/bot.py
```

Use it from a training script in the project root:

```python
import time
from test.bot import create_training_bot

bot, controller = create_training_bot(observation_delay_ms=250)
apply_action = controller.apply_action
get_state = controller.get_state

try:
    # None until spawn and the first snapshot has aged by 250 ms.
    while get_state() is None:
        time.sleep(0.01)

    apply_action(
        forward=1,       # -1=S, 0=release, 1=W
        strafe=0,        # -1=A, 0=release, 1=D
        jump=False,
        sprint=True,
        delta_yaw=0.02,  # radians, relative to current aim
        delta_pitch=0.0,
        attack=True,    # one click per call
    )

    state = get_state()
    print(state)
    # Add your training loop here. Process each state["tick"] once.
    # get_state() is nonblocking and can repeat the same snapshot.
finally:
    controller.close()
    bot.quit()
```

Every action replaces all movement keys; omitted controls revert to their
defaults. Keys stay held until the next action. `controller.stop()` releases
them. Aim deltas apply once, using Mineflayer's yaw/pitch convention in radians:
yaw 0 faces -Z, positive yaw turns toward -X, positive pitch looks up. Pitch is
clamped to [-pi/2, pi/2]; Mineflayer quantizes rotation to mouse sensitivity.

An attack clicks once, targets the entity under the crosshair using Mineflayer's
raycast with a 3-block limit, or swings if nothing is targeted. It does not turn
toward the closest entity. Actions run immediately; the delay affects observations.

Snapshots include:

- `episode_id`: `None` for standalone use, or the plugin episode ID when
  managed by `SumoEnvironment`.
- `self` and `closest_entity`: entity ID/type/username, absolute `position`
  (`x`, `y`, `z`), `yaw`, `pitch`, and `head_yaw` in radians. The closest entity
  is selected by 3D distance at sampling time, excluding self, nonplayers, and invalid
  entities. Arena episodes filter
  this selection to the assigned opponent. It is `None` if none are tracked.
  For an opponent's facing direction, use `head_yaw` and `pitch`.
- `on_ground` in each pose: Mineflayer's boolean `onGround`, or `None` if unknown.
- `observation`: the calculated features below when `arena_center` was supplied;
  otherwise `None`.
- `tick`: a counter of sampled client physics ticks spanning spawns, labeled
  `tick_source="client_physics"`. It describes the delayed snapshot.
- `server_world_age_at_last_sync`: the last world age received from the server,
  or `None`. This is coarse time-packet data, not an exact current server tick.
- `sampled_at_ms`: monotonic milliseconds in the Node process, `delay_ms`:
  the configured minimum delay, and `age_ms`: the snapshot's age when read.

Snapshots are copied atomically on each Mineflayer `physicsTick`, then withheld
for at least the configured delay (250 ms by default). With normal 50 ms tick
spacing, the newest available snapshot is roughly 250–300 ms old. This delay
starts when the client state is sampled; network latency and any time since an
entity's last packet are additional. Self position is Mineflayer's local physics
state, and other positions are its last known entity state. No exact server
tick is inferred. If physics pauses, the last snapshot can be older; check
`age_ms`. Death, respawn, and disconnect clear observations and held keys.
Teleports also discard delayed observations and calculation history.

## Calculated training observations

Supply your arena's actual center as `(x, y, z)` in Minecraft world coordinates.
The center is explicit because the plugin's spawn positions need not be symmetric
about the platform center. No Gymnasium environment or dependency is added.

```python
from test.bot import create_training_bot
from test.observations import OBSERVATION_FIELDS, observation_vector

bot, controller = create_training_bot(
    arena_center=(-316.5, 76.0, -171.5),  # replace with your platform center
    observation_delay_ms=250,
)

# In your own environment's observation method, after spawn/warmup:
state = controller.get_state()
if state is not None:
    features = state["observation"]
    values = observation_vector(features)
    # np.asarray(values, dtype=np.float32) if your Gymnasium space needs it.
    print(dict(zip(OBSERVATION_FIELDS, values)))

# Convenience methods; None until a delayed snapshot is available:
features = controller.get_observation()
values = controller.get_observation(vector=True)
```

Use one `get_state()` call when you need matching tick/age metadata and features.
`get_observation()` requires `arena_center` at construction. Calling either
getter repeatedly does not advance history or timers.

Positions use horizontal polar coordinates plus vertical height. Angles are
radians, wrapped to `[-pi, pi)`. Yaw follows Mineflayer: 0 faces -Z, +pi/2
faces -X. All position/aim rays run between **feet positions**, with head yaw
used for facing. Thus aim offsets describe orientation relative to a geometric
ray, rather than an eye-to-hitbox aiming command.

| Feature | Meaning |
| --- | --- |
| `self_position` | `radius`: horizontal distance from center; `yaw`: world bearing center→self; `height`: self Y minus center Y. |
| `self_velocity` | `radial`, `tangential`, `vertical` velocity in blocks/client physics tick, measured in the current center→self polar basis. Positive radial = outward; positive tangential = increasing polar yaw. |
| `self_aim_to_center` | Self head yaw/pitch minus the self→center ray's yaw/pitch. Zero means facing the center. |
| `opponent_position` | `radius`: horizontal distance self→opponent; `yaw`: that ray's bearing minus self yaw; `height`: opponent Y minus self Y. Zero yaw means directly ahead. |
| `opponent_velocity` | Opponent world velocity **minus self world velocity**, projected into the current self→opponent radial/tangential/vertical basis. Positive radial means separating. This is physical relative velocity, not the derivative of the rotating local bearing. |
| `opponent_aim_to_self` | Opponent head yaw/pitch minus the opponent→self ray's yaw/pitch. Zero means the opponent faces you. |
| `on_ground` | Self's sampled ground flag. |
| `ticks_since_gotten_hit` | Physics ticks since an observed self `entityHurt` event; `None` before the first event. Damage source is unknown. |
| `ticks_since_hit_on_opponent` | Physics ticks since the opponent's `entityHurt` event matched a bot attack targeting that entity within the preceding 6 client ticks; `None` until a match. A swing/click alone never counts. |

Velocity comes from consecutive sampled positions, with a bounded history of
40 physics ticks maintained on the JavaScript side, regardless of Python polling
frequency. The initial velocity is `None`; an absent/newly selected opponent
also has unknown velocity until a consecutive pair exists. Other players' motion
uses their latest received positions: network updates can produce zeros followed
by jumps. At zero horizontal radius, bearing defaults to 0 and velocity uses the
world -Z radial / -X tangential basis. A fully coincident aim ray also uses
yaw/pitch 0.

Mineflayer's [hurt event](https://github.com/PrismarineJS/mineflayer/blob/master/docs/api.md#entityhurt-entity)
identifies the damaged entity, not its attacker. Outgoing hit attribution is an
estimate suited to a two-player arena; other damage can create false matches,
and confirmation arriving beyond 6 ticks is missed. Timers are 0 in the first
snapshot after the event, then advance with sampled physics ticks. Events and
all calculated features pass through the same observation delay as positions.
Spawn/death/respawn/disconnect, episode preparation/resume, and bot teleport clear
velocity and hit history.

The vector layout is exported as `OBSERVATION_FIELDS` (23 floats). It contains
the table's values in order, followed by `self_velocity_valid`,
`opponent_visible`, `opponent_velocity_valid`, and `on_ground_valid`.
Missing geometry/velocity/ground values become 0 with the corresponding flag
set to 0; unknown hit timers become -1. Values are unnormalized. The timer
sentinel distinguishes an unknown event from a hit on the current tick.

For manual testing, chat commands are `forward`, `back`, `left`, `right`, `jump`,
`sprint`, `turn`, `attack`, `stop`, and `state` (prints a delayed snapshot in the
Python console). Each command submits a complete action.

Run deterministic controller checks with `node --test test/controller.test.js`.
Run Python interface checks with `.venv/bin/python -m unittest -v test.test_observations`.
The implementation uses the [Mineflayer API](https://github.com/PrismarineJS/mineflayer/blob/master/docs/api.md).
