# Chatapp

## Client

Tkinter desktop client (no extra packages needed).

```
python Client/main.py --demo                     # try the UI with a built-in fake server
python Client/main.py --host 127.0.0.1 --port 5000
```

Features: login screen, channel + online-user sidebar (click a user to DM), chat window
with unread counts, message input, file picker (files up to 5 MB, click a received file to save it).

The client speaks newline-delimited JSON over TCP. The message formats are documented
at the top of `Client/network.py`; the server needs to implement the same ones.
