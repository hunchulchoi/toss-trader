from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import Any
from urllib.parse import urlsplit

_ASSETS = {
    "/": ("timeline.html", "text/html; charset=utf-8"),
    "/assets/timeline.css": ("timeline.css", "text/css; charset=utf-8"),
    "/assets/timeline.js": (
        "timeline.js",
        "text/javascript; charset=utf-8",
    ),
}


def timeline_response(
    method: str,
    path: str,
    payload: Mapping[str, Any],
) -> tuple[int, str, bytes]:
    if method not in {"GET", "HEAD"}:
        return 405, "application/json; charset=utf-8", b'{"error":"method not allowed"}'
    normalized = urlsplit(path).path
    if normalized == "/healthz":
        return 200, "application/json; charset=utf-8", b'{"status":"ok"}'
    if normalized == "/api/timeline":
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            default=_json_default,
        ).encode()
        return 200, "application/json; charset=utf-8", body
    asset = _ASSETS.get(normalized)
    if asset is None:
        return 404, "text/plain; charset=utf-8", b"not found\n"
    filename, content_type = asset
    body = files("toss_trader.web").joinpath(filename).read_bytes()
    return 200, content_type, body


def create_timeline_server(
    *, host: str, port: int, payload: Mapping[str, Any]
) -> ThreadingHTTPServer:
    if not host.strip():
        raise ValueError("timeline host must not be empty")
    if not 1 <= port <= 65535:
        raise ValueError("timeline port must be between 1 and 65535")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._respond("GET")

        def do_HEAD(self) -> None:
            self._respond("HEAD")

        def do_POST(self) -> None:
            self._respond("POST")

        def _respond(self, method: str) -> None:
            status, content_type, body = timeline_response(method, self.path, payload)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; object-src 'none'",
            )
            self.send_header(
                "Cache-Control",
                "no-store" if self.path.startswith("/api/") else "public, max-age=300",
            )
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    return ThreadingHTTPServer((host, port), Handler)


def serve_timeline(*, host: str, port: int, payload: Mapping[str, Any]) -> None:
    with create_timeline_server(host=host, port=port, payload=payload) as server:
        server.serve_forever()


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"not JSON serializable: {type(value).__name__}")
