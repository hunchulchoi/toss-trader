from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any
from urllib.parse import urlencode

from .client import HttpRequest, Transport, UrllibTransport
from .krx_flow import KrMarketCalendar, resolve_kr_session_indexes_through
from .official_data import OfficialDataRepository

KIS_BASE_URL = "https://openapi.koreainvestment.com:9443"
KIS_FLOW_PATH = "/uapi/domestic-stock/v1/quotations/investor-trade-by-stock-daily"
KIS_FLOW_TR_ID = "FHPTJ04160001"
_SYMBOL = re.compile(r"^[0-9A-Z]{6}$")
_FLOW_AMOUNT_FIELDS = (
    "frgn_ntby_tr_pbmn",
    "orgn_ntby_tr_pbmn",
    "acml_tr_pbmn",
)
KIS_TOKEN_COOLDOWN_SECONDS = 61.0

logger = logging.getLogger(__name__)


class KisTokenRequestError(RuntimeError):
    """KIS could not issue an access token for this collection run."""


class KisInvestorFlowClient:
    def __init__(
        self,
        *,
        app_key: str,
        app_secret: str,
        base_url: str = KIS_BASE_URL,
        transport: Transport | None = None,
        timeout: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        minimum_interval_seconds: float = 0.06,
    ) -> None:
        if not app_key or not app_secret:
            raise ValueError("KIS_APP_KEY and KIS_APP_SECRET must not be empty")
        if minimum_interval_seconds < 0:
            raise ValueError("KIS request interval must not be negative")
        self._app_key = app_key
        self._app_secret = app_secret
        self._base_url = base_url.rstrip("/")
        self._transport = transport or UrllibTransport()
        self._timeout = timeout
        self._clock = clock
        self._sleeper = sleeper
        self._minimum_interval = minimum_interval_seconds
        self._next_request_at = 0.0
        self._request_lock = threading.Lock()
        self._access_token: str | None = None
        self._token_expires_at = 0.0

    def daily_investor_flow(self, symbol: str, *, as_of: date) -> list[dict[str, Any]]:
        if not _SYMBOL.fullmatch(symbol):
            raise ValueError("KIS symbol must be six uppercase alphanumeric characters")
        query = urlencode(
            {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol,
                "FID_INPUT_DATE_1": as_of.strftime("%Y%m%d"),
                "FID_ORG_ADJ_PRC": "",
                "FID_ETC_CLS_CODE": "",
            }
        )
        auth_retry = True
        for attempt in range(3):
            self._wait_for_slot()
            response = self._transport.send(
                HttpRequest(
                    "GET",
                    f"{self._base_url}{KIS_FLOW_PATH}?{query}",
                    {
                        "accept": "application/json",
                        "content-type": "application/json; charset=utf-8",
                        "authorization": f"Bearer {self._get_access_token()}",
                        "appkey": self._app_key,
                        "appsecret": self._app_secret,
                        "tr_id": KIS_FLOW_TR_ID,
                        "custtype": "P",
                    },
                ),
                self._timeout,
            )
            if response.status == 401 and auth_retry:
                self._access_token = None
                self._token_expires_at = 0.0
                auth_retry = False
                continue
            if response.status in {429, 500, 502, 503, 504} and attempt < 2:
                self._sleeper(0.5 * (attempt + 1))
                continue
            payload = _json_object(response.body)
            if response.status != 200 or str(payload.get("rt_cd")) != "0":
                code = str(payload.get("msg_cd") or f"HTTP_{response.status}")
                message = str(payload.get("msg1") or "KIS investor flow request failed")
                raise RuntimeError(f"KIS API error {code}: {message}")
            rows = payload.get("output2")
            if not isinstance(rows, list):
                raise TypeError("KIS investor flow output2 is missing")
            return [dict(row) for row in rows if isinstance(row, Mapping)]
        raise RuntimeError("KIS investor flow retry limit exceeded")

    def _get_access_token(self) -> str:
        if self._access_token and self._clock() < self._token_expires_at:
            return self._access_token
        body = json.dumps(
            {
                "grant_type": "client_credentials",
                "appkey": self._app_key,
                "appsecret": self._app_secret,
            }
        ).encode()
        for attempt in range(2):
            response = self._transport.send(
                HttpRequest(
                    "POST",
                    f"{self._base_url}/oauth2/tokenP",
                    {"content-type": "application/json; charset=utf-8"},
                    body,
                ),
                self._timeout,
            )
            payload = _json_object(response.body)
            token = payload.get("access_token")
            if response.status == 200 and isinstance(token, str) and token:
                try:
                    expires_in = float(payload.get("expires_in", 300))
                except (TypeError, ValueError):
                    expires_in = 300
                self._access_token = token
                self._token_expires_at = self._clock() + max(1.0, expires_in - 60.0)
                return token
            code = str(payload.get("error_code") or f"HTTP_{response.status}")
            if code == "EGW00133" and attempt == 0:
                self._sleeper(KIS_TOKEN_COOLDOWN_SECONDS)
                continue
            raise KisTokenRequestError(f"KIS token request failed: {code}")
        raise AssertionError("unreachable")

    def _wait_for_slot(self) -> None:
        with self._request_lock:
            now = self._clock()
            delay = max(0.0, self._next_request_at - now)
            if delay:
                self._sleeper(delay)
                now = self._clock()
            self._next_request_at = now + self._minimum_interval


