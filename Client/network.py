"""Networking layer for the chat client.

Wire protocol (to be agreed with the Server side - move to Common/ once settled):
every message is one JSON object followed by a newline ("JSON lines") over TCP.

Client -> Server
    {"type": "login",   "username": str, "password": str}
    {"type": "message", "to": "#channel" | "@user", "text": str}
    {"type": "file_offer",   "to": "#channel" | "@user", "offer_id": uuid4 hex, "filename": str, "size": int}
    {"type": "file_request", "offer_id": str}                  # the user clicked a file
    {"type": "file_start", "to": "@requester", "file_id": uuid4 hex, "offer_id": str}
    {"type": "file_chunk", "to": str, "file_id": str, "seq": int, "data": base64 str}   # 64 KB of file
    {"type": "file_end",   "to": str, "file_id": str, "sha256": hex str}
    {"type": "file_abort", "file_id": str, "reason": str}

Server -> Client
    {"type": "login_ok",    "username": str, "channels": [str], "users": [str]}
    {"type": "login_error", "reason": str}
    {"type": "message", "to": str, "sender": str, "text": str, "ts": float}
    {"type": "file_offer",   "to": str, "sender": str, "offer_id": str, "filename": str, "size": int, "ts": float}
    {"type": "file_request", "offer_id": str, "requester": str}   # to the sender: someone clicked your file
    {"type": "file_start", "to": str, "sender": str, "file_id": str, "offer_id": str, "filename": str,
     "size": int, "ts": float}
    {"type": "file_chunk", "to": str, "sender": str, "file_id": str, "seq": int, "data": str}
    {"type": "file_end",   "to": str, "sender": str, "file_id": str, "sha256": str}
    {"type": "file_abort", "file_id" | "offer_id": str, "reason": str}   # a transfer or offer failed
    {"type": "users",   "users": [str]}          # full online-user list whenever it changes
    {"type": "error",   "reason": str}

Sharing a file sends one file_offer, which shows as a card in the chat. Nothing else
happens until someone clicks the card (file_request). The sender then streams it to
that person: one file_start, one file_chunk per 64 KB, one file_end. The server stamps
"sender" so a client can't claim to be someone else, stores no file data, and only
accepts a file_start that answers a real file_request.

Events whose type starts with "_" are generated locally (connected, disconnected, ...).
Incoming events are put on a queue.Queue so the GUI thread can consume them safely.
File messages are the exception: see file_transfer.py for why they skip the queue.
"""

import json
import queue
import random
import socket
import threading
import time

from file_transfer import FileTransfers

ENCODING = "utf-8"


class TCPConnection:
    """Real connection to the chat server."""

    def __init__(self, inbox: queue.Queue, download_dir="downloads"):
        self.inbox = inbox
        self.sock = None
        self._send_lock = threading.Lock()  # one sendall at a time, or framing corrupts
        self._closing = False
        self._down_lock = threading.Lock()  # so only one thread reports the disconnect
        self._down = False
        self._outbox = queue.Queue()  # chat waiting for the writer thread
        self.files = FileTransfers(self, inbox, download_dir)

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
        threading.Thread(target=self._write_loop, daemon=True).start()
        self.inbox.put({"type": "_connected"})
        self._read_loop(sock)

    def _read_loop(self, sock):
        try:
            with sock.makefile("r", encoding=ENCODING, newline="\n") as reader:
                for line in reader:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        self.inbox.put({"type": "error", "reason": "Received malformed data from server."})
                        continue
                    if not self.files.handle(msg):
                        self.inbox.put(msg)
        except (OSError, ValueError):  # ValueError: socket closed under the reader
            pass
        self._connection_lost()

    def _write_loop(self):
        """Sends chat for the GUI thread, which must never wait on a busy socket."""
        while True:
            msg = self._outbox.get()
            if msg is None:
                return
            self.safe_send(msg)

    def send(self, msg: dict):
        """Queue a message and return at once. Safe to call from the GUI thread."""
        if self.sock is not None:
            self._outbox.put(msg)

    def safe_send(self, msg: dict):
        """Send one whole message. The lock stops threads interleaving their bytes.

        Blocks while the socket is full, so only call it from a background thread.
        Returns False if the connection has gone.
        """
        data = (json.dumps(msg) + "\n").encode(ENCODING)
        try:
            with self._send_lock:
                self.sock.sendall(data)
            return True
        except (OSError, AttributeError):  # AttributeError: close() already cleared self.sock
            self._connection_lost()
            return False

    def offer_file(self, path, to):
        return self.files.offer_file(path, to)

    def request_file(self, offer):
        self.files.request_file(offer)

    def _connection_lost(self):
        # abort_all is inside the lock so that close(), when another thread got here
        # first, still waits until the partial files are really gone.
        with self._down_lock:
            if self._down:
                return
            self._down = True
            self.files.abort_all("Connection lost.")
        if not self._closing:
            self.inbox.put({"type": "_disconnected"})

    def close(self):
        self._closing = True
        self._outbox.put(None)
        if self.sock is not None:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.sock.close()
            self.sock = None
        # Do this here too: the process may exit before the reader thread notices.
        self._connection_lost()


class DemoConnection:
    """Fake server so the GUI can be developed and tested without the real Server."""

    CHANNELS = ["general", "random", "homework"]
    BOTS = ["alice", "bob"]
    REPLIES = ["Nice!", "lol", "Agreed.", "Can you explain that again?", "Sounds good to me", "brb"]

    def __init__(self, inbox: queue.Queue, download_dir="downloads"):
        self.inbox = inbox
        self.username = None
        self.files = FileTransfers(self, inbox, download_dir)

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
        elif kind == "file_offer":
            self.files.handle(dict(msg, sender=self.username, ts=time.time()))  # shows our own card
        elif kind == "message":
            echo = dict(msg, sender=self.username, ts=time.time())
            self.inbox.put(echo)  # the server broadcasts our own message back to us
            to = msg["to"]
            bot = to[1:] if to.startswith("@") else random.choice(self.BOTS)
            if bot in self.BOTS:
                # A DM reply arrives addressed to us; channel replies go to the channel
                reply_to = f"@{self.username}" if to.startswith("@") else to
                self._later(1.5, {"type": "message", "to": reply_to, "sender": bot,
                                  "text": random.choice(self.REPLIES), "ts": time.time()})

    def safe_send(self, msg: dict):
        self.send(msg)  # the fake server ignores everything except offers (see send)
        return True

    def offer_file(self, path, to):
        return self.files.offer_file(path, to)

    def request_file(self, offer):
        self.files.request_file(offer)

    def close(self):
        pass
