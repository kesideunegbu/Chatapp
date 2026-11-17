"""File sharing for the chat client.

Sharing a file only sends an offer, which shows up as a card in the chat. The file
stays on the sender's computer until someone clicks its card; the sender then streams
it to that one person. Nothing is ever written to a receiver's disk without a click.

Files are streamed as many small JSON lines instead of one huge one (see the
protocol notes at the top of network.py). That keeps memory use constant no
matter how big the file is, and lets chat messages travel between chunks.

Threads:
    * one background thread per download being served reads and sends its chunks
    * the connection's reader thread decodes incoming chunks and writes them to
      disk. They deliberately skip the GUI inbox: the GUI only polls every 50 ms,
      so chunks would pile up in memory. Doing the work here also gives us
      backpressure, because a slow disk stops the reader, which fills the TCP
      window, which slows the sender down.

The GUI sees file_offer messages (plus "path" on the sender's own), and these local events:
    {"type": "_file_progress", "file_id", "offer_id", "direction": "send"|"recv", "filename", "done", "size"}
    {"type": "_file_done",     "file_id", "offer_id", "direction", "filename", "size", "to", "sender",
                               "requester" (send only), "seconds", "path" (recv only), "verified" (recv only)}
    {"type": "_file_failed",   "file_id", "offer_id", "direction", "filename", "to", "sender", "reason"}
"to" is always where the offer was made, so results appear in that conversation.
"""

import base64
import binascii
import hashlib
import os
import re
import threading
import time
import uuid

CHUNK_SIZE = 64 * 1024  # bytes of file data per file_chunk message
MAX_FILE_BYTES = 1024 * 1024 * 1024  # 1 GB; keep in sync with Server/main.py
PROGRESS_INTERVAL = 0.1  # seconds between progress events, so the GUI isn't flooded

_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"{p}{i}" for p in ("COM", "LPT") for i in range(1, 10)}


def sanitize_filename(name):
    """Reduce a name supplied by another user to a safe bare filename."""
    # basename() only splits on the local OS separator, so normalize first:
    # a Linux receiver must still strip the folders from "..\\..\\evil.exe".
    name = os.path.basename(str(name).replace("\\", "/"))
    name = _UNSAFE_CHARS.sub("_", name).rstrip(". ")  # Windows drops trailing dots and spaces
    if not name:
        return "file"
    if name.split(".")[0].upper() in _WINDOWS_RESERVED:
        name = "_" + name
    return name


def _open_unique(folder, name):
    """Create "<name>.part" under a name not yet taken, adding " (1)" etc. if needed.

    Returns (open file, part path, final path). Mode "xb" makes the claim atomic,
    so two transfers of the same name can never end up sharing a file.
    """
    stem, ext = os.path.splitext(name)
    n = 0
    while True:
        candidate = os.path.join(folder, f"{stem} ({n}){ext}" if n else name)
        part = candidate + ".part"
        if not os.path.exists(candidate):
            try:
                return open(part, "xb"), part, candidate
            except FileExistsError:
                pass
        n += 1