class KisInvestorFlowCollector:
    def __init__(
        self,
        client: KisInvestorFlowClient,
        repository: OfficialDataRepository,
    ) -> None:
        self._client = client
        self._repository = repository
        self._failures: list[str] = []

    @property
    def failures(self) -> tuple[str, ...]:
        return tuple(self._failures)

    def collect(
        self,
        *,
        symbols: Sequence[str],
        as_of: date,
        completed_through: date,
        retrieved_at: datetime,
        calendar: KrMarketCalendar | None = None,
    ) -> int:
        if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
            raise ValueError("retrieved_at must include a timezone offset")
        session_indexes = (
            resolve_kr_session_indexes_through(
                self._repository,
                calendar,
                completed_through,
            )
            if calendar is not None
            else self._repository.session_indexes()
        )
        retrieved_text = retrieved_at.isoformat()
        stored = 0
        self._failures = []
        for symbol in symbols:
            if not _SYMBOL.fullmatch(symbol):
                self._failures.append(f"invalid symbol: {symbol!r}")
                logger.warning("KIS flow skipped invalid symbol=%r", symbol)
                continue
            rows: list[dict[str, object]] = []
            try:
                payload_rows = self._client.daily_investor_flow(symbol, as_of=as_of)
                rows = _flow_rows(
                    payload_rows,
                    symbol=symbol,
                    completed_through=completed_through,
                    session_indexes=session_indexes,
                    retrieved_text=retrieved_text,
                )
            except KisTokenRequestError as error:
                self._failures.append(str(error))
                logger.warning("KIS flow stopped because token issuance failed: %s", error)
                break
            except (RuntimeError, TypeError, ValueError) as error:
                self._failures.append(f"{symbol}: {error}")
                logger.warning("KIS flow skipped symbol=%s: %s", symbol, error)
                continue
            stored += self._repository.insert_flow_rows(rows)
        return stored


def _flow_rows(
    payload_rows: Sequence[Mapping[str, object]],
    *,
    symbol: str,
    completed_through: date,
    session_indexes: Mapping[date, int],
    retrieved_text: str,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for payload in payload_rows:
        raw_date = str(payload.get("stck_bsop_date", ""))
        if len(raw_date) != 8 or not raw_date.isdigit():
            continue
        session = date(int(raw_date[:4]), int(raw_date[4:6]), int(raw_date[6:]))
        if session > completed_through or session not in session_indexes:
            continue
        if any(
            not str(payload.get(field, "")).replace(",", "").strip()
            for field in _FLOW_AMOUNT_FIELDS
        ):
            continue
        foreign = _decimal_field(payload, "frgn_ntby_tr_pbmn")
        institutional = _decimal_field(payload, "orgn_ntby_tr_pbmn")
        trading_value = _decimal_field(payload, "acml_tr_pbmn")
        if trading_value <= 0:
            continue
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        rows.append(
            {
                "symbol": symbol,
                "session_date": session.isoformat(),
                "session_index": session_indexes[session],
                "available_at": retrieved_text,
                "foreign_net_buy": str(foreign),
                "institutional_net_buy": str(institutional),
                "trading_value": str(trading_value),
                "source": f"kis:{KIS_FLOW_TR_ID}",
                "source_record_id": f"{symbol}:{session.isoformat()}",
                "retrieved_at": retrieved_text,
                "payload_hash": sha256(canonical).hexdigest(),
            }
        )
    return rows


def _decimal_field(payload: Mapping[str, Any], field: str) -> Decimal:
    raw = str(payload.get(field, "")).replace(",", "").strip()
    try:
        return Decimal(raw)
    except InvalidOperation as error:
        raise RuntimeError(f"KIS investor flow field {field} is invalid") from error


def _json_object(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("KIS API returned invalid JSON") from error
    if not isinstance(payload, dict):
        raise TypeError("KIS API response must be an object")
    return payload
