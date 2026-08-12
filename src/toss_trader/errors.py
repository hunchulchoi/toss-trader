from __future__ import annotations

from typing import Any


class TossApiError(RuntimeError):
    def __init__(
        self,
        *,
        status: int,
        code: str,
        message: str,
        request_id: str | None = None,
        data: Any = None,
    ) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.request_id = request_id
        self.data = data
        suffix = f" request_id={request_id}" if request_id else ""
        super().__init__(f"Toss API {status} {code}: {message}{suffix}")
