"""Threaded, local JSON-lines connection to the SumoTraining Spigot plugin."""

import json
import socket
import threading
import time


class BridgeError(RuntimeError):
    pass


class BridgeDisconnected(BridgeError):
    pass


class ArenaClient:
    def __init__(self, host="127.0.0.1", port=8765, *, timeout=10.0):
        self.timeout = timeout
        self._socket = socket.create_connection((host, port), timeout=timeout)
        self._socket.settimeout(1.0)
        self._socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._condition = threading.Condition()
        self._send_lock = threading.Lock()
        self._pending = {}
        self._events = {}
        self._next_id = 1
        self._failure: BridgeError | None = None
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name="sumo-bridge-reader")
        self._reader.start()
        try:
            reply = self.request("hello", protocol=1)
            if reply.get("protocol") != 1:
                raise BridgeError("Unsupported bridge protocol")
        except BaseException:
            self.close()
            raise

    def _fail(self, error: BridgeError) -> BridgeError:
        with self._condition:
            if self._failure is None:
                self._failure = error
            failure = self._failure
            self._condition.notify_all()
        try:
            self._socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._socket.close()
        return failure

    def _send(self, message):
        payload = (json.dumps(message, allow_nan=False) + "\n").encode("utf-8")
        if len(payload) > 16384:
            raise BridgeError("Bridge message exceeds 16 KiB")
        with self._send_lock:
            self._socket.sendall(payload)

    def _read_loop(self):
        buffer = bytearray()
        next_heartbeat = time.monotonic() + 5
        try:
            while True:
                with self._condition:
                    if self._failure is not None:
                        return
                if time.monotonic() >= next_heartbeat:
                    self._send({"id": 0, "op": "ping"})
                    next_heartbeat = time.monotonic() + 5
                try:
                    chunk = self._socket.recv(8192)
                except socket.timeout:
                    continue
                if not chunk:
                    raise BridgeDisconnected("Spigot training bridge disconnected")
                buffer.extend(chunk)
                while b"\n" in buffer:
                    line, _, remainder = buffer.partition(b"\n")
                    buffer = bytearray(remainder)
                    if len(line) > 16384:
                        raise BridgeError("Oversized bridge message")
                    message = json.loads(line)
                    if not isinstance(message, dict):
                        raise BridgeError("Bridge message must be a JSON object")
                    with self._condition:
                        request_id = message.get("reply_to")
                        # Validate before dictionary lookups: JSON arrays/objects
                        # are unhashable, and bools compare equal to integer IDs.
                        if "reply_to" in message and type(request_id) is not int:
                            raise BridgeError("Bridge reply_to must be an integer")
                        if request_id in self._pending:
                            self._pending[request_id] = message
                        elif message.get("event") in ("ready", "finished"):
                            episode = message["episode_id"]
                            if type(episode) is not int:
                                raise BridgeError("Bridge episode_id must be an integer")
                            self._events.setdefault(episode, {})[message["event"]] = message
                            while len(self._events) > 64:
                                del self._events[next(iter(self._events))]
                        self._condition.notify_all()
                if len(buffer) > 16384:
                    raise BridgeError("Oversized bridge message")
        except (OSError, ValueError, KeyError, BridgeError) as error:
            self._fail(BridgeDisconnected(str(error)))

    def request(self, op, *, timeout=None, **fields):
        timeout = self.timeout if timeout is None else timeout
        with self._condition:
            if self._failure is not None:
                raise self._failure
            request_id = self._next_id
            self._next_id += 1
            self._pending[request_id] = None
        try:
            self._send({"id": request_id, "op": op, **fields})
            with self._condition:
                arrived = self._condition.wait_for(
                    lambda: self._pending[request_id] is not None or self._failure is not None,
                    timeout=timeout,
                )
                if not arrived:
                    # The operation may have executed. Closing the session makes
                    # the plugin abort instead of leaving an ambiguous live round.
                    self._fail(BridgeDisconnected(f"Timed out waiting for {op}"))
                if self._failure is not None:
                    raise self._failure
                reply = self._pending[request_id]
                if not reply.get("ok"):
                    raise BridgeError(reply.get("error", "Plugin rejected request"))
                return reply
        except OSError as error:
            raise self._fail(BridgeDisconnected(str(error))) from error
        finally:
            with self._condition:
                self._pending.pop(request_id, None)

    def reset(self, players, arena="sumo"):
        return self.request("reset", players=list(players), arena=arena)

    def start(self, episode_id):
        return self.request("start", episode_id=episode_id)

    def abort(self, episode_id):
        return self.request("abort", episode_id=episode_id)

    def wait_ready(self, episode_id, timeout=None):
        with self._condition:
            arrived = self._condition.wait_for(
                lambda: bool(self._events.get(episode_id)) or self._failure is not None,
                timeout=self.timeout if timeout is None else timeout,
            )
            if self._failure is not None:
                raise self._failure
            if not arrived:
                raise BridgeError("Timed out waiting for arena readiness")
            events = self._events[episode_id]
            if "finished" in events:
                raise BridgeError(f"Episode ended during preparation: {events['finished']['reason']}")
            return events["ready"]

    def poll_result(self, episode_id):
        with self._condition:
            if self._failure is not None:
                raise self._failure
            return self._events.get(episode_id, {}).get("finished")

    def close(self):
        self._fail(BridgeDisconnected("Training bridge closed"))
        if threading.current_thread() is not self._reader:
            self._reader.join(timeout=2)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
