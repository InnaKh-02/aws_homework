from __future__ import annotations

import errno
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


CALLBACK_SUCCESS_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Consent Complete</title>
  <style>
    body {
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background: #f5f1e8;
      color: #1f2328;
      font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    main {
      max-width: 36rem;
      padding: 2rem 2.25rem;
      border: 1px solid #d0c7b8;
      border-radius: 1rem;
      background: #fffdf8;
      box-shadow: 0 18px 40px rgba(31, 35, 40, 0.08);
    }
    h1 {
      margin: 0 0 0.75rem;
      font-size: 1.85rem;
      line-height: 1.1;
    }
    p {
      margin: 0.5rem 0;
      line-height: 1.5;
    }
  </style>
</head>
<body>
  <main>
    <h1>Consent complete</h1>
    <p>The OAuth redirect reached the local callback server successfully.</p>
    <p>You can return to the notebook and run the next invoke step.</p>
  </main>
</body>
</html>
"""

_CALLBACK_SERVER_REGISTRY: dict[
    tuple[str, int, str],
    tuple[ThreadingHTTPServer, threading.Thread],
] = {}


class _CallbackHandler(BaseHTTPRequestHandler):
    server_version = "WorkshopCallback/1.0"

    def do_GET(self) -> None:
        self.server.last_request = {
            "path": self.path,
            "query": urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query),
        }
        self.server.callback_event.set()
        body = CALLBACK_SUCCESS_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


def callback_endpoint(oauth_return_url: str) -> tuple[str, int, str]:
    parsed = urllib.parse.urlparse(oauth_return_url)
    if parsed.scheme != "http":
        raise ValueError("OAUTH_RETURN_URL must use http:// for the local callback server.")
    host = parsed.hostname or "localhost"
    if host not in {"localhost", "127.0.0.1"}:
        raise ValueError(
            "OAUTH_RETURN_URL must point to localhost or 127.0.0.1 for demo callback handling."
        )
    port = parsed.port or 80
    path = parsed.path or "/"
    return host, port, path


def callback_server_info(
    oauth_return_url: str,
    host: str,
    port: int,
    path: str,
    *,
    status: str = "running",
) -> dict[str, Any]:
    return {
        "url": oauth_return_url,
        "host": host,
        "port": port,
        "path": path,
        "status": status,
    }


def reset_callback_server_state(server: ThreadingHTTPServer) -> None:
    server.callback_event.clear()
    server.last_request = None


def start_local_callback_server(
    oauth_return_url: str,
    *,
    current_server: ThreadingHTTPServer | None = None,
    current_thread: threading.Thread | None = None,
) -> tuple[ThreadingHTTPServer, threading.Thread, dict[str, Any]]:
    host, port, path = callback_endpoint(oauth_return_url)
    endpoint = (host, port, path)

    existing = _CALLBACK_SERVER_REGISTRY.get(endpoint)
    if existing:
        server, thread = existing
        if thread.is_alive():
            reset_callback_server_state(server)
            return server, thread, callback_server_info(oauth_return_url, host, port, path)
        _CALLBACK_SERVER_REGISTRY.pop(endpoint, None)

    if current_server is not None and current_thread is not None and current_thread.is_alive():
        current_host, current_port = current_server.server_address[:2]
        current_path = getattr(current_server, "callback_path", path)
        if (current_host, current_port, current_path) == endpoint:
            reset_callback_server_state(current_server)
            return current_server, current_thread, callback_server_info(oauth_return_url, host, port, path)
        stop_local_callback_server(oauth_return_url, current_server, current_thread)

    class CallbackServer(ThreadingHTTPServer):
        allow_reuse_address = True

    try:
        server = CallbackServer((host, port), _CallbackHandler)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            raise RuntimeError(
                f"Callback port {host}:{port} is already in use. "
                "If this is a stale notebook callback server, stop the old local callback server or restart the kernel."
            ) from exc
        raise

    server.callback_event = threading.Event()
    server.last_request = None
    server.callback_path = path
    thread = threading.Thread(
        target=server.serve_forever,
        name="agentcore-demo-callback-server",
        daemon=True,
    )
    thread.start()
    _CALLBACK_SERVER_REGISTRY[endpoint] = (server, thread)
    return server, thread, callback_server_info(oauth_return_url, host, port, path)


def stop_local_callback_server(
    oauth_return_url: str,
    server: ThreadingHTTPServer | None,
    thread: threading.Thread | None,
) -> dict[str, Any]:
    if server is None:
        return {
            "url": oauth_return_url,
            "status": "stopped",
        }

    host, port, path = callback_endpoint(oauth_return_url)
    endpoint = (host, port, path)
    server.shutdown()
    server.server_close()
    if thread is not None:
        thread.join(timeout=2)
    _CALLBACK_SERVER_REGISTRY.pop(endpoint, None)
    return {
        "url": oauth_return_url,
        "status": "stopped",
    }


def wait_for_local_callback(
    oauth_return_url: str,
    server: ThreadingHTTPServer,
    *,
    timeout_sec: int = 180,
) -> dict[str, Any]:
    if not server.callback_event.wait(timeout=timeout_sec):
        raise TimeoutError(f"Timed out waiting for OAuth callback on {oauth_return_url}.")
    return dict(server.last_request or {})
