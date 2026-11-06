import socket
import threading
import time
import json
from enum import Enum

# Server IP and Port configuration
HOST = '127.0.0.1'  # Server IP (localhost)
PORT = 65432        # Port for client connections

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


def send_json(conn, payload):
    try:
        conn.sendall((json.dumps(payload) + "\n").encode("utf-8"))
    except (BrokenPipeError, OSError):
        pass


def broadcast_to_channel(channel_name, payload, exclude_conn=None):
    if channel_name not in channels:
        return

    for username in list(channels[channel_name]):
        target_conn = user_to_conn.get(username)
        if target_conn is None or target_conn == exclude_conn:
            continue
        send_json(target_conn, payload)


def broadcast_user_list():
    online_users = sorted(user_to_conn.keys())
    for conn in list(clients.keys()):
        send_json(conn, {"type": "users", "users": online_users})


def remove_client(conn):
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
    broadcast_user_list()


def login_client(conn, addr, msg):
    username = (msg.get("username") or "").strip()
    if not username:
        send_json(conn, {"type": "login_error", "reason": "Username is required."})
        return

    if username in user_to_conn:
        send_json(conn, {"type": "login_error", "reason": "That username is already in use."})
        return

    clients[conn] = {"username": username, "channels": set()}
    user_to_conn[username] = conn

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

    username = clients[conn]["username"]
    channels.setdefault(channel_name, set())

    if username in channels[channel_name]:
        send_json(conn, {"type": "joined_channel", "channel": channel_name})
        return

    channels[channel_name].add(username)
    clients[conn]["channels"].add(channel_name)

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


def handle_client_request(conn, addr, data):
    raw_text = data.decode("utf-8", errors="replace")
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue

        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            send_json(conn, {"type": "error", "reason": "Malformed JSON message."})
            continue

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
                continue

            if target.startswith("#"):
                send_channel_message(conn, msg)
            elif target.startswith("@"):
                send_direct_message(conn, msg)
            else:
                send_json(conn, {"type": "error", "reason": "Message target must start with # or @."})
        else:
            send_json(conn, {"type": "error", "reason": f"Unsupported message type: {msg_type}"})


# Function to manage client connections
def handle_client(conn, addr):
    print(f"Connection established with {addr}.")
    while True:
        try:
            data = conn.recv(1024)
            if not data:
                break

            print(f"Received from {addr}: {data.decode('utf-8', errors='replace')}")
            handle_client_request(conn, addr, data)
        except ConnectionResetError:
            print(f"Client {addr} has disconnected.")
            break
        except OSError:
            print(f"Socket error for {addr}.")
            break

    remove_client(conn)
    conn.close()
    print(f"Connection with {addr} closed.")


# Start the server
def start_server():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
        server_socket.bind((HOST, PORT))
        server_socket.listen()
        print(f"Server listening on {HOST}:{PORT}...")

        while True:
            conn, addr = server_socket.accept()
            thread = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
            thread.start()
            print(f"Active connections: {threading.active_count() - 1}")


if __name__ == "__main__":
    start_server()
