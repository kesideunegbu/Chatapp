"""Tkinter GUI for the chat client: login screen and main chat window."""

import os
import queue
import subprocess
import sys
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from network import DemoConnection, TCPConnection

POLL_MS = 50  # how often the GUI checks for incoming network events

# Opening one of these runs it, so a received file gets a warning first
RUNNABLE_TYPES = {".exe", ".msi", ".bat", ".cmd", ".com", ".scr", ".pif", ".lnk", ".reg", ".ps1", ".vbs",
                  ".vbe", ".js", ".jse", ".wsf", ".wsh", ".hta", ".cpl", ".jar", ".py", ".pyw", ".sh"}

FILE_ICONS = {"🖼": (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg"),
              "🎞": (".mp4", ".mov", ".avi", ".mkv", ".webm", ".mp3", ".wav"),
              "📦": (".zip", ".rar", ".7z", ".tar", ".gz")}

COLORS = {
    "bg": "#1e1f22",
    "sidebar": "#2b2d31",
    "panel": "#313338",
    "text": "#dbdee1",
    "muted": "#949ba4",
    "accent": "#5865f2",
    "me": "#57f287",
    "other": "#00a8fc",
    "system": "#faa61a",
    "error": "#ed4245",
}


class ChatApp(tk.Tk):
    def __init__(self, host="127.0.0.1", port=5000, demo=False, download_dir="downloads"):
        super().__init__()
        self.title("Chatapp")
        self.geometry("900x600")
        self.minsize(640, 420)
        self.configure(bg=COLORS["bg"])
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.demo = demo
        self.download_dir = download_dir
        self.inbox = queue.Queue()
        self.conn = None
        self.username = None

        self._setup_styles()
        self.login_frame = LoginFrame(self, host, port, demo)
        self.chat_frame = None
        self.login_frame.pack(fill="both", expand=True)

        self._poll_id = self.after(POLL_MS, self._poll)

    def _setup_styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background=COLORS["bg"])
        style.configure("Card.TFrame", background=COLORS["sidebar"])
        style.configure("TLabel", background=COLORS["sidebar"], foreground=COLORS["text"])
        style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"))
        style.configure("Muted.TLabel", foreground=COLORS["muted"])
        style.configure("Error.TLabel", foreground=COLORS["error"])
        style.configure("TButton", background=COLORS["accent"], foreground="white", borderwidth=0, padding=6)
        style.map("TButton", background=[("active", "#4752c4"), ("disabled", "#3c3f45")])
        style.configure("TEntry", fieldbackground=COLORS["panel"], foreground=COLORS["text"],
                        insertcolor=COLORS["text"], borderwidth=0, padding=6)
        style.configure("TCheckbutton", background=COLORS["sidebar"], foreground=COLORS["text"])
        style.map("TCheckbutton", background=[("active", COLORS["sidebar"])])
        style.configure("Transfer.Horizontal.TProgressbar", background=COLORS["accent"],
                        troughcolor=COLORS["bg"], borderwidth=0)

    # ---------- connection lifecycle ----------

    def start_login(self, host, port, username, password, demo):
        self.username = username
        self._pending_login = {"type": "login", "username": username, "password": password}
        connection = DemoConnection if demo else TCPConnection
        self.conn = connection(self.inbox, self.download_dir)
        self.conn.connect(host, port)

    def _poll(self):
        """Drain network events on the GUI thread (Tkinter is not thread-safe)."""
        try:
            while True:
                self._handle(self.inbox.get_nowait())
        except queue.Empty:
            pass
        self._poll_id = self.after(POLL_MS, self._poll)

    def _handle(self, ev):
        kind = ev.get("type")
        if kind == "_connected":
            self.conn.send(self._pending_login)
        elif kind == "_connect_failed":
            self.login_frame.show_error(f"Could not connect: {ev.get('reason')}")
        elif kind == "login_ok":
            self.username = ev.get("username", self.username)
            self._show_chat(ev.get("channels", []), ev.get("users", []))
        elif kind == "login_error":
            self.login_frame.show_error(ev.get("reason", "Login failed."))
            self._drop_connection()
        elif kind == "_disconnected":
            if self.chat_frame:
                self.chat_frame.system_message("Disconnected from server.", error=True)
                self.chat_frame.set_connected(False)
            else:
                self.login_frame.show_error("Connection closed by server.")
            self._drop_connection()
        elif self.chat_frame:
            self.chat_frame.handle_event(ev)

    def _drop_connection(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    def _show_chat(self, channels, users):
        self.login_frame.pack_forget()
        self.chat_frame = ChatFrame(self, self.username, channels, users)
        self.chat_frame.pack(fill="both", expand=True)
        self.title(f"Chatapp - {self.username}")

    def send(self, msg):
        if self.conn:
            self.conn.send(msg)

    def offer_file(self, path, to):
        if self.conn:
            self.conn.offer_file(path, to)

    def request_file(self, offer):
        if self.conn:
            self.conn.request_file(offer)

    def logout(self):
        self._drop_connection()
        if self.chat_frame:
            self.chat_frame.destroy()
            self.chat_frame = None
        self.title("Chatapp")
        self.login_frame.reset()
        self.login_frame.pack(fill="both", expand=True)

    def on_close(self):
        self.after_cancel(self._poll_id)
        self._drop_connection()
        self.destroy()


class LoginFrame(ttk.Frame):
    def __init__(self, app, host, port, demo):
        super().__init__(app)
        self.app = app

        card = ttk.Frame(self, style="Card.TFrame", padding=30)
        card.place(relx=0.5, rely=0.5, anchor="center")

        ttk.Label(card, text="Welcome back!", style="Title.TLabel").grid(row=0, column=0, columnspan=2, pady=(0, 4))
        ttk.Label(card, text="Log in to start chatting", style="Muted.TLabel").grid(row=1, column=0, columnspan=2, pady=(0, 16))

        self.username = tk.StringVar()
        self.password = tk.StringVar()
        self.host = tk.StringVar(value=host)
        self.port = tk.StringVar(value=str(port))
        self.demo = tk.BooleanVar(value=demo)

        fields = [("Username", self.username, None), ("Password", self.password, "*"),
                  ("Server", self.host, None), ("Port", self.port, None)]
        self.entries = []
        for i, (label, var, show) in enumerate(fields, start=2):
            ttk.Label(card, text=label).grid(row=i, column=0, sticky="w", padx=(0, 10), pady=4)
            e = ttk.Entry(card, textvariable=var, show=show or "", width=28)
            e.grid(row=i, column=1, pady=4)
            e.bind("<Return>", lambda _e: self.submit())
            self.entries.append(e)

        ttk.Checkbutton(card, text="Demo mode (no server needed)", variable=self.demo).grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))

        self.button = ttk.Button(card, text="Log in", command=self.submit)
        self.button.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(16, 4))

        self.status = ttk.Label(card, text="", style="Error.TLabel", wraplength=300)
        self.status.grid(row=8, column=0, columnspan=2)

        self.entries[0].focus_set()

    def submit(self):
        username = self.username.get().strip()
        if not username:
            return self.show_error("Please enter a username.")
        if not self.port.get().strip().isdigit():
            return self.show_error("Port must be a number.")
        self.status.configure(text="Connecting...", style="Muted.TLabel")
        self.button.state(["disabled"])
        self.app.start_login(self.host.get().strip(), int(self.port.get()), username,
                             self.password.get(), self.demo.get())

    def show_error(self, text):
        self.status.configure(text=text, style="Error.TLabel")
        self.button.state(["!disabled"])

    def reset(self):
        self.password.set("")
        self.status.configure(text="")
        self.button.state(["!disabled"])


