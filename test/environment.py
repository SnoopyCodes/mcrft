"""Real-time arena adapter. Raw dict observations; no Gymnasium dependency.

reset -> (observation, info)
step -> (observation, reward, terminated, truncated, info)
Infrastructure failures raise EpisodeAborted: discard that episode's rollout.
"""

import math
import time

from .arena_client import BridgeError


class EpisodeAborted(RuntimeError):
    pass


class SumoEnvironment:
    def __init__(self, arena, controllers, *, arena_name="sumo",
                 decision_interval=0.05, setup_timeout=15.0,
                 observation_timeout=3.0, opponent_policy=None):
        if len(controllers) != 2 or len({id(c) for c in controllers.values()}) != 2:
            raise ValueError("Supply two different controllers keyed by bot username")
        for value in (decision_interval, setup_timeout, observation_timeout):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Intervals and timeouts must be finite and positive")
        self.arena = arena
        self.controllers = dict(controllers)
        self.names = list(controllers)
        self.learner = self.names[0]
        self.opponent = self.names[1]
        self.arena_name = arena_name
        self.decision_interval = decision_interval
        self.setup_timeout = setup_timeout
        self.observation_timeout = observation_timeout
        self.opponent_policy = opponent_policy
        self.episode_id = None
        self._running = False
        self._states = {}
        self._next_decision = 0

    def _stop(self):
        for controller in self.controllers.values():
            controller.stop()

    def _abort(self):
        self._running = False
        self._stop()
        if self.episode_id is not None:
            try:
                self.arena.abort(self.episode_id)
            except BridgeError:
                pass

    @staticmethod
    def _wait(predicate, deadline, message):
        while True:
            value = predicate()
            if value:
                return value
            if time.monotonic() >= deadline:
                raise EpisodeAborted(message)
            time.sleep(0.005)

    def _observations(self, *, newer=False):
        states = {name: controller.get_state() for name, controller in self.controllers.items()}
        for name, state in states.items():
            if (state is None or state.get("episode_id") != self.episode_id or
                    state["age_ms"] > self.observation_timeout * 1000):
                return None
            if newer and state["tick"] <= self._states[name]["tick"]:
                return None
        return states

    def reset(self):
        self._abort()
        self.episode_id = None
        self._states = {}
        deadline = time.monotonic() + self.setup_timeout
        try:
            for name, controller in self.controllers.items():
                other = self.opponent if name == self.learner else self.learner
                controller.prepare_episode(None, other)
            self._wait(lambda: all(c.get_lifecycle()["spawned"] for c in self.controllers.values()),
                       deadline, "Bots did not spawn")
            before = {name: c.get_lifecycle()["teleport_count"] for name, c in self.controllers.items()}
            reply = self.arena.reset(self.names, self.arena_name)
            self.episode_id = reply["episode_id"]
            ready = self.arena.wait_ready(self.episode_id, max(0, deadline - time.monotonic()))
            targets = {s["username"]: s for s in ready["spawns"]}

            def teleported():
                for name, controller in self.controllers.items():
                    lifecycle = controller.get_lifecycle()
                    p = lifecycle["position"]
                    if (not lifecycle["spawned"] or p is None or
                            lifecycle["teleport_count"] <= before[name] or
                            sum((p[axis] - targets[name][axis]) ** 2 for axis in ("x", "y", "z")) > 0.25 ** 2):
                        return False
                return True

            self._wait(teleported, deadline, "Bots did not acknowledge arena teleports")
            for name, controller in self.controllers.items():
                other = self.opponent if name == self.learner else self.learner
                controller.prepare_episode(self.episode_id, other)
                controller.resume_episode()
            self._states = self._wait(self._observations, deadline, "Fresh delayed observations unavailable")
            started = self.arena.start(self.episode_id)
            self._running = True
            self._next_decision = time.monotonic()
            return self._states[self.learner], {"episode_id": self.episode_id,
                                                "start_server_tick": started["server_tick"]}
        except BaseException:
            self._abort()
            raise

    def step(self, action, *, opponent_action=None):
        if not self._running:
            raise RuntimeError("Call reset() before step()")
        try:
            result = self.arena.poll_result(self.episode_id)
            if result is None:
                if opponent_action is None:
                    opponent_action = (self.opponent_policy(self._states[self.opponent])
                                       if self.opponent_policy else {})
                self.controllers[self.learner].apply_action(**action)
                self.controllers[self.opponent].apply_action(**opponent_action)
                # No catch-up bursts if inference was slower than the interval.
                self._next_decision = max(self._next_decision, time.monotonic()) + self.decision_interval
                deadline = self._next_decision + self.observation_timeout
                while True:
                    result = self.arena.poll_result(self.episode_id)
                    if result is not None:
                        break
                    states = self._observations(newer=True)
                    if time.monotonic() >= self._next_decision and states is not None:
                        self._states = states
                        return states[self.learner], 0.0, False, False, {"episode_id": self.episode_id}
                    if time.monotonic() >= deadline:
                        raise EpisodeAborted("Bot observations stopped advancing")
                    time.sleep(0.005)
            self._running = False
            self._stop()
            if result.get("aborted"):
                raise EpisodeAborted(result["reason"])
            states = self._observations()
            if states:
                self._states = states
            winner = result.get("winner")
            reward = 0.0 if winner is None else (1.0 if winner == self.learner else -1.0)
            truncated = result["reason"] == "timeout"
            return self._states[self.learner], reward, not truncated, truncated, dict(result)
        except BaseException:
            self._abort()
            raise

    def close(self):
        self._abort()


def chase_policy(state):
    """Simple smoke-test opponent. Uses only the supplied delayed snapshot."""
    target = state["closest_entity"]
    if target is None:
        return {}
    own = state["self"]
    dx = target["position"]["x"] - own["position"]["x"]
    dz = target["position"]["z"] - own["position"]["z"]
    desired = math.atan2(-dx, -dz)
    turn = (desired - own["yaw"] + math.pi) % (2 * math.pi) - math.pi
    return {"forward": 1, "sprint": True, "attack": True,
            "delta_yaw": max(-0.35, min(0.35, turn)),
            "delta_pitch": -own["pitch"]}
