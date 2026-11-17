import argparse
import socket
import threading
import time
import json
from enum import Enum

# Server IP and Port configuration
HOST = '127.0.0.1'  # Server IP (localhost)
PORT = 65432        # Port for client connections

RECV_SIZE = 65536                     # bytes per recv(); one message may span many
MAX_LINE_BYTES = 256 * 1024           # longest message allowed; a 64 KB chunk is ~88 KB as base64
MAX_FILE_BYTES = 1024 * 1024 * 1024   # 1 GB; keep in sync with Client/file_transfer.py
SEND_TIMEOUT = 30                     # seconds before a client that stopped reading is dropped

# clients: conn -> {"username": str, "channels": set[str]}
# channels: channel_name -> set[str] of usernames currently subscribed
# user_to_conn: username -> conn
clients = {}
channels = {
    "general": set(),
    "random": set(),
    "homework": set(),
}
user_to_conn = {}

# offers: offer_id -> {"sender_conn": conn, "sender": str, "filename": str, "size": int,
#                      "allowed": set[str] of usernames, "pending": set[conn] awaiting file_start}
# The file itself stays on the sender's computer until someone asks for it.
offers = {}

# transfers: file_id -> {"sender_conn": conn, "sender": str, "filename": str, "recipients": set[conn]}
# One transfer per download: each person who clicks a file gets their own stream.
transfers = {}

# send_locks: conn -> Lock, held for each sendall. Several handler threads can write
# to one client at once; without this their bytes could interleave and break framing.
send_locks = {}

# state_lock guards the dicts above, which every handler thread shares. Only hold it
# for quick dict work, never around a send: a slow client must not stall the server.
# A thread never holds two of these locks at once, so they can't deadlock.
state_lock = threading.RLock()


def safe_send(conn, data):
    """Send already-encoded bytes as one unit. Returns False if it didn't go out."""
    lock = send_locks.get(conn)
    if lock is None:
        return False  # client already removed
    try:
        with lock:
            if send_locks.get(conn) is not lock:
                return False  # dropped while we waited for the lock
            conn.sendall(data)
        return True
    except socket.timeout:
        # The client stopped reading. Part of a message may have gone out, so the
        # stream is broken: drop them. Removing the lock stops further sends and tells
        # their handler thread to clean up (on Windows, shutdown() alone won't wake it).
        print("Dropping a client that stopped reading.")
        send_locks.pop(conn, None)
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        return False
    except (BrokenPipeError, OSError):
        return False


def send_json(conn, payload):
    safe_send(conn, (json.dumps(payload) + "\n").encode("utf-8"))


def broadcast_to_channel(channel_name, payload, exclude_conn=None):
    with state_lock:
        if channel_name not in channels:
            return
        targets = [user_to_conn.get(username) for username in channels[channel_name]]

    for target_conn in targets:
        if target_conn is None or target_conn == exclude_conn:
            continue
        send_json(target_conn, payload)


def broadcast_user_list():
    with state_lock:
        online_users = sorted(user_to_conn.keys())
        conns = list(clients.keys())
    for conn in conns:
        send_json(conn, {"type": "users", "users": online_users})


def remove_client(conn):
    with state_lock:
        if conn not in clients:
            return

        username = clients[conn].get("username")
        if username:
            user_to_conn.pop(username, None)
            for channel_name in list(clients[conn].get("channels", set())):
                if channel_name in channels:
                    channels[channel_name].discard(username)
                    if not channels[channel_name]:
                        del channels[channel_name]

        clients.pop(conn, None)

    drop_client_transfers(conn, username)
    broadcast_user_list()


def login_client(conn, addr, msg):
    username = (msg.get("username") or "").strip()
    if not username:
        send_json(conn, {"type": "login_error", "reason": "Username is required."})
        return

    # Check and claim under one lock, or two logins could both take the same name.
    with state_lock:
        taken = username in user_to_conn
        if not taken:
            clients[conn] = {"username": username, "channels": set()}
            user_to_conn[username] = conn

    if taken:
        send_json(conn, {"type": "login_error", "reason": "That username is already in use."})
        return

    # Join the default channel automatically so the app has an initial room.
    join_channel(conn, "general")

    send_json(conn, {
        "type": "login_ok",
        "username": username,
        "channels": sorted(clients[conn]["channels"]),
        "users": sorted(user_to_conn.keys()),
    })

    broadcast_user_list()


