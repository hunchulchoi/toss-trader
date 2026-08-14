from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping
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

DEFAULT_CACHE_SECONDS = 30.0


class PayloadCache:
    def __init__(
        self,
        loader: Callable[[], Mapping[str, Any]],
        *,
        ttl_seconds: float = DEFAULT_CACHE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        initial: Mapping[str, Any] | None = None,
    ) -> None:
        if ttl_seconds < 0:
            raise ValueError("timeline cache ttl must not be negative")
        self._loader = loader
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._value: Mapping[str, Any] | None = initial
        self._loaded_at = clock() if initial is not None else None

    def get(self) -> Mapping[str, Any]:
        with self._lock:
            now = self._clock()
            stale = (
                self._value is None
                or self._loaded_at is None
                or now - self._loaded_at >= self._ttl_seconds
            )
            if stale:
                self._value = self._loader()
                self._loaded_at = now
            assert self._value is not None
            return self._value


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
    *,
    host: str,
    port: int,
    payload: Mapping[str, Any] | None = None,
    payload_loader: Callable[[], Mapping[str, Any]] | None = None,
    cache_seconds: float = DEFAULT_CACHE_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> ThreadingHTTPServer:
    if not host.strip():
        raise ValueError("timeline host must not be empty")
    if not 1 <= port <= 65535:
        raise ValueError("timeline port must be between 1 and 65535")
    if payload_loader is None:
        if payload is None:
            raise ValueError("timeline payload or payload_loader is required")
        cache = PayloadCache(lambda: payload, ttl_seconds=cache_seconds, clock=clock, initial=payload)
    else:
        cache = PayloadCache(
            payload_loader,
            ttl_seconds=cache_seconds,
            clock=clock,
            initial=payload,
        )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._respond("GET")

        def do_HEAD(self) -> None:
            self._respond("HEAD")

        def do_POST(self) -> None:
            self._respond("POST")

        def _respond(self, method: str) -> None:
            current = cache.get() if urlsplit(self.path).path == "/api/timeline" else (payload or cache.get())
            status, content_type, body = timeline_response(method, self.path, current)
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


def serve_timeline(
    *,
    host: str,
    port: int,
    payload: Mapping[str, Any] | None = None,
    payload_loader: Callable[[], Mapping[str, Any]] | None = None,
    cache_seconds: float = DEFAULT_CACHE_SECONDS,
) -> None:
    with create_timeline_server(
        host=host,
        port=port,
        payload=payload,
        payload_loader=payload_loader,
        cache_seconds=cache_seconds,
    ) as server:
        server.serve_forever()


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"not JSON serializable: {type(value).__name__}")
