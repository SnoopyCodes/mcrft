"""Manual controls and intentionally delayed observations for a Mineflayer bot."""

import json
import math
from pathlib import Path

if __package__:
    from .observations import observation_vector
else:
    from observations import observation_vector


class SumoController:
    def __init__(self, bot, *, observation_delay_ms=250, arena_center=None):
        if not math.isfinite(observation_delay_ms) or observation_delay_ms < 0:
            raise ValueError("observation_delay_ms must be finite and nonnegative")

        if arena_center is not None:
            if (len(arena_center) != 3 or
                    any(isinstance(v, bool) or not isinstance(v, (int, float)) or
                        not math.isfinite(v) for v in arena_center)):
                raise ValueError("arena_center must be three finite coordinates (x, y, z)")
            arena_center = dict(zip(("x", "y", "z"), arena_center))
        self._has_center = arena_center is not None

        from javascript import require

        helper = require(str(Path(__file__).with_suffix(".js").resolve()))
        self._controller = helper.createController(
            bot, {"delayMs": observation_delay_ms, "arenaCenter": arena_center}
        )

    def apply_action(
        self,
        *,
        forward=0,
        strafe=0,
        jump=False,
        sprint=False,
        delta_yaw=0.0,
        delta_pitch=0.0,
        attack=False,
    ):
        """Replace held keys; apply aim deltas and one attack click immediately.

        forward: -1=S, 0=release, 1=W. strafe: -1=A, 0=release, 1=D.
        Angles use Mineflayer radians; positive pitch looks up. Keys persist
        until the next action. Calling with no arguments releases all keys.
        Call only after spawn; each attack uses the crosshair with 3-block reach.
        """
        for name, value in (("forward", forward), ("strafe", strafe)):
            if isinstance(value, bool) or value not in (-1, 0, 1):
                raise ValueError(f"{name} must be -1, 0, or 1")
        for name, value in (("jump", jump), ("sprint", sprint), ("attack", attack)):
            if not isinstance(value, bool):
                raise TypeError(f"{name} must be a bool")
        for name, value in (("delta_yaw", delta_yaw), ("delta_pitch", delta_pitch)):
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{name} must be finite radians")

        self._controller.applyActionJSON(json.dumps({
            "forward": forward,
            "strafe": strafe,
            "jump": jump,
            "sprint": sprint,
            "delta_yaw": delta_yaw,
            "delta_pitch": delta_pitch,
            "attack": attack,
        }, allow_nan=False))

    def get_state(self):
        """Return the newest snapshot at least observation_delay_ms old, or None.

        Snapshots are sampled each client physics tick. All positions, aim,
        nearest-entity selection, and tick data come from that past snapshot.
        The tick counter spans spawns; it is not an authoritative server tick.
        """
        return json.loads(self._controller.getStateJSON())

    def get_observation(self, *, vector=False):
        """Return delayed calculated features (dict), or a fixed list of floats.

        Supply arena_center=(x, y, z) at construction. Returns None during
        startup/reset warmup. Uses the same snapshot/delay as get_state();
        calling repeatedly does not advance history or hit timers. For tick
        and age metadata, read get_state() and use its "observation" entry.
        """
        if not self._has_center:
            raise ValueError("Supply arena_center=(x, y, z) when creating the bot/controller")
        state = self.get_state()
        observation = None if state is None else state["observation"]
        return observation_vector(observation) if vector else observation

    def stop(self):
        """Release all held movement and jump keys."""
        self._controller.stopControls()

    def prepare_episode(self, episode_id, opponent_username):
        """Pause controls and discard observations/actions from the old round."""
        self._controller.prepareEpisodeJSON(json.dumps({
            "episode_id": episode_id, "opponent_username": opponent_username,
        }))

    def resume_episode(self):
        """Resume sampling after the environment has verified the teleport."""
        self._controller.resumeEpisode()

    def get_lifecycle(self):
        """Immediate setup telemetry. Do not include this in model observations."""
        return json.loads(self._controller.getLifecycleJSON())

    def close(self):
        """Release controls and detach the controller's event listeners."""
        self._controller.close()
