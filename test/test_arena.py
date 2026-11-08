"""Protocol and environment regression tests; no Minecraft server required."""

import json
import socket
import threading
import unittest
from contextlib import contextmanager

from test.arena_client import ArenaClient, BridgeDisconnected, BridgeError
from test.environment import EpisodeAborted, SumoEnvironment


@contextmanager
def fake_server(handler):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    connections = []
    failures = []

    def run():
        try:
            connection, _ = listener.accept()
            connections.append(connection)
            with connection, connection.makefile("rb") as stream:
                for line in stream:
                    message = json.loads(line)
                    if message["op"] == "hello":
                        connection.sendall((json.dumps({"reply_to": message["id"], "ok": True,
                                                        "protocol": 1}) + "\n").encode())
                    elif handler(connection, message) is False:
                        break
        except OSError:
            pass  # Fixture teardown may close the connection.
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1]
    finally:
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        listener.close()
        thread.join(timeout=2)
        if failures:
            raise failures[0]


class ArenaClientTests(unittest.TestCase):
    def test_events_before_replies_fragmentation_and_episode_isolation(self):
        def handler(connection, message):
            payload = (
                {"event": "finished", "episode_id": 6, "reason": "fall"},
                {"event": "ready", "episode_id": 7, "spawns": []},
                {"reply_to": message["id"], "ok": True, "episode_id": 7},
            )
            encoded = "".join(json.dumps(item) + "\n" for item in payload).encode()
            connection.sendall(encoded[:13])
            connection.sendall(encoded[13:])

        with fake_server(handler) as port, ArenaClient(port=port) as client:
            self.assertEqual(client.reset(["A", "B"])["episode_id"], 7)
            self.assertEqual(client.wait_ready(7)["episode_id"], 7)
            self.assertIsNone(client.poll_result(7))
            self.assertEqual(client.poll_result(6)["reason"], "fall")

    def test_concurrent_requests_receive_their_own_replies(self):
        requests = []

        def handler(connection, message):
            requests.append(message)
            if len(requests) == 2:
                for request in reversed(requests):
                    connection.sendall((json.dumps({"reply_to": request["id"], "ok": True,
                                                    "echo": request["value"]}) + "\n").encode())

        with fake_server(handler) as port, ArenaClient(port=port) as client:
            results = {}
            threads = [threading.Thread(target=lambda n=n: results.update(
                {n: client.request("status", value=n)["echo"]})) for n in (1, 2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)
            self.assertEqual(results, {1: 1, 2: 2})

    def test_error_reply_does_not_destroy_connection(self):
        def handler(connection, message):
            connection.sendall((json.dumps({"reply_to": message["id"], "ok": False,
                                            "error": "Stale episode_id"}) + "\n").encode())

        with fake_server(handler) as port, ArenaClient(port=port) as client:
            for _ in range(2):
                with self.assertRaisesRegex(BridgeError, "Stale episode"):
                    client.start(1)

    def test_disconnect_wakes_request_and_event_waiters(self):
        with fake_server(lambda *_: False) as port, ArenaClient(port=port) as client:
            with self.assertRaises(BridgeDisconnected):
                client.request("drop")
            with self.assertRaises(BridgeDisconnected):
                client.wait_ready(1)

    def test_malformed_ids_disconnect_instead_of_crashing_reader(self):
        for message in (
            {"reply_to": []},
            {"reply_to": {}},
            {"reply_to": True},
            {"reply_to": 1.0},
            {"reply_to": None},
            {"event": "ready", "episode_id": []},
            {"event": "finished", "episode_id": {}},
            {"event": "ready", "episode_id": True},
        ):
            with self.subTest(message=message):
                def handler(connection, request):
                    connection.sendall((json.dumps(message) + "\n").encode())

                with fake_server(handler) as port, ArenaClient(port=port) as client:
                    with self.assertRaisesRegex(BridgeDisconnected, "must be an integer"):
                        client.request("status", timeout=0.5)
                    with self.assertRaises(BridgeDisconnected):
                        client.wait_ready(1)

    def test_timeout_closes_ambiguous_session(self):
        with fake_server(lambda *_: None) as port, ArenaClient(port=port) as client:
            with self.assertRaisesRegex(BridgeDisconnected, "Timed out"):
                client.request("reset", timeout=0.03)
            with self.assertRaises(BridgeDisconnected):
                client.request("status")

    def test_abort_during_preparation_wakes_readiness_wait(self):
        def handler(connection, message):
            connection.sendall((json.dumps({"event": "finished", "episode_id": 2,
                                            "reason": "player_disconnected", "aborted": True}) + "\n" +
                                json.dumps({"reply_to": message["id"], "ok": True}) + "\n").encode())

        with fake_server(handler) as port, ArenaClient(port=port) as client:
            client.request("status")
            with self.assertRaisesRegex(BridgeError, "player_disconnected"):
                client.wait_ready(2)


class FakeController:
    def __init__(self):
        self.episode = None
        self.paused = False
        self.teleports = 0
        self.tick = 0
        self.stops = 0
        self.actions = []
        self.on_action = lambda: None
        self.frozen_observations = False

    def prepare_episode(self, episode, opponent):
        self.episode = episode
        self.opponent = opponent
        self.paused = True

    def resume_episode(self):
        self.paused = False

    def get_lifecycle(self):
        return {"spawned": True, "teleport_count": self.teleports,
                "position": {"x": 0, "y": 65, "z": 0}}

    def get_state(self):
        if self.paused:
            return None
        if not self.frozen_observations:
            self.tick += 1
        return {"episode_id": self.episode, "tick": self.tick, "age_ms": 250}

    def apply_action(self, **action):
        if self.paused:
            raise RuntimeError("Controller paused")
        self.actions.append(action)
        self.on_action()

    def stop(self):
        self.stops += 1


class FakeArena:
    def __init__(self, controllers):
        self.controllers = controllers
        self.episode = 0
        self.results = {}
        self.aborts = []
        self.deliver_teleport = True

    def reset(self, names, arena):
        self.episode += 1
        if self.deliver_teleport:
            for controller in self.controllers.values():
                controller.teleports += 1
        return {"episode_id": self.episode}

    def wait_ready(self, episode, timeout):
        return {"spawns": [{"username": name, "x": 0, "y": 65, "z": 0}
                           for name in self.controllers]}

    def start(self, episode):
        return {"server_tick": 20}

    def poll_result(self, episode):
        return self.results.get(episode)

    def abort(self, episode):
        self.aborts.append(episode)


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.controllers = {name: FakeController() for name in ("A", "B")}
        self.arena = FakeArena(self.controllers)
        self.env = SumoEnvironment(self.arena, self.controllers, decision_interval=0.001,
                                   setup_timeout=0.03, observation_timeout=0.3)

    def tearDown(self):
        self.env.close()

    def result(self, *, winner="A", reason="fall", aborted=False):
        self.arena.results[self.env.episode_id] = {
            "episode_id": self.env.episode_id, "server_tick": 50,
            "winner": winner, "reason": reason, "aborted": aborted,
        }

    def test_reset_and_step_use_fresh_episode_observations(self):
        obs, info = self.env.reset()
        self.assertEqual(obs["episode_id"], 1)
        self.assertEqual(info["start_server_tick"], 20)
        self.assertEqual(self.controllers["A"].opponent, "B")
        next_obs, reward, terminated, truncated, _ = self.env.step({"forward": 1})
        self.assertGreater(next_obs["tick"], obs["tick"])
        self.assertEqual((reward, terminated, truncated), (0, False, False))
        self.assertEqual(self.controllers["B"].actions, [{}])
        new_obs, _ = self.env.reset()
        self.assertEqual(new_obs["episode_id"], 2)
        self.assertIn(1, self.arena.aborts)

    def test_missing_teleport_aborts_instead_of_starting_with_old_state(self):
        self.arena.deliver_teleport = False
        with self.assertRaisesRegex(EpisodeAborted, "teleports"):
            self.env.reset()
        self.assertIn(1, self.arena.aborts)
        self.assertTrue(self.controllers["A"].paused)

    def test_win_loss_draw_and_timeout_have_distinct_outputs(self):
        for winner, reason, expected in (
            ("A", "fall", (1, True, False)), ("B", "fall", (-1, True, False)),
            (None, "fall", (0, True, False)), (None, "timeout", (0, False, True)),
        ):
            self.env.reset()
            self.controllers["A"].on_action = lambda w=winner, r=reason: self.result(winner=w, reason=r)
            _, reward, terminated, truncated, _ = self.env.step({"attack": True})
            self.assertEqual((reward, terminated, truncated), expected)
            with self.assertRaisesRegex(RuntimeError, "reset"):
                self.env.step({})

    def test_infrastructure_abort_is_never_returned_as_training_loss(self):
        self.env.reset()
        self.result(winner=None, reason="player_disconnected", aborted=True)
        with self.assertRaisesRegex(EpisodeAborted, "player_disconnected"):
            self.env.step({})
        self.assertEqual(self.controllers["A"].actions, [])

    def test_frozen_client_ticks_abort_the_episode(self):
        self.env.observation_timeout = 0.02
        # Keep the fake observation age below the new stale-state threshold.
        for c in self.controllers.values():
            original = c.get_state
            def fresh_age(original=original):
                value = original()
                if value:
                    value["age_ms"] = 0
                return value
            c.get_state = fresh_age
        self.env.reset()
        for c in self.controllers.values():
            c.frozen_observations = True
        with self.assertRaisesRegex(EpisodeAborted, "stopped advancing"):
            self.env.step({})


if __name__ == "__main__":
    unittest.main()