def join_channel(conn, channel_name):
    if conn not in clients:
        send_json(conn, {"type": "error", "reason": "Client is not logged in."})
        return

    channel_name = (channel_name or "").strip()
    if not channel_name:
        send_json(conn, {"type": "error", "reason": "Channel name is required."})
        return

    if not channel_name.startswith("#"):
        channel_name = f"#{channel_name}"

    with state_lock:
        username = clients[conn]["username"]
        members = channels.setdefault(channel_name, set())
        already_joined = username in members
        members.add(username)
        clients[conn]["channels"].add(channel_name)

    if already_joined:
        send_json(conn, {"type": "joined_channel", "channel": channel_name})
        return

    payload = {
        "type": "message",
        "to": channel_name,
        "sender": "server",
        "text": f"{username} joined {channel_name}",
        "ts": time.time(),
    }
    broadcast_to_channel(channel_name, payload, exclude_conn=conn)
    send_json(conn, {"type": "joined_channel", "channel": channel_name})


def leave_channel(conn, channel_name):
    if conn not in clients:
        send_json(conn, {"type": "error", "reason": "Client is not logged in."})
        return

    channel_name = (channel_name or "").strip()
    if not channel_name:
        send_json(conn, {"type": "error", "reason": "Channel name is required."})
        return

    if not channel_name.startswith("#"):
        channel_name = f"#{channel_name}"

    with state_lock:
        username = clients[conn]["username"]
        if channel_name in clients[conn]["channels"]:
            clients[conn]["channels"].remove(channel_name)

        if channel_name in channels:
            channels[channel_name].discard(username)
            if not channels[channel_name]:
                del channels[channel_name]

    payload = {
        "type": "message",
        "to": channel_name,
        "sender": "server",
        "text": f"{username} left {channel_name}",
        "ts": time.time(),
    }
    broadcast_to_channel(channel_name, payload, exclude_conn=conn)
    send_json(conn, {"type": "left_channel", "channel": channel_name})


def send_channel_message(conn, msg):
    if conn not in clients:
        send_json(conn, {"type": "error", "reason": "Client is not logged in."})
        return

    sender = clients[conn]["username"]
    channel_name = (msg.get("to") or "").strip()
    text = (msg.get("text") or "").strip()

    if not channel_name:
        send_json(conn, {"type": "error", "reason": "Channel target is required."})
        return

    if not channel_name.startswith("#"):
        channel_name = f"#{channel_name}"

    if channel_name not in channels:
        send_json(conn, {"type": "error", "reason": f"Channel {channel_name} does not exist."})
        return

    if sender not in channels[channel_name]:
        send_json(conn, {"type": "error", "reason": f"You are not subscribed to {channel_name}."})
        return

    payload = {
        "type": "message",
        "to": channel_name,
        "sender": sender,
        "text": text,
        "ts": time.time(),
    }
    broadcast_to_channel(channel_name, payload)


def send_direct_message(conn, msg):
    if conn not in clients:
        send_json(conn, {"type": "error", "reason": "Client is not logged in."})
        return

    sender = clients[conn]["username"]
    target_username = (msg.get("to") or "").strip().lstrip("@")
    text = (msg.get("text") or "").strip()

    if not target_username:
        send_json(conn, {"type": "error", "reason": "Recipient username is required."})
        return

    target_conn = user_to_conn.get(target_username)
    if target_conn is None:
        send_json(conn, {"type": "error", "reason": f"User {target_username} is not online."})
        return

    payload = {
        "type": "message",
        "to": f"@{target_username}",
        "sender": sender,
        "text": text,
        "ts": time.time(),
    }
    send_json(target_conn, payload)
    send_json(conn, payload)


def resolve_recipients(conn, target):
    """Who a file sent to target reaches: same rules as chat, minus the sender.

    Returns (set of conns, None), or (None, reason) if the file can't be sent.
    """
    with state_lock:
        if conn not in clients:
            return None, "Client is not logged in."
        sender = clients[conn]["username"]

        if target.startswith("#"):
            if target not in channels:
                return None, f"Channel {target} does not exist."
            if sender not in channels[target]:
                return None, f"You are not subscribed to {target}."
            names = channels[target] - {sender}
            if not names:
                return None, f"Nobody else is in {target}."
        elif target.startswith("@"):
            names = {target.lstrip("@")}
            if sender in names:
                return None, "You can't send a file to yourself."
            if not names <= user_to_conn.keys():
                return None, f"User {target.lstrip('@')} is not online."
        else:
            return None, "File target must start with # or @."

        return {user_to_conn[name] for name in names if name in user_to_conn}, None


