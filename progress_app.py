#!/usr/bin/env python3
"""Entry point for the BIM4D Construction Progress Estimator application."""
from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

from progress_app.server import AppState, create_server


def main():
    parser = argparse.ArgumentParser(
        description="BIM4D · Interactive Construction Progress Estimator with Landmark Point Matching & ICP"
    )
    parser.add_argument("--port", type=int, default=8765, help="HTTP server port (default: 8765)")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind (default: 127.0.0.1)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open browser automatically on start")
    args = parser.parse_args()

    workspace_dir = Path(__file__).resolve().parent
    app_state = AppState(workspace_root=str(workspace_dir))
    server = create_server(app_state, host=args.host, port=args.port)
    actual_port = server.server_address[1]
    url = f"http://{args.host}:{actual_port}"

    print(f"🚀 BIM4D Progress Estimator running at: {url}")
    print("👉 Serving interactive 3D WebGL UI with Point Cloud & BIM landmark matching.")
    print("Press Ctrl+C to terminate the server.\n")

    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down BIM4D server...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
