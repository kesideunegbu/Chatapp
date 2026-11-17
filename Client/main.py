"""Entry point for the chat client.

Usage:
    python Client/main.py                    # connect to 127.0.0.1:5000
    python Client/main.py --host 10.0.0.5 --port 6000
    python Client/main.py --demo             # fake server, no network needed
    python Client/main.py --downloads C:/tmp/bob   # where received files are saved
"""

import argparse
import os

from gui import ChatApp

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    parser = argparse.ArgumentParser(description="Chatapp client")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--demo", action="store_true", help="use a built-in fake server")
    parser.add_argument("--downloads", default=os.path.join(PROJECT_ROOT, "downloads"),
                        help="folder for received files")
    args = parser.parse_args()

    ChatApp(host=args.host, port=args.port, demo=args.demo, download_dir=args.downloads).mainloop()


if __name__ == "__main__":
    main()