def offer_file(conn, msg):
    """Show a file to the target's users. No file data moves until one of them asks."""
    offer_id = msg.get("offer_id")
    target = (msg.get("to") or "").strip()
    filename = msg.get("filename")
    size = msg.get("size")

    def reject(reason):
        send_json(conn, {"type": "file_abort", "offer_id": offer_id, "reason": reason})

    if not isinstance(offer_id, str) or not offer_id:
        send_json(conn, {"type": "error", "reason": "file_offer needs an offer_id."})
        return
    if not isinstance(filename, str) or not filename:
        reject("File name is required.")
        return
    if not isinstance(size, int) or not 0 <= size <= MAX_FILE_BYTES:
        reject(f"Files can be at most {MAX_FILE_BYTES // 1024 // 1024} MB.")
        return

    recipients, reason = resolve_recipients(conn, target)
    if recipients is None:
        reject(reason)
        return

    with state_lock:
        duplicate = offer_id in offers
        if not duplicate:
            sender = clients[conn]["username"]
            offers[offer_id] = {"sender_conn": conn, "sender": sender, "filename": filename, "size": size,
                                "allowed": {clients[c]["username"] for c in recipients if c in clients},
                                "pending": set()}
    if duplicate:
        reject("That offer_id is already in use.")
        return

    payload = {"type": "file_offer", "to": target, "sender": sender, "offer_id": offer_id,
               "filename": filename, "size": size, "ts": time.time()}
    for target_conn in recipients | {conn}:  # the echo gives the sender their own card
        send_json(target_conn, payload)


def request_file(conn, msg):
    """Someone clicked a file: ask the sender to stream it to them."""
    offer_id = msg.get("offer_id")
    with state_lock:
        offer = offers.get(offer_id) if isinstance(offer_id, str) else None
        requester = clients.get(conn, {}).get("username")
        allowed = offer is not None and requester in offer["allowed"]
        if allowed:
            offer["pending"].add(conn)

    if not allowed:
        send_json(conn, {"type": "file_abort", "offer_id": offer_id, "reason": "That file is no longer available."})
        return
    send_json(offer["sender_conn"], {"type": "file_request", "offer_id": offer_id, "requester": requester})


def start_file_transfer(conn, msg):
    """The sender is answering a file_request. Anything else is refused, so nobody can
    push a file to a user who didn't click on it."""
    file_id = msg.get("file_id")
    offer_id = msg.get("offer_id")
    target = (msg.get("to") or "").strip()

    if not isinstance(file_id, str) or not file_id:
        send_json(conn, {"type": "error", "reason": "file_start needs a file_id."})
        return

    with state_lock:
        offer = offers.get(offer_id) if isinstance(offer_id, str) else None
        target_conn = user_to_conn.get(target.lstrip("@"))
        requested = (offer is not None and offer["sender_conn"] is conn
                     and target_conn is not None and target_conn in offer["pending"])
        duplicate = file_id in transfers
        if requested and not duplicate:
            offer["pending"].discard(target_conn)
            transfers[file_id] = {"sender_conn": conn, "sender": offer["sender"],
                                  "filename": offer["filename"], "recipients": {target_conn}}

    if not requested or duplicate:
        reason = "That file_id is already in use." if requested else "Nobody asked for that file."
        send_json(conn, {"type": "file_abort", "file_id": file_id, "reason": reason})
        return

    send_json(target_conn, {"type": "file_start", "to": target, "sender": offer["sender"], "file_id": file_id,
                            "offer_id": offer_id, "filename": offer["filename"], "size": offer["size"],
                            "ts": time.time()})


def relay_file_message(conn, msg):
    """Pass a file_chunk or file_end to the recipients chosen at file_start."""
    file_id = msg.get("file_id")
    with state_lock:
        transfer = transfers.get(file_id) if isinstance(file_id, str) else None
        if transfer is None or transfer["sender_conn"] is not conn:
            # Normal after an abort: chunks already in flight just get dropped here.
            return
        targets = list(transfer["recipients"])
        if msg.get("type") == "file_end":
            del transfers[file_id]

    msg["sender"] = transfer["sender"]
    data = (json.dumps(msg) + "\n").encode("utf-8")  # encode once, however many recipients
    for target_conn in targets:
        safe_send(target_conn, data)


def abort_file_transfer(conn, msg):
    """A client gave up: the sender cancels for everyone, a recipient only for themselves."""
    file_id = msg.get("file_id")
    reason = str(msg.get("reason") or "Transfer cancelled.")
    with state_lock:
        transfer = transfers.get(file_id) if isinstance(file_id, str) else None
        if transfer is None:
            return
    if transfer["sender_conn"] is conn:
        with state_lock:
            transfers.pop(file_id, None)
        for target_conn in list(transfer["recipients"]):
            send_json(target_conn, {"type": "file_abort", "file_id": file_id, "reason": reason})
    elif conn in transfer["recipients"]:
        username = clients.get(conn, {}).get("username", "A recipient")
        drop_recipient(conn, file_id, transfer, f"{username} could not receive {transfer['filename']}: {reason}")


def drop_recipient(conn, file_id, transfer, reason):
    """Stop relaying to one recipient, and tell the sender what happened."""
    with state_lock:
        transfer["recipients"].discard(conn)
        nobody_left = not transfer["recipients"]
        if nobody_left:
            transfers.pop(file_id, None)
    if nobody_left:
        send_json(transfer["sender_conn"], {"type": "file_abort", "file_id": file_id, "reason": reason})
    else:
        send_json(transfer["sender_conn"], {"type": "error", "reason": reason})