class ChatFrame(ttk.Frame):
    def __init__(self, app, username, channels, users):
        super().__init__(app)
        self.app = app
        self.username = username
        self.history = {}  # conversation key ("#chan" / "@user") -> list of events
        self.unread = {}
        self.channels = [f"#{c.lstrip('#')}" for c in channels]
        self.users = []
        self.current = self.channels[0] if self.channels else None
        self.cards = {}  # offer_id -> the offer, plus "state" and "path" (see _card_action)
        self.card_widgets = {}  # offer_id -> widgets of its card, if it's on screen
        self.transfer_rows = {}  # file_id -> widgets of its progress row
        self._finished_transfers = set()  # file_ids that must not get a new row

        self._build_sidebar()
        self._build_chat_area()
        self.set_users(users)
        self._refresh_sidebar()
        self._render_current()

    # ---------- layout ----------

    def _build_sidebar(self):
        side = tk.Frame(self, bg=COLORS["sidebar"], width=220)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)

        lb_opts = dict(bg=COLORS["sidebar"], fg=COLORS["text"], selectbackground=COLORS["panel"],
                       selectforeground="white", highlightthickness=0, borderwidth=0,
                       activestyle="none", font=("Segoe UI", 11), exportselection=False)

        tk.Label(side, text="CHANNELS", bg=COLORS["sidebar"], fg=COLORS["muted"],
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(fill="x", padx=12, pady=(12, 2))
        self.channel_list = tk.Listbox(side, height=8, **lb_opts)
        self.channel_list.pack(fill="x", padx=8)
        self.channel_list.bind("<<ListboxSelect>>", lambda _e: self._on_select(self.channel_list, self.channels))

        tk.Label(side, text="ONLINE USERS", bg=COLORS["sidebar"], fg=COLORS["muted"],
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(fill="x", padx=12, pady=(16, 2))
        self.user_list = tk.Listbox(side, **lb_opts)
        self.user_list.pack(fill="both", expand=True, padx=8)
        self.user_list.bind("<<ListboxSelect>>", lambda _e: self._on_select(self.user_list, self.users))

        footer = tk.Frame(side, bg="#232428")
        footer.pack(fill="x", side="bottom")
        tk.Label(footer, text=f"● {self.username}", bg="#232428", fg=COLORS["me"],
                 font=("Segoe UI", 10, "bold")).pack(side="left", padx=10, pady=8)
        tk.Button(footer, text="Log out", command=self.app.logout, bg="#232428", fg=COLORS["muted"],
                  activebackground=COLORS["panel"], activeforeground="white", relief="flat",
                  cursor="hand2").pack(side="right", padx=8)

    def _build_chat_area(self):
        main = tk.Frame(self, bg=COLORS["panel"])
        main.pack(side="left", fill="both", expand=True)

        self.header = tk.Label(main, text="", bg=COLORS["panel"], fg="white", anchor="w",
                               font=("Segoe UI", 13, "bold"), padx=16, pady=10)
        self.header.pack(fill="x")
        tk.Frame(main, bg=COLORS["bg"], height=1).pack(fill="x")

        body = tk.Frame(main, bg=COLORS["panel"])
        body.pack(fill="both", expand=True)
        # height=1 just means "ask for little": it still expands to fill the window,
        # but no longer pushes the progress rows and input bar off the bottom.
        self.display = tk.Text(body, bg=COLORS["panel"], fg=COLORS["text"], wrap="word", height=1,
                               borderwidth=0, highlightthickness=0, padx=16, pady=10,
                               font=("Segoe UI", 11), state="disabled", cursor="arrow")
        scroll = ttk.Scrollbar(body, command=self.display.yview)
        self.display.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.display.pack(side="left", fill="both", expand=True)

        self.display.tag_configure("time", foreground=COLORS["muted"], font=("Segoe UI", 9))
        self.display.tag_configure("me", foreground=COLORS["me"], font=("Segoe UI", 11, "bold"))
        self.display.tag_configure("other", foreground=COLORS["other"], font=("Segoe UI", 11, "bold"))
        self.display.tag_configure("system", foreground=COLORS["system"], font=("Segoe UI", 10, "italic"))
        self.display.tag_configure("error", foreground=COLORS["error"], font=("Segoe UI", 10, "italic"))

        # Packed only while a transfer is running (see _update_transfer)
        self.transfer_panel = tk.Frame(main, bg=COLORS["panel"])

        bar = tk.Frame(main, bg=COLORS["panel"])
        bar.pack(fill="x", padx=16, pady=(0, 16))
        self.input_bar = bar
        inner = tk.Frame(bar, bg="#383a40")
        inner.pack(fill="x")

        self.file_button = tk.Button(inner, text="＋", command=self.pick_file, bg="#383a40", fg=COLORS["text"],
                                     activebackground=COLORS["panel"], activeforeground="white", relief="flat",
                                     font=("Segoe UI", 13, "bold"), width=3, cursor="hand2")
        self.file_button.pack(side="left", padx=(4, 0))

        self.entry = tk.Entry(inner, bg="#383a40", fg=COLORS["text"], insertbackground=COLORS["text"],
                              relief="flat", font=("Segoe UI", 11))
        self.entry.pack(side="left", fill="x", expand=True, ipady=10, padx=8)
        self.entry.bind("<Return>", lambda _e: self.send_message())

        self.send_button = ttk.Button(inner, text="Send", command=self.send_message)
        self.send_button.pack(side="right", padx=6, pady=6)
        self.entry.focus_set()

    # ---------- sidebar ----------

    def set_users(self, users):
        self.users = [f"@{u}" for u in users if u != self.username]
        self._refresh_sidebar()

    def _refresh_sidebar(self):
        for listbox, keys in ((self.channel_list, self.channels), (self.user_list, self.users)):
            listbox.delete(0, "end")
            for i, key in enumerate(keys):
                label = ("# " if key.startswith("#") else "● ") + key[1:]
                count = self.unread.get(key, 0)
                if count:
                    label += f"  ({count})"
                listbox.insert("end", label)
                if key == self.current:
                    listbox.selection_set(i)
                if count:
                    listbox.itemconfigure(i, fg="white")

    def _on_select(self, listbox, keys):
        sel = listbox.curselection()
        if not sel:
            return
        self.switch_to(keys[sel[0]])

    def switch_to(self, key):
        self.current = key
        self.unread.pop(key, None)
        self._refresh_sidebar()
        self._render_current()
        self.entry.focus_set()

    # ---------- message display ----------

    def _conversation_key(self, ev):
        to = ev.get("to", "")
        if to.startswith("@"):
            # A DM: file it under the other person's name
            sender = ev.get("sender")
            return to if sender == self.username else f"@{sender}"
        return to if to.startswith("#") else f"#{to}"

    def handle_event(self, ev):
        kind = ev.get("type")
        if kind == "message":
            self._add_to_conversation(self._conversation_key(ev), ev)
        elif kind == "users":
            self.set_users(ev.get("users", []))
        elif kind == "error":
            self.system_message(ev.get("reason", "Server error."), error=True)
        elif kind == "file_offer":
            self._add_card(ev)
        elif kind == "_file_progress":
            self._update_transfer(ev)
        elif kind in ("_file_done", "_file_failed"):
            self._finish_transfer(ev)

    def _add_to_conversation(self, key, ev):
        self.history.setdefault(key, []).append(ev)
        if key.startswith("#") and key not in self.channels:
            self.channels.append(key)
        if key.startswith("@") and key not in self.users:
            self.users.append(key)
        if key == self.current:
            self._append(ev)
        else:
            self.unread[key] = self.unread.get(key, 0) + 1
        self._refresh_sidebar()

    def _render_current(self):
        self.header.configure(text=self._title(self.current) if self.current else "No conversation selected")
        self.display.configure(state="normal")
        self.display.delete("1.0", "end")
        self.display.configure(state="disabled")
        for widgets in self.card_widgets.values():
            widgets["frame"].destroy()
        self.card_widgets.clear()
        for ev in self.history.get(self.current, []):
            self._append(ev)

    def _title(self, key):
        return f"# {key[1:]}" if key.startswith("#") else f"@ {key[1:]}  (direct message)"

    def _append(self, ev):
        d = self.display
        d.configure(state="normal")
        if ev.get("type") == "system":
            d.insert("end", ev["text"] + "\n", "error" if ev.get("error") else "system")
        else:
            sender = ev.get("sender", "?")
            stamp = time.strftime("%H:%M", time.localtime(ev.get("ts") or time.time()))
            d.insert("end", f"{stamp}  ", "time")
            d.insert("end", sender, "me" if sender == self.username else "other")
            if ev.get("type") == "file_offer":
                d.insert("end", "\n")
                d.window_create("end", window=self._make_card(ev["offer_id"]), pady=4)
                d.insert("end", "\n")
                d.update_idletasks()  # the card needs a height before see("end") can scroll past it
            else:
                d.insert("end", f"\n{ev.get('text', '')}\n")
        d.insert("end", "\n")
        d.configure(state="disabled")
        d.see("end")

    def system_message(self, text, error=False):
        ev = {"type": "system", "text": text, "error": error}
        if self.current:
            self.history.setdefault(self.current, []).append(ev)
        self._append(ev)

    def set_connected(self, connected):
        state = "normal" if connected else "disabled"
        self.entry.configure(state=state)
        self.file_button.configure(state=state)
        self.send_button.state(["!disabled"] if connected else ["disabled"])

    # ---------- sending ----------

    def send_message(self):
        text = self.entry.get().strip()
        if not text or not self.current:
            return
        self.app.send({"type": "message", "to": self.current, "text": text})
        self.entry.delete(0, "end")

    def pick_file(self):
        if not self.current:
            return
        path = filedialog.askopenfilename(title="Choose a file to send")
        if not path:
            return
        try:
            self.app.offer_file(path, self.current)
        except (OSError, ValueError) as e:
            messagebox.showerror("Can't send file", str(e))

    # ---------- file cards ----------

    def _add_card(self, ev):
        mine = ev.get("sender") == self.username
        self.cards[ev["offer_id"]] = dict(ev, state="saved" if mine else "available")
        self._add_to_conversation(self._conversation_key(ev), ev)

    def _make_card(self, offer_id):
        """A file's preview in the chat. Clicking it downloads or opens the file."""
        card = self.cards[offer_id]
        name = card.get("filename", "file")
        bg = COLORS["sidebar"]
        frame = tk.Frame(self.display, bg=bg, padx=12, pady=8, cursor="hand2")
        icon = tk.Label(frame, text=_file_icon(name), bg=bg, fg=COLORS["text"], font=("Segoe UI Emoji", 20))
        icon.grid(row=0, column=0, rowspan=3, padx=(0, 10))
        title = tk.Label(frame, text=name, bg=bg, fg="white", font=("Segoe UI", 11, "bold"), anchor="w")
        title.grid(row=0, column=1, sticky="w")
        kind = os.path.splitext(name)[1].lstrip(".").upper() or "FILE"
        meta = tk.Label(frame, text=f"{kind}  ·  {_human_size(card.get('size', 0))}", bg=bg,
                        fg=COLORS["muted"], font=("Segoe UI", 9), anchor="w")
        meta.grid(row=1, column=1, sticky="w")
        action = tk.Label(frame, bg=bg, font=("Segoe UI", 9, "bold"), anchor="w")
        action.grid(row=2, column=1, sticky="w")
        for widget in (frame, icon, title, meta, action):
            widget.bind("<Button-1>", lambda _e: self._on_card_click(offer_id))
        self.card_widgets[offer_id] = {"frame": frame, "action": action}
        self._refresh_card(offer_id)
        return frame

    def _card_action(self, card):
        """What a card offers in each state, as (text, color)."""
        return {"available": ("Click to download", COLORS["accent"]),
                "downloading": ("Downloading...", COLORS["muted"]),
                "saved": ("Click to open", COLORS["me"]),
                "failed": ("Download failed. Click to try again", COLORS["error"])}[card["state"]]

    def _refresh_card(self, offer_id):
        widgets = self.card_widgets.get(offer_id)
        if widgets:
            text, color = self._card_action(self.cards[offer_id])
            widgets["action"].configure(text=text, fg=color)

    def _set_card_state(self, offer_id, state, path=None):
        card = self.cards.get(offer_id)
        if card:
            card["state"] = state
            card["path"] = path or card.get("path")
            self._refresh_card(offer_id)

    def _on_card_click(self, offer_id):
        card = self.cards[offer_id]
        if card["state"] == "saved":
            self._open_file(card)
        elif card["state"] in ("available", "failed"):
            self._set_card_state(offer_id, "downloading")
            self.app.request_file(card)

    def _open_file(self, card):
        path = card.get("path")
        mine = card.get("sender") == self.username
        if not path or not os.path.exists(path):
            messagebox.showerror("Can't open file", f"{card.get('filename')} is no longer at:\n{path}")
            return
        if not mine and os.path.splitext(path)[1].lower() in RUNNABLE_TYPES:
            if not messagebox.askyesno("Open this file?",
                                       f"{os.path.basename(path)} is a program or script from "
                                       f"{card.get('sender')}. Opening it will run it on your computer.\n\n"
                                       "Only continue if you trust this file. Open it?",
                                       icon="warning", default="no"):
                return
        try:
            _open_with_default_app(path)
        except OSError as e:
            messagebox.showerror("Can't open file", str(e))

    # ---------- file transfers ----------

    def _update_transfer(self, ev):
        file_id = ev["file_id"]
        if file_id in self._finished_transfers:
            return  # a last progress event can arrive just after the transfer failed
        row = self.transfer_rows.get(file_id)
        if row is None:
            row = self._add_transfer_row(file_id, ev)
        size = ev.get("size", 0)
        done = ev.get("done", 0)
        row["bar"].configure(value=100 * done / size if size else 100)
        if ev.get("direction") == "send":
            verb = f"Sending to {ev.get('requester')}:"
        else:
            verb = "Downloading"
        row["label"].configure(text=f"{verb} {ev.get('filename')}  "
                                    f"{_human_size(done)} / {_human_size(size)}")

    def _add_transfer_row(self, file_id, ev):
        if not self.transfer_rows:
            self.transfer_panel.pack(fill="x", padx=16, pady=(0, 8), before=self.input_bar)
        frame = tk.Frame(self.transfer_panel, bg=COLORS["panel"])
        frame.pack(fill="x", pady=2)
        label = tk.Label(frame, bg=COLORS["panel"], fg=COLORS["muted"], anchor="w",
                         font=("Segoe UI", 9))
        label.pack(fill="x")
        bar = ttk.Progressbar(frame, style="Transfer.Horizontal.TProgressbar", maximum=100)
        bar.pack(fill="x")
        row = {"frame": frame, "label": label, "bar": bar}
        self.transfer_rows[file_id] = row
        return row

    def _finish_transfer(self, ev):
        """Remove the progress row and report the result in the right conversation."""
        file_id = ev["file_id"]
        self._finished_transfers.add(file_id)
        row = self.transfer_rows.pop(file_id, None)
        if row:
            row["frame"].destroy()
        if not self.transfer_rows:
            self.transfer_panel.pack_forget()

        sending = ev.get("direction") == "send"
        name = ev.get("filename", "file")
        key = ev.get("to", "") if sending else self._conversation_key(ev)
        size = _human_size(ev.get("size", 0))

        if not sending:
            failed = ev["type"] == "_file_failed"
            self._set_card_state(ev.get("offer_id"), "failed" if failed else "saved", ev.get("path"))

        if ev["type"] == "_file_failed":
            if "verified" in ev:
                text = f"Received {name} ({size}) from {ev.get('sender')}: {ev.get('reason')}"
            else:
                text = f"Transfer of {name} failed: {ev.get('reason')}"
            self._add_to_conversation(key, {"type": "system", "text": text, "error": True})
            return

        took = f"{ev.get('seconds', 0):.1f} s"
        if sending:
            text = f"Sent {name} ({size}) to {ev.get('requester')} in {took}"
        else:
            text = (f"Received {name} ({size}) from {ev.get('sender')} in {took}\n"
                    f"Integrity check passed. Saved to {ev.get('path')}")
        self._add_to_conversation(key, {"type": "system", "text": text})


def _file_icon(name):
    ext = os.path.splitext(name)[1].lower()
    for icon, extensions in FILE_ICONS.items():
        if ext in extensions:
            return icon
    return "📄"


def _open_with_default_app(path):
    if sys.platform == "win32":
        os.startfile(path)
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])


def _human_size(n):
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"
