import unittest

from toss_trader.sectors import ksic_to_sector


class SectorsTest(unittest.TestCase):
    def test_maps_known_ksic_codes(self) -> None:
        self.assertEqual(ksic_to_sector("2612"), "전기전자")
        self.assertEqual(ksic_to_sector("264"), "전기전자")
        self.assertEqual(ksic_to_sector("21212"), "의약품")
        self.assertEqual(ksic_to_sector("30121"), "운수장비")
        self.assertEqual(ksic_to_sector("23322"), "비금속광물")
        self.assertEqual(ksic_to_sector("29229"), "기계")
        self.assertEqual(ksic_to_sector("41112"), "건설업")
        self.assertEqual(ksic_to_sector("108"), "음식료품")
        self.assertEqual(ksic_to_sector("70113"), "서비스업")
        self.assertEqual(ksic_to_sector("551"), "서비스업")
        self.assertEqual(ksic_to_sector("681"), "서비스업")
        self.assertEqual(ksic_to_sector("762"), "서비스업")

    def test_defaults_unknown_to_unknown_cluster(self) -> None:
        self.assertEqual(ksic_to_sector(None), "UNKNOWN")
        self.assertEqual(ksic_to_sector(""), "UNKNOWN")
        self.assertEqual(ksic_to_sector("99999"), "UNKNOWN")
        self.assertEqual(ksic_to_sector("1"), "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