def drop_client_transfers(conn, username):
    """A client left: withdraw their offers and abort what they were sending, and drop
    them from what they were receiving."""
    with state_lock:
        for offer in offers.values():
            offer["pending"].discard(conn)
        withdrawn = [offer_id for offer_id, offer in offers.items() if offer["sender_conn"] is conn]
        waiting = [(offer_id, offers.pop(offer_id)["pending"]) for offer_id in withdrawn]
        affected = [(file_id, transfer) for file_id, transfer in transfers.items()
                    if transfer["sender_conn"] is conn or conn in transfer["recipients"]]

    for offer_id, pending in waiting:
        for target_conn in pending:
            send_json(target_conn, {"type": "file_abort", "offer_id": offer_id,
                                    "reason": f"{username} went offline, so the file is no longer available."})

    for file_id, transfer in affected:
        if transfer["sender_conn"] is conn:
            with state_lock:
                transfers.pop(file_id, None)
            reason = f"{username} disconnected before finishing {transfer['filename']}."
            for target_conn in list(transfer["recipients"]):
                send_json(target_conn, {"type": "file_abort", "file_id": file_id, "reason": reason})
        else:
            drop_recipient(conn, file_id, transfer,
                           f"{username} disconnected before receiving {transfer['filename']}.")


def handle_client_request(conn, addr, line):
    """Handle one complete message (one line, without its newline)."""
    line = line.strip()
    if not line:
        return

    try:
        msg = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        send_json(conn, {"type": "error", "reason": "Malformed JSON message."})
        return
    if not isinstance(msg, dict):
        send_json(conn, {"type": "error", "reason": "Malformed JSON message."})
        return

    msg_type = msg.get("type")

    if msg_type == "login":
        login_client(conn, addr, msg)
    elif msg_type == "join_channel":
        join_channel(conn, msg.get("channel"))
    elif msg_type == "leave_channel":
        leave_channel(conn, msg.get("channel"))
    elif msg_type == "message":
        target = (msg.get("to") or "").strip()
        if not target:
            send_json(conn, {"type": "error", "reason": "Message target is required."})
            return

        if target.startswith("#"):
            send_channel_message(conn, msg)
        elif target.startswith("@"):
            send_direct_message(conn, msg)
        else:
            send_json(conn, {"type": "error", "reason": "Message target must start with # or @."})
    elif msg_type == "file_offer":
        offer_file(conn, msg)
    elif msg_type == "file_request":
        request_file(conn, msg)
    elif msg_type == "file_start":
        start_file_transfer(conn, msg)
    elif msg_type in ("file_chunk", "file_end"):
        relay_file_message(conn, msg)
    elif msg_type == "file_abort":
        abort_file_transfer(conn, msg)
    else:
        send_json(conn, {"type": "error", "reason": f"Unsupported message type: {msg_type}"})


# Function to manage client connections
def handle_client(conn, addr):
    print(f"Connection established with {addr}.")
    send_locks[conn] = threading.Lock()
    # sendall() needs a timeout to spot a client that stopped reading. recv() shares
    # it, but timing out there only means the client has been quiet, which is fine.
    conn.settimeout(SEND_TIMEOUT)

    # TCP is a byte stream: one recv() can hold part of a message or several of them.
    # Keep whatever arrives here and only act on complete, newline-terminated lines.
    buffer = bytearray()
    try:
        while True:
            try:
                data = conn.recv(RECV_SIZE)
            except socket.timeout:
                if conn not in send_locks:
                    break  # safe_send gave up on this client
                continue
            if not data:
                break

            buffer += data
            while (end := buffer.find(b"\n")) != -1:
                line = bytes(buffer[:end])
                del buffer[:end + 1]
                handle_client_request(conn, addr, line)

            if len(buffer) > MAX_LINE_BYTES:
                send_json(conn, {"type": "error", "reason": "Message too long."})
                break
    except ConnectionResetError:
        print(f"Client {addr} has disconnected.")
    except OSError:
        print(f"Socket error for {addr}.")
    finally:
        # In finally so that a bug in a handler can't leave a ghost user online.
        remove_client(conn)
        send_locks.pop(conn, None)
        conn.close()
        print(f"Connection with {addr} closed.")


# Start the server
def start_server(host=HOST, port=PORT):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
        server_socket.bind((host, port))
        server_socket.listen()
        print(f"Server listening on {host}:{port}...")

        while True:
            conn, addr = server_socket.accept()
            thread = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
            thread.start()
            print(f"Active connections: {threading.active_count() - 1}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Chatapp server")
    parser.add_argument("--host", default=HOST, help="use 0.0.0.0 to accept other computers")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    start_server(args.host, args.port)
