"""Networking layer for the chat client.

Wire protocol (to be agreed with the Server side - move to Common/ once settled):
every message is one JSON object followed by a newline ("JSON lines") over TCP.

Client -> Server
    {"type": "login",   "username": str, "password": str}
    {"type": "message", "to": "#channel" | "@user", "text": str}
    {"type": "file",    "to": "#channel" | "@user", "filename": str, "size": int, "data": base64 str}

Server -> Client
    {"type": "login_ok",    "username": str, "channels": [str], "users": [str]}
    {"type": "login_error", "reason": str}
    {"type": "message", "to": str, "sender": str, "text": str, "ts": float}
    {"type": "file",    "to": str, "sender": str, "filename": str, "size": int, "data": str, "ts": float}
    {"type": "users",   "users": [str]}          # full online-user list whenever it changes
    {"type": "error",   "reason": str}

Events whose type starts with "_" are generated locally (connected, disconnected, ...).
All incoming events are put on a queue.Queue so the GUI thread can consume them safely.
"""

import base64
import json
import queue
import random
import socket
import threading
import time

ENCODING = "utf-8"
MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MB limit for file transfers


def encode_file(path):
    """Read a file and return (filename, size, base64_data). Raises ValueError if too big."""
    import os

    size = os.path.getsize(path)
    if size > MAX_FILE_BYTES:
        raise ValueError(f"File is {size / 1024 / 1024:.1f} MB; the limit is {MAX_FILE_BYTES // 1024 // 1024} MB.")
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode("ascii")
    return os.path.basename(path), size, data


class TCPConnection:
    """Real connection to the chat server."""

    def __init__(self, inbox: queue.Queue):
        self.inbox = inbox
        self.sock = None
        self._send_lock = threading.Lock()
        self._closing = False

    def connect(self, host, port):
        """Connect in a background thread; posts _connected or _connect_failed to the inbox."""
        threading.Thread(target=self._connect_worker, args=(host, port), daemon=True).start()

    def _connect_worker(self, host, port):
        try:
            sock = socket.create_connection((host, int(port)), timeout=5)
            sock.settimeout(None)
        except (OSError, ValueError) as e:
            self.inbox.put({"type": "_connect_failed", "reason": str(e)})
            return
        self.sock = sock
        self.inbox.put({"type": "_connected"})
        self._read_loop()

    def _read_loop(self):
        try:
            with self.sock.makefile("r", encoding=ENCODING, newline="\n") as reader:
                for line in reader:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self.inbox.put(json.loads(line))
                    except json.JSONDecodeError:
                        self.inbox.put({"type": "error", "reason": "Received malformed data from server."})
        except OSError:
            pass
        if not self._closing:
            self.inbox.put({"type": "_disconnected"})

    def send(self, msg: dict):
        if self.sock is None:
            return
        data = (json.dumps(msg) + "\n").encode(ENCODING)
        try:
            with self._send_lock:
                self.sock.sendall(data)
        except OSError:
            self.inbox.put({"type": "_disconnected"})

    def close(self):
        self._closing = True
        if self.sock is not None:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.sock.close()
            self.sock = None


class DemoConnection:
    """Fake server so the GUI can be developed and tested without the real Server."""

    CHANNELS = ["general", "random", "homework"]
    BOTS = ["alice", "bob"]
    REPLIES = ["Nice!", "lol", "Agreed.", "Can you explain that again?", "Sounds good to me", "brb"]

    def __init__(self, inbox: queue.Queue):
        self.inbox = inbox
        self.username = None

    def connect(self, host, port):
        self.inbox.put({"type": "_connected"})

    def _later(self, delay, msg):
        threading.Timer(delay, self.inbox.put, args=(msg,)).start()

    def send(self, msg: dict):
        kind = msg.get("type")
        if kind == "login":
            name = msg.get("username", "").strip()
            if not name:
                self.inbox.put({"type": "login_error", "reason": "Username required."})
                return
            if name.lower() in self.BOTS:
                self.inbox.put({"type": "login_error", "reason": "That username is taken."})
                return
            self.username = name
            self.inbox.put({"type": "login_ok", "username": name,
                            "channels": self.CHANNELS, "users": [name] + self.BOTS})
            self._later(1.0, {"type": "message", "to": "#general", "sender": "alice",
                              "text": f"Hey {name}, welcome!", "ts": time.time()})
        elif kind in ("message", "file"):
            echo = dict(msg, sender=self.username, ts=time.time())
            self.inbox.put(echo)  # the server broadcasts our own message back to us
            if kind == "message":
                to = msg["to"]
                bot = to[1:] if to.startswith("@") else random.choice(self.BOTS)
                if bot in self.BOTS:
                    # A DM reply arrives addressed to us; channel replies go to the channel
                    reply_to = f"@{self.username}" if to.startswith("@") else to
                    self._later(1.5, {"type": "message", "to": reply_to, "sender": bot,
                                      "text": random.choice(self.REPLIES), "ts": time.time()})

    def close(self):
        pass
