#!/usr/bin/env python3
"""Serve the static dashboard locally."""

import argparse
import http.server
import socketserver
import webbrowser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("127.0.0.1", args.port), handler) as httpd:
        url = f"http://127.0.0.1:{args.port}/"
        print(url)
        if not args.no_browser:
            webbrowser.open(url)
        httpd.serve_forever()


if __name__ == "__main__":
    import os

    os.chdir(ROOT)
    main()
