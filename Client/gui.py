"""Tkinter GUI for the chat client: login screen and main chat window."""

import base64
import os
import queue
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from network import DemoConnection, TCPConnection, encode_file

POLL_MS = 50  # how often the GUI checks for incoming network events

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
    def __init__(self, host="127.0.0.1", port=5000, demo=False):
        super().__init__()
        self.title("Chatapp")
        self.geometry("900x600")
        self.minsize(640, 420)
        self.configure(bg=COLORS["bg"])
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.demo = demo
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

    # ---------- connection lifecycle ----------

    def start_login(self, host, port, username, password, demo):
        self.username = username
        self._pending_login = {"type": "login", "username": username, "password": password}
        self.conn = DemoConnection(self.inbox) if demo else TCPConnection(self.inbox)
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
        self._file_links = {}  # Text tag -> file event, for click-to-save

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
        self.display = tk.Text(body, bg=COLORS["panel"], fg=COLORS["text"], wrap="word",
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
        self.display.tag_configure("file", foreground=COLORS["accent"], underline=True)

        bar = tk.Frame(main, bg=COLORS["panel"])
        bar.pack(fill="x", padx=16, pady=(0, 16))
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
        if kind in ("message", "file"):
            key = self._conversation_key(ev)
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
        elif kind == "users":
            self.set_users(ev.get("users", []))
        elif kind == "error":
            self.system_message(ev.get("reason", "Server error."), error=True)

    def _render_current(self):
        self.header.configure(text=self._title(self.current) if self.current else "No conversation selected")
        self.display.configure(state="normal")
        self.display.delete("1.0", "end")
        self.display.configure(state="disabled")
        self._file_links.clear()
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
            if ev.get("type") == "file":
                tag = f"file{len(self._file_links)}"
                self._file_links[tag] = ev
                d.insert("end", "  sent a file: ")
                d.insert("end", f"📎 {ev.get('filename', 'file')} ({_human_size(ev.get('size', 0))})", ("file", tag))
                d.tag_bind(tag, "<Button-1>", lambda _e, t=tag: self.save_file(self._file_links[t]))
                d.tag_bind(tag, "<Enter>", lambda _e: d.configure(cursor="hand2"))
                d.tag_bind(tag, "<Leave>", lambda _e: d.configure(cursor="arrow"))
                d.insert("end", "\n")
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
            filename, size, data = encode_file(path)
        except (OSError, ValueError) as e:
            messagebox.showerror("Can't send file", str(e))
            return
        self.app.send({"type": "file", "to": self.current, "filename": filename, "size": size, "data": data})

    def save_file(self, ev):
        path = filedialog.asksaveasfilename(title="Save file", initialfile=ev.get("filename", "file"))
        if not path:
            return
        try:
            with open(path, "wb") as f:
                f.write(base64.b64decode(ev.get("data", "")))
        except (OSError, ValueError) as e:
            messagebox.showerror("Can't save file", str(e))
            return
        self.system_message(f"Saved {os.path.basename(path)}")


def _human_size(n):
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"
