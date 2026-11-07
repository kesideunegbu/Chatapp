# Chatapp

## Client

Tkinter desktop client (no extra packages needed).

```
python Client/main.py --demo                     # try the UI with a built-in fake server
python Client/main.py --host 127.0.0.1 --port 5000
```

Features: login screen, channel + online-user sidebar (click a user to DM), chat window
with unread counts, message input, file sharing (files up to 1 GB, shown as a card in the
current channel or DM; click a card to download, then click again to open).
Downloads are saved to `downloads/`.

The client speaks newline-delimited JSON over TCP. The message formats are documented
at the top of `Client/network.py`; the server needs to implement the same ones.

## Server

```
python Server/main.py --port 5000                # this computer only
python Server/main.py --host 0.0.0.0 --port 5000 # accept other computers
```

The server defaults to port 65432 but the client defaults to 5000, so pass `--port`.

## File transfer

Sharing a file sends a `file_offer`, which appears as a card in the chat. The file stays
on the sender's computer, and the server stores nothing. Clicking a card sends a
`file_request`, and the sender streams the file to that person: `file_start`, many
`file_chunk` messages (64 KB of base64 each), and `file_end` with the SHA-256. The
sender therefore has to stay online and keep the file until everyone has it.

Chat keeps working during a transfer, and several files can move at once. Details are at
the top of `Client/file_transfer.py`.

The size limit is `MAX_FILE_BYTES`, in both `Client/file_transfer.py` and `Server/main.py`.

To run several clients on one computer, give each its own folder so their downloads
stay separate: `python Client/main.py --downloads downloads/bob`
