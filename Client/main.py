"""Entry point for the chat client.

Usage:
    python Client/main.py                    # connect to 127.0.0.1:5000
    python Client/main.py --host 10.0.0.5 --port 6000
    python Client/main.py --demo             # fake server, no network needed
"""

import argparse

from gui import ChatApp


def main():
    parser = argparse.ArgumentParser(description="Chatapp client")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--demo", action="store_true", help="use a built-in fake server")
    args = parser.parse_args()

    ChatApp(host=args.host, port=args.port, demo=args.demo).mainloop()


if __name__ == "__main__":
    main()
