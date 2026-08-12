import unittest
from decimal import Decimal

from toss_trader.config import Settings


class SettingsTest(unittest.TestCase):
    def test_live_trading_defaults_to_disabled(self) -> None:
        settings = Settings.from_mapping({})

        self.assertFalse(settings.trading_enabled)
        self.assertEqual(settings.base_url, "https://openapi.tossinvest.com")
        self.assertEqual(settings.metrics_host, "0.0.0.0")
        self.assertEqual(settings.metrics_port, 9108)

    def test_supports_canonical_and_legacy_credential_names(self) -> None:
        canonical = Settings.from_mapping(
            {
                "TOSS_CLIENT_ID": "canonical-id",
                "TOSS_CLIENT_SECRET": "canonical-secret",
            }
        )
        legacy = Settings.from_mapping(
            {"TOSS_API_KEY": "legacy-id", "TOSS_SECRET_KEY": "legacy-secret"}
        )

        self.assertEqual(
            canonical.require_credentials(), ("canonical-id", "canonical-secret")
        )
        self.assertEqual(legacy.require_credentials(), ("legacy-id", "legacy-secret"))

    def test_only_explicit_true_enables_trading(self) -> None:
        self.assertTrue(
            Settings.from_mapping({"TRADING_ENABLED": "true"}).trading_enabled
        )
        self.assertFalse(
            Settings.from_mapping({"TRADING_ENABLED": "1"}).trading_enabled
        )

    def test_separate_postgres_keys_select_postgres(self) -> None:
        settings = Settings.from_mapping(
            {
                "POSTGRES_HOST": "postgres.internal",
                "POSTGRES_PORT": "5433",
                "POSTGRES_USER": "trader",
                "POSTGRES_PASSWORD": "secret@:/value",
                "POSTGRES_DB": "toss_trader",
            }
        )

        self.assertEqual(settings.market_backend, "postgresql")
        self.assertTrue(settings.database_configured)
        self.assertEqual(
            settings.postgres_connection_parameters(),
            {
                "host": "postgres.internal",
                "port": 5433,
                "user": "trader",
                "password": "secret@:/value",
                "dbname": "toss_trader",
            },
        )
        self.assertEqual(settings.market_db_path, "data/market.db")

    def test_partial_postgres_config_lists_missing_keys(self) -> None:
        settings = Settings.from_mapping({"POSTGRES_HOST": "postgres.internal"})

        self.assertEqual(settings.market_backend, "postgresql")
        self.assertFalse(settings.database_configured)
        self.assertEqual(
            settings.database_missing_keys,
            ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"),
        )
        with self.assertRaisesRegex(ValueError, "POSTGRES_USER"):
            settings.postgres_connection_parameters()

    def test_parses_and_deduplicates_watchlist_strategy_settings(self) -> None:
        settings = Settings.from_mapping(
            {
                "WATCHLIST_SYMBOLS": "005930, aapl,005930",
                "STRATEGY_INTERVAL": "1m",
                "STRATEGY_SHORT_WINDOW": "5",
                "STRATEGY_LONG_WINDOW": "20",
                "PAPER_ORDER_QUANTITY": "2.5",
            }
        )

        self.assertEqual(settings.watchlist_symbols, ("005930", "AAPL"))
        self.assertEqual(settings.strategy_interval, "1m")
        self.assertEqual(settings.strategy_short_window, 5)
        self.assertEqual(settings.strategy_long_window, 20)
        self.assertEqual(settings.paper_order_quantity, Decimal("2.5"))

    def test_parses_market_benchmarks_and_discovery_universe(self) -> None:
        settings = Settings.from_mapping(
            {
                "WATCHLIST_SYMBOLS": "005930",
                "MARKET_BENCHMARK_SYMBOLS": "069500,spy,069500",
                "DISCOVERY_SYMBOLS": "005930,aapl,005930",
                "DISCOVERY_TOP_N": "7",
            }
        )

        self.assertEqual(settings.market_benchmark_symbols, ("069500", "SPY"))
        self.assertEqual(settings.discovery_symbols, ("005930", "AAPL"))
        self.assertEqual(settings.discovery_top_n, 7)

    def test_discovery_defaults_to_watchlist(self) -> None:
        settings = Settings.from_mapping({"WATCHLIST_SYMBOLS": "005930,AAPL"})

        self.assertEqual(settings.discovery_symbols, ("005930", "AAPL"))

    def test_rejects_invalid_watchlist_strategy_settings(self) -> None:
        with self.assertRaises(ValueError):
            Settings.from_mapping({"WATCHLIST_SYMBOLS": "005930;DROP"})
        with self.assertRaises(ValueError):
            Settings.from_mapping(
                {"STRATEGY_SHORT_WINDOW": "60", "STRATEGY_LONG_WINDOW": "20"}
            )
        with self.assertRaises(ValueError):
            Settings.from_mapping({"STRATEGY_LONG_WINDOW": "201"})
        with self.assertRaises(ValueError):
            Settings.from_mapping({"PAPER_ORDER_QUANTITY": "0"})

    def test_parses_and_validates_metrics_listener(self) -> None:
        settings = Settings.from_mapping(
            {"METRICS_HOST": "127.0.0.1", "METRICS_PORT": "9200"}
        )

        self.assertEqual(settings.metrics_host, "127.0.0.1")
        self.assertEqual(settings.metrics_port, 9200)
        with self.assertRaises(ValueError):
            Settings.from_mapping({"METRICS_HOST": ""})
        with self.assertRaises(ValueError):
            Settings.from_mapping({"METRICS_PORT": "70000"})

if __name__ == "__main__":
    unittest.main()