class FileTransfers:
    """Tracks every file this client is sending or receiving."""

    def __init__(self, conn, inbox, download_dir):
        self.conn = conn  # needs safe_send(msg) -> bool
        self.inbox = inbox
        self.download_dir = download_dir
        self._lock = threading.Lock()  # guards the dicts below and open receive files
        self._offers = {}     # offer_id -> {"path", "to", "filename", "size"}: files we shared
        self._requested = {}  # offer_id -> the offer: files we clicked, awaiting file_start
        self._sending = {}    # file_id -> {"cancel": threading.Event, "info": dict}
        self._receiving = {}  # file_id -> dict of state for an incoming file
        self._closed = False  # set by abort_all; a dead connection takes no new files

    # ---------- sending ----------

    def offer_file(self, path, to):
        """Share a file: it stays on disk here until someone asks for it.

        Raises OSError/ValueError if it can't be shared.
        """
        size = os.path.getsize(path)
        if size > MAX_FILE_BYTES:
            raise ValueError(f"File is {size / 1024 / 1024:.1f} MB; "
                             f"the limit is {MAX_FILE_BYTES // 1024 // 1024} MB.")
        offer_id = uuid.uuid4().hex
        filename = os.path.basename(path)
        with self._lock:
            self._offers[offer_id] = {"path": path, "to": to, "filename": filename, "size": size}
        self.conn.send({"type": "file_offer", "to": to, "offer_id": offer_id,
                        "filename": filename, "size": size})
        return offer_id

    def _on_offer(self, msg):
        with self._lock:
            mine = self._offers.get(msg.get("offer_id"))
        self.inbox.put(dict(msg, path=mine["path"]) if mine else msg)

    def _on_request(self, msg):
        """Someone clicked our file: stream it to them on its own thread."""
        offer_id = msg.get("offer_id")
        requester = msg.get("requester")
        with self._lock:
            offer = self._offers.get(offer_id)
            if offer is None:
                return
            busy = any(e["info"]["offer_id"] == offer_id and e["info"]["requester"] == requester
                       for e in self._sending.values())
            if busy:
                return  # they're already getting it
            file_id = uuid.uuid4().hex
            info = {"file_id": file_id, "offer_id": offer_id, "direction": "send",
                    "filename": offer["filename"], "to": offer["to"], "requester": requester}
            cancel = threading.Event()
            self._sending[file_id] = {"cancel": cancel, "info": info}
        self.inbox.put(dict(info, type="_file_progress", done=0, size=offer["size"]))
        threading.Thread(target=self._send_worker,
                         args=(offer["path"], f"@{requester}", file_id, offer["size"], cancel, info),
                         daemon=True).start()

    def _send_worker(self, path, to, file_id, size, cancel, info):
        started = time.perf_counter()
        try:
            sent = self._stream_file(path, to, file_id, size, cancel, info)
        except OSError as e:
            # The file couldn't be read, e.g. it was deleted part way through.
            self.conn.safe_send({"type": "file_abort", "file_id": file_id, "reason": "Sender could not read the file."})
            self._end_send(file_id, type="_file_failed", reason=str(e))
            return
        if sent:
            self._end_send(file_id, type="_file_done", size=size, seconds=time.perf_counter() - started)
        else:
            self._end_send(file_id, type="_file_failed", reason="Connection lost.")

    def _stream_file(self, path, to, file_id, size, cancel, info):
        """Send start, chunks, end. Returns True if the whole file went out."""
        digest = hashlib.sha256()
        start_msg = {"type": "file_start", "to": to, "file_id": file_id, "offer_id": info["offer_id"]}
        if not self.conn.safe_send(start_msg):
            return False

        done = seq = 0
        last_progress = 0.0
        with open(path, "rb") as f:
            while chunk := f.read(CHUNK_SIZE):  # one chunk in memory at a time
                if cancel.is_set():
                    return False
                digest.update(chunk)
                data = base64.b64encode(chunk).decode("ascii")
                if not self.conn.safe_send({"type": "file_chunk", "to": to, "file_id": file_id,
                                            "seq": seq, "data": data}):
                    return False
                seq += 1
                done += len(chunk)
                now = time.perf_counter()
                if now - last_progress >= PROGRESS_INTERVAL:
                    last_progress = now
                    self.inbox.put(dict(info, type="_file_progress", done=done, size=size))

        if done != size:
            raise OSError("The file changed while it was being sent.")
        return self.conn.safe_send({"type": "file_end", "to": to, "file_id": file_id,
                                    "sha256": digest.hexdigest()})

    def _end_send(self, file_id, **event):
        """Report a send's outcome. Only the first caller wins, so the GUI gets exactly one
        final event even when the worker, an abort and a disconnect all race to finish it."""
        with self._lock:
            entry = self._sending.pop(file_id, None)
        if entry is None:
            return False
        entry["cancel"].set()
        self.inbox.put(dict(entry["info"], **event))
        return True

    # ---------- receiving ----------

    def request_file(self, offer):
        """The user clicked a file: ask for it. offer is the file_offer message."""
        with self._lock:
            self._requested[offer["offer_id"]] = offer
        self.conn.send({"type": "file_request", "offer_id": offer["offer_id"]})

    def handle(self, msg):
        """Called on the reader thread. Returns True if msg was a file message."""
        handler = {"file_offer": self._on_offer, "file_request": self._on_request,
                   "file_start": self._on_start, "file_chunk": self._on_chunk,
                   "file_end": self._on_end, "file_abort": self._on_abort}.get(msg.get("type"))
        if handler is None:
            return False
        handler(msg)
        return True

    def _on_start(self, msg):
        file_id = msg.get("file_id")
        offer_id = msg.get("offer_id")
        with self._lock:
            offer = self._requested.get(offer_id)
            if offer is not None and offer.get("sender") == msg.get("sender"):
                del self._requested[offer_id]
            else:
                offer = None
        if offer is None:
            # We never clicked this. The server should have stopped it, but disk
            # writes are too important to rely on that alone.
            self.conn.send({"type": "file_abort", "file_id": file_id, "reason": "File was not requested."})
            return

        size = msg.get("size")
        filename = sanitize_filename(msg.get("filename", "file"))
        info = {"file_id": file_id, "offer_id": offer_id, "direction": "recv", "filename": filename,
                "to": offer.get("to", ""), "sender": offer.get("sender", "?")}
        if not isinstance(size, int) or not 0 <= size <= MAX_FILE_BYTES:
            self._refuse(info, "Incoming file has an invalid size.")
            return
        try:
            os.makedirs(self.download_dir, exist_ok=True)
            f, part, final = _open_unique(self.download_dir, filename)
        except OSError as e:
            self._refuse(info, f"Could not create the file: {e}")
            return
        t = dict(info, file=f, part=part, final=final, size=size, seq=0, done=0,
                 sha256=hashlib.sha256(), started=time.perf_counter(), last_progress=0.0)
        with self._lock:
            # A file_start still buffered at disconnect arrives after abort_all has
            # run, so nothing would ever clean up its partial file.
            closed = self._closed
            if not closed:
                self._receiving[file_id] = t
        if closed:
            self._discard(t)
            return
        self.inbox.put(dict(info, type="_file_progress", done=0, size=size))

    def _on_chunk(self, msg):
        file_id = msg.get("file_id")
        progress = None
        with self._lock:
            t = self._receiving.get(file_id)
            if t is None:
                return  # already aborted (or we joined late); chunks in flight are expected
            try:
                if msg.get("seq") != t["seq"]:
                    raise ValueError(f"expected chunk {t['seq']}, got {msg.get('seq')}")
                chunk = base64.b64decode(msg.get("data", ""), validate=True)
                if t["done"] + len(chunk) > t["size"]:
                    raise ValueError("more data than the announced size")
                t["file"].write(chunk)
            except (OSError, ValueError, binascii.Error) as e:
                failure = f"Could not save the file: {e}"
            else:
                failure = None
                t["sha256"].update(chunk)
                t["seq"] += 1
                t["done"] += len(chunk)
                now = time.perf_counter()
                if now - t["last_progress"] >= PROGRESS_INTERVAL:
                    t["last_progress"] = now
                    progress = {"done": t["done"], "size": t["size"]}
        if failure:
            self._fail_receive(file_id, failure, tell_server=True)
        elif progress:
            self.inbox.put(dict(self._public(t), type="_file_progress", **progress))

    def _on_end(self, msg):
        file_id = msg.get("file_id")
        with self._lock:
            t = self._receiving.pop(file_id, None)
            if t is None:
                return
            verified = t["done"] == t["size"] and t["sha256"].hexdigest() == msg.get("sha256")
            result = dict(self._public(t), size=t["size"], seconds=time.perf_counter() - t["started"])
            try:
                t["file"].close()
                if verified:
                    os.replace(t["part"], t["final"])
                else:
                    os.remove(t["part"])
            except OSError as e:
                self._delete_quietly(t["part"])
                self.inbox.put(dict(self._public(t), type="_file_failed", reason=f"Could not save the file: {e}"))
                return
        if verified:
            self.inbox.put(dict(result, type="_file_done", verified=True, path=t["final"]))
        else:
            self.inbox.put(dict(result, type="_file_failed", verified=False,
                                reason="integrity check failed, so the file was deleted."))

    def _on_abort(self, msg):
        file_id = msg.get("file_id")
        reason = msg.get("reason", "Transfer cancelled.")
        if "offer_id" in msg:
            self._fail_offer(msg["offer_id"], reason)
        elif not self._end_send(file_id, type="_file_failed", reason=reason):
            self._fail_receive(file_id, reason)

    def _fail_offer(self, offer_id, reason):
        """An offer of ours was refused, or a file we asked for can't be delivered."""
        with self._lock:
            requested = self._requested.pop(offer_id, None)
            mine = None if requested else self._offers.pop(offer_id, None)
        if requested:
            self.inbox.put({"type": "_file_failed", "file_id": offer_id, "offer_id": offer_id, "direction": "recv",
                            "filename": requested.get("filename"), "to": requested.get("to", ""),
                            "sender": requested.get("sender"), "reason": reason})
        elif mine:
            self.inbox.put({"type": "_file_failed", "file_id": offer_id, "offer_id": offer_id, "direction": "send",
                            "filename": mine["filename"], "to": mine["to"], "reason": reason})

    def _fail_receive(self, file_id, reason, tell_server=False):
        """Drop an incoming transfer: close and delete the partial file, then tell the GUI."""
        with self._lock:
            t = self._receiving.pop(file_id, None)
            if t is None:
                return
            self._discard(t)
        self.inbox.put(dict(self._public(t), type="_file_failed", reason=reason))
        if tell_server:
            # So the server stops relaying chunks that we would only throw away.
            self.conn.safe_send({"type": "file_abort", "file_id": file_id, "reason": reason})

    def _refuse(self, info, reason):
        self.inbox.put(dict(info, type="_file_failed", reason=reason))
        self.conn.safe_send({"type": "file_abort", "file_id": info["file_id"], "reason": reason})

    @staticmethod
    def _public(t):
        return {k: t[k] for k in ("file_id", "offer_id", "direction", "filename", "to", "sender")}

    def _discard(self, t):
        try:
            t["file"].close()
        except OSError:
            pass
        self._delete_quietly(t["part"])

    @staticmethod
    def _delete_quietly(path):
        try:
            os.remove(path)
        except OSError:
            pass

    # ---------- shutdown ----------

    def abort_all(self, reason):
        """Connection is gone: stop every send and delete every partial download."""
        with self._lock:
            self._closed = True
            sending = list(self._sending)
            receiving = list(self._receiving)
            requested = list(self._requested)
        for file_id in sending:
            self._end_send(file_id, type="_file_failed", reason=reason)
        for file_id in receiving:
            self._fail_receive(file_id, reason)
        for offer_id in requested:
            self._fail_offer(offer_id, reason)
