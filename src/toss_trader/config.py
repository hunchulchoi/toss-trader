from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .models import SYMBOL_PATTERN


@dataclass(frozen=True, slots=True)
class Settings:
    client_id: str | None = None
    client_secret: str | None = None
    account_seq: str | None = None
    base_url: str = "https://openapi.tossinvest.com"
    trading_enabled: bool = False
    paper_db_path: str = "data/paper.db"
    market_db_path: str = "data/market.db"
    postgres_host: str | None = None
    postgres_port: str = "5432"
    postgres_user: str | None = None
    postgres_password: str | None = None
    postgres_database: str | None = None
    watchlist_symbols: tuple[str, ...] = ("005930",)
    market_benchmark_symbols: tuple[str, ...] = ("069500",)
    discovery_symbols: tuple[str, ...] = ("005930",)
    discovery_top_n: int = 10
    dynamic_universe_candidate_count: int = 30
    dynamic_universe_size: int = 15
    strategy_interval: str = "1d"
    strategy_short_window: int = 20
    strategy_long_window: int = 60
    paper_order_quantity: Decimal = Decimal(1)
    paper_initial_cash: Decimal = Decimal(1000000)
    candle_request_interval_seconds: float = 0.25
    metrics_host: str = "0.0.0.0"
    metrics_port: int = 9108

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> Settings:
        short_window = _positive_int(
            "STRATEGY_SHORT_WINDOW", values.get("STRATEGY_SHORT_WINDOW", "20")
        )
        long_window = _positive_int(
            "STRATEGY_LONG_WINDOW", values.get("STRATEGY_LONG_WINDOW", "60")
        )
        if short_window >= long_window:
            raise ValueError("strategy windows must satisfy short_window < long_window")
        if long_window > 199:
            raise ValueError("STRATEGY_LONG_WINDOW must not exceed 199")
        interval = values.get("STRATEGY_INTERVAL", "1d")
        if interval not in {"1m", "1d"}:
            raise ValueError("STRATEGY_INTERVAL must be 1m or 1d")
        quantity = _positive_decimal(
            "PAPER_ORDER_QUANTITY", values.get("PAPER_ORDER_QUANTITY", "1")
        )
        initial_cash = _positive_decimal(
            "PAPER_INITIAL_CASH", values.get("PAPER_INITIAL_CASH", "1000000")
        )
        candle_interval = _non_negative_float(
            "CANDLE_REQUEST_INTERVAL_SECONDS",
            values.get("CANDLE_REQUEST_INTERVAL_SECONDS", "0.25"),
        )
        metrics_host = values.get("METRICS_HOST", "0.0.0.0").strip()
        if not metrics_host:
            raise ValueError("METRICS_HOST must not be empty")
        metrics_port = _port(values.get("METRICS_PORT", "9108"))
        watchlist_symbols = _symbol_list(
            "WATCHLIST_SYMBOLS", values.get("WATCHLIST_SYMBOLS", "005930")
        )
        discovery_top_n = _positive_int(
            "DISCOVERY_TOP_N", values.get("DISCOVERY_TOP_N", "10")
        )
        if discovery_top_n > 50:
            raise ValueError("DISCOVERY_TOP_N must not exceed 50")
        universe_candidates = _positive_int(
            "DYNAMIC_UNIVERSE_CANDIDATE_COUNT",
            values.get("DYNAMIC_UNIVERSE_CANDIDATE_COUNT", "30"),
        )
        universe_size = _positive_int(
            "DYNAMIC_UNIVERSE_SIZE", values.get("DYNAMIC_UNIVERSE_SIZE", "15")
        )
        if universe_candidates > 100:
            raise ValueError("DYNAMIC_UNIVERSE_CANDIDATE_COUNT must not exceed 100")
        if universe_size > universe_candidates * 2:
            raise ValueError(
                "DYNAMIC_UNIVERSE_SIZE must not exceed twice candidate count"
            )
        return cls(
            client_id=values.get("TOSS_CLIENT_ID") or values.get("TOSS_API_KEY"),
            client_secret=values.get("TOSS_CLIENT_SECRET")
            or values.get("TOSS_SECRET_KEY"),
            account_seq=values.get("TOSS_ACCOUNT_SEQ")
            or values.get("TOSSINVEST_ACCOUNT"),
            base_url=values.get(
                "TOSS_API_BASE_URL", "https://openapi.tossinvest.com"
            ).rstrip("/"),
            trading_enabled=values.get("TRADING_ENABLED", "false").lower() == "true",
            paper_db_path=values.get("PAPER_DB_PATH", "data/paper.db"),
            market_db_path=values.get("MARKET_DB_PATH", "data/market.db"),
            postgres_host=values.get("POSTGRES_HOST"),
            postgres_port=values.get("POSTGRES_PORT", "5432"),
            postgres_user=values.get("POSTGRES_USER"),
            postgres_password=values.get("POSTGRES_PASSWORD"),
            postgres_database=values.get("POSTGRES_DB"),
            watchlist_symbols=watchlist_symbols,
            market_benchmark_symbols=_symbol_list(
                "MARKET_BENCHMARK_SYMBOLS",
                values.get("MARKET_BENCHMARK_SYMBOLS", "069500"),
            ),
            discovery_symbols=_symbol_list(
                "DISCOVERY_SYMBOLS",
                values.get("DISCOVERY_SYMBOLS")
                or ",".join(watchlist_symbols),
            ),
            discovery_top_n=discovery_top_n,
            dynamic_universe_candidate_count=universe_candidates,
            dynamic_universe_size=universe_size,
            strategy_interval=interval,
            strategy_short_window=short_window,
            strategy_long_window=long_window,
            paper_order_quantity=quantity,
            paper_initial_cash=initial_cash,
            candle_request_interval_seconds=candle_interval,
            metrics_host=metrics_host,
            metrics_port=metrics_port,
        )

    @classmethod
    def from_env(cls) -> Settings:
        return cls.from_mapping(os.environ)

    def require_credentials(self) -> tuple[str, str]:
        if not self.client_id or not self.client_secret:
            raise ValueError(
                "TOSS_CLIENT_ID and TOSS_CLIENT_SECRET must be injected into the process"
            )
        return self.client_id, self.client_secret

    @property
    def market_backend(self) -> str:
        return "postgresql" if self._has_postgres_values else "sqlite"

    @property
    def database_configured(self) -> bool:
        return self._has_postgres_values and not self.database_missing_keys

    @property
    def database_missing_keys(self) -> tuple[str, ...]:
        fields = (
            ("POSTGRES_HOST", self.postgres_host),
            ("POSTGRES_USER", self.postgres_user),
            ("POSTGRES_PASSWORD", self.postgres_password),
            ("POSTGRES_DB", self.postgres_database),
        )
        return tuple(name for name, value in fields if not value)

    def postgres_connection_parameters(self) -> dict[str, str | int] | None:
        if not self._has_postgres_values:
            return None
        missing = self.database_missing_keys
        if missing:
            raise ValueError(f"missing PostgreSQL settings: {', '.join(missing)}")
        try:
            port = int(self.postgres_port)
        except ValueError as error:
            raise ValueError("POSTGRES_PORT must be an integer") from error
        if not 1 <= port <= 65535:
            raise ValueError("POSTGRES_PORT must be between 1 and 65535")
        return {
            "host": self.postgres_host or "",
            "port": port,
            "user": self.postgres_user or "",
            "password": self.postgres_password or "",
            "dbname": self.postgres_database or "",
        }

    @property
    def _has_postgres_values(self) -> bool:
        return any(
            (
                self.postgres_host,
                self.postgres_user,
                self.postgres_password,
                self.postgres_database,
            )
        )


def _symbol_list(name: str, raw: str) -> tuple[str, ...]:
    symbols = tuple(
        dict.fromkeys(
            symbol.strip().upper() for symbol in raw.split(",") if symbol.strip()
        )
    )
    if not symbols:
        raise ValueError(f"{name} must not be empty")
    if any(not SYMBOL_PATTERN.fullmatch(symbol) for symbol in symbols):
        raise ValueError(f"{name} contains unsupported characters")
    return symbols


def _positive_int(name: str, raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _positive_decimal(name: str, raw: str) -> Decimal:
    try:
        value = Decimal(raw)
    except InvalidOperation as error:
        raise ValueError(f"{name} must be a decimal") from error
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _non_negative_float(name: str, raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be a number") from error
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    return value


def _port(raw: str) -> int:
    value = _positive_int("METRICS_PORT", raw)
    if value > 65535:
        raise ValueError("METRICS_PORT must be between 1 and 65535")
    return value
