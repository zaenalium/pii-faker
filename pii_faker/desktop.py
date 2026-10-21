"""
PII Faker desktop launcher (roadmap 1.2).

Starts the bundled FastAPI app on 127.0.0.1 with a random free port and a
per-launch session token, then opens it in a native pywebview window.

Security model:
- The server binds loopback only and requires the session token
  (X-PII-Session header) on every /api/* call. Other local processes
  cannot call the API without the token, which only exists in this
  process's memory.
- No token is needed for / (the UI shell) or /static/*.
- Closing the window shuts the server down cleanly.
- On Windows, WebView2 is required (roadmap 1.9 — see _check_webview2).

Usage:
    python -m pii_faker.desktop
    pii-faker-desktop
"""

import os
import secrets
import socket
import sys
import threading
import webbrowser

import uvicorn

SESSION_HEADER = "X-PII-Session"


def _check_webview2() -> bool:
    """Warn if WebView2 is missing on Windows (roadmap 1.9).

    WebView2 is built into Windows 11 and updated Windows 10. pywebview
    normally discovers it at window-creation time, so this is a best-effort
    preflight that prints a clear hint rather than a blank window.
    Returns False when the runtime is missing and the caller should abort.
    """
    if sys.platform != "win32":
        return True
    try:
        import webview.platforms.edgechromium as edge

        if edge.is_chromium_installed():
            return True
    except Exception:
        pass
    print(
        "WebView2 Runtime not found. Install it from "
        "https://developer.microsoft.com/microsoft-edge/webview2/",
        file=sys.stderr,
    )
    return False


def find_free_port() -> int:
    """Pick a free loopback port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def create_app(session_token: str):
    """Build the FastAPI app wrapped with session-token auth."""
    from fastapi import Request
    from fastapi.responses import JSONResponse
    from pii_faker.web.app import app as base_app

    base_app.state.session_token = session_token
    # The window signs in with the same token (see main()). The desktop is single-user:
    # it may process local paths, and it keeps jobs until it closes.
    from pii_faker.web.auth import Authenticator

    base_app.state.auth = Authenticator(session_token)
    base_app.state.allow_local_paths = True
    base_app.state.retention_seconds = 0

    @base_app.middleware("http")
    async def _require_session_token(request: Request, call_next):
        if request.url.path.startswith(("/api/", "/download/")):
            if request.headers.get(SESSION_HEADER, "") != session_token:
                return JSONResponse(
                    {"detail": "missing or invalid session token"}, status_code=401
                )
        return await call_next(request)

    return base_app


def run_server(app, port: int, ready: threading.Event, server_holder: dict):
    """Run uvicorn until shutdown is requested."""
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", access_log=False
    )
    server = uvicorn.Server(config)
    server_holder["server"] = server
    ready.set()
    server.run()


class DesktopApi:
    """Native dialogs exposed to the web UI (desktop only)."""

    FILE_TYPES = (
        "Data files (*.csv;*.tsv;*.xlsx;*.xls;*.json;*.ndjson;*.jsonl;*.parquet)",
    )

    def open_file(self):
        """Show a native open-file dialog; returns the chosen path or ''."""
        try:
            import webview

            window = webview.active_window()
            if window is None:
                return ""
            result = window.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=False,
                file_types=self.FILE_TYPES,
            )
            if result:
                return result[0]
        except Exception:
            pass
        return ""

    def save_file(self, suggested_name: str = ""):
        """Show a native save-file dialog; returns the chosen path or ''."""
        try:
            import webview

            window = webview.active_window()
            if window is None:
                return ""
            result = window.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=suggested_name or "processed.csv",
            )
            if result:
                return result if isinstance(result, str) else result[0]
        except Exception:
            pass
        return ""


def main() -> int:
    if not _check_webview2():
        return 1
    port = find_free_port()
    session_token = secrets.token_urlsafe(32)
    app = create_app(session_token)

    ready = threading.Event()
    server_holder: dict = {}
    thread = threading.Thread(
        target=run_server, args=(app, port, ready, server_holder), daemon=True
    )
    thread.start()
    ready.wait(timeout=10)

    url = f"http://127.0.0.1:{port}/"
    sign_in_url = f"{url}?token={session_token}"      # signs the window in, then redirects to /
    os.environ["PII_FAKER_SESSION_TOKEN"] = session_token

    try:
        import webview

        window = webview.create_window(
            "PII Faker",
            sign_in_url,
            width=1100,
            height=800,
            min_size=(900, 650),
            js_api=DesktopApi(),
        )

        def _on_closed():
            server = server_holder.get("server")
            if server is not None:
                server.should_exit = True

        window.events.closed += _on_closed
        webview.start(debug=False)
    except Exception:
        print(f"PII Faker running at {url}")
        print("Press Ctrl+C to stop.")
        try:
            webbrowser.open(sign_in_url)
        except Exception:
            pass
        try:
            thread.join()
        except KeyboardInterrupt:
            pass
    finally:
        server = server_holder.get("server")
        if server is not None:
            server.should_exit = True

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
