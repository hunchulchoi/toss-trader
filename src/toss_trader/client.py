from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .errors import TossApiError

SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9.\-]+$")


@dataclass(frozen=True, slots=True)
class HttpRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes | None = None


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class Transport(Protocol):
    def send(self, request: HttpRequest, timeout: float) -> HttpResponse: ...


class UrllibTransport:
    def send(self, request: HttpRequest, timeout: float) -> HttpResponse:
        urllib_request = Request(
            request.url,
            data=request.body,
            headers=dict(request.headers),
            method=request.method,
        )
        try:
            with urlopen(urllib_request, timeout=timeout) as response:
                return HttpResponse(
                    status=response.status,
                    headers=dict(response.headers.items()),
                    body=response.read(),
                )
        except HTTPError as error:
            return HttpResponse(
                status=error.code,
                headers=dict(error.headers.items()),
                body=error.read(),
            )


class TossClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        account_seq: str | None = None,
        base_url: str = "https://openapi.tossinvest.com",
        transport: Transport | None = None,
        timeout: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        max_get_retries: int = 2,
    ) -> None:
        if not client_id or not client_secret:
            raise ValueError("client credentials must not be empty")
        self._client_id = client_id
        self._client_secret = client_secret
        self._account_seq = account_seq
        self._base_url = base_url.rstrip("/")
        self._transport = transport or UrllibTransport()
        self._timeout = timeout
        self._clock = clock
        self._sleeper = sleeper
        self._max_get_retries = max_get_retries
        self._access_token: str | None = None
        self._token_expires_at = 0.0

    def prices(self, symbols: Sequence[str]) -> list[dict[str, Any]]:
        clean_symbols = self._validate_symbols(symbols)
        payload = self._request(
            "GET", "/api/v1/prices", query={"symbols": ",".join(clean_symbols)}
        )
        return self._require_result(payload, list)

    def stocks(self, symbols: Sequence[str]) -> list[dict[str, Any]]:
        clean_symbols = self._validate_symbols(symbols)
        payload = self._request(
            "GET", "/api/v1/stocks", query={"symbols": ",".join(clean_symbols)}
        )
        return self._require_result(payload, list)

    def candles(
        self,
        symbol: str,
        *,
        interval: str = "1m",
        count: int = 100,
        before: str | None = None,
        adjusted: bool = True,
    ) -> dict[str, Any]:
        self._validate_symbols([symbol])
        if interval not in {"1m", "1d"}:
            raise ValueError("interval must be 1m or 1d")
        if not 1 <= count <= 200:
            raise ValueError("count must be between 1 and 200")
        query: dict[str, str | int] = {
            "symbol": symbol,
            "interval": interval,
            "count": count,
            "adjusted": str(adjusted).lower(),
        }
        if before:
            query["before"] = before
        payload = self._request("GET", "/api/v1/candles", query=query)
        return self._require_result(payload, dict)

    def accounts(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/api/v1/accounts")
        return self._require_result(payload, list)

    def market_calendar(
        self, country: str, *, day: date | None = None
    ) -> dict[str, Any]:
        normalized = country.upper()
        if normalized not in {"KR", "US"}:
            raise ValueError("market calendar country must be KR or US")
        query = {"date": day.isoformat()} if day is not None else None
        payload = self._request(
            "GET", f"/api/v1/market-calendar/{normalized}", query=query
        )
        return self._require_result(payload, dict)

    def holdings(self, symbol: str | None = None) -> dict[str, Any]:
        query = None
        if symbol is not None:
            self._validate_symbols([symbol])
            query = {"symbol": symbol}
        payload = self._request(
            "GET", "/api/v1/holdings", query=query, account_scoped=True
        )
        return self._require_result(payload, dict)

    @staticmethod
    def _validate_symbols(symbols: Sequence[str]) -> list[str]:
        if not 1 <= len(symbols) <= 200:
            raise ValueError("symbols must contain between 1 and 200 entries")
        clean = [symbol.strip() for symbol in symbols]
        if any(not SYMBOL_PATTERN.fullmatch(symbol) for symbol in clean):
            raise ValueError("symbol contains unsupported characters")
        return clean

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str | int] | None = None,
        account_scoped: bool = False,
    ) -> dict[str, Any]:
        if account_scoped and not self._account_seq:
            raise ValueError("TOSS_ACCOUNT_SEQ is required for this endpoint")

        url = f"{self._base_url}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"

        retries = self._max_get_retries if method == "GET" else 0
        auth_retry_available = True
        attempt = 0
        while True:
            headers = {
                "Accept": "application/json",
                "Authorization": f"Bearer {self._get_access_token()}",
            }
            if account_scoped:
                headers["X-Tossinvest-Account"] = self._account_seq or ""
            response = self._transport.send(
                HttpRequest(method=method, url=url, headers=headers), self._timeout
            )

            if 200 <= response.status < 300:
                return self._decode_json(response)
            if response.status == 401 and auth_retry_available:
                self._access_token = None
                self._token_expires_at = 0.0
                auth_retry_available = False
                continue
            if response.status == 429 and attempt < retries:
                self._sleeper(self._retry_after(response, attempt))
                attempt += 1
                continue
            self._raise_api_error(response)

    def _get_access_token(self) -> str:
        if self._access_token and self._clock() < self._token_expires_at:
            return self._access_token

        body = urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            }
        ).encode()
        response = self._transport.send(
            HttpRequest(
                method="POST",
                url=f"{self._base_url}/oauth2/token",
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                body=body,
            ),
            self._timeout,
        )
        if not 200 <= response.status < 300:
            self._raise_api_error(response)
        payload = self._decode_json(response)
        token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        if not isinstance(token, str) or not token:
            raise TossApiError(
                status=response.status,
                code="invalid-token-response",
                message="access_token missing from token response",
            )
        if not isinstance(expires_in, (int, float)):
            expires_in = 300
        self._access_token = token
        self._token_expires_at = self._clock() + max(1.0, float(expires_in) - 30.0)
        return token

    @staticmethod
    def _decode_json(response: HttpResponse) -> dict[str, Any]:
        try:
            payload = json.loads(response.body)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise TossApiError(
                status=response.status,
                code="invalid-json-response",
                message="response was not valid JSON",
            ) from error
        if not isinstance(payload, dict):
            raise TossApiError(
                status=response.status,
                code="invalid-json-response",
                message="response JSON must be an object",
            )
        return payload

    @staticmethod
    def _require_result(
        payload: dict[str, Any], expected_type: type[list | dict]
    ) -> Any:
        result = payload.get("result")
        if not isinstance(result, expected_type):
            raise TossApiError(
                status=200,
                code="invalid-response-shape",
                message="result has an unexpected type",
            )
        return result

    @staticmethod
    def _header(response: HttpResponse, name: str) -> str | None:
        target = name.lower()
        return next(
            (value for key, value in response.headers.items() if key.lower() == target),
            None,
        )

    def _retry_after(self, response: HttpResponse, attempt: int) -> float:
        raw = self._header(response, "Retry-After")
        try:
            return max(0.0, float(raw)) if raw is not None else float(2**attempt)
        except ValueError:
            return float(2**attempt)

    def _raise_api_error(self, response: HttpResponse) -> None:
        payload = self._decode_json(response)
        error_payload = payload.get("error")
        if isinstance(error_payload, dict):
            code = str(error_payload.get("code", "unknown-error"))
            message = str(error_payload.get("message", "Toss API request failed"))
            request_id = error_payload.get("requestId")
            data = error_payload.get("data")
        else:
            code = str(error_payload or "unknown-error")
            message = str(payload.get("error_description", "Toss API request failed"))
            request_id = self._header(response, "X-Request-Id")
            data = None
        raise TossApiError(
            status=response.status,
            code=code,
            message=message,
            request_id=str(request_id) if request_id is not None else None,
            data=data,
        )
