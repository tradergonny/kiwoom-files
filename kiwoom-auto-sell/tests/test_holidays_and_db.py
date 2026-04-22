import importlib
import os
import tempfile
import unittest
from datetime import date


class HolidaysTest(unittest.TestCase):
    def test_may_first_is_market_holiday(self):
        from app.holidays import is_market_holiday

        self.assertTrue(is_market_holiday(date(2025, 5, 1)))
        self.assertTrue(is_market_holiday(date(2026, 5, 1)))
        self.assertTrue(is_market_holiday(date(2027, 5, 1)))
        self.assertFalse(is_market_holiday(date(2026, 5, 4)))


class DbSmokeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        os.environ["KIWOOM_DB_PATH"] = self.db_path

        from app import db as db_module
        importlib.reload(db_module)
        self.db = db_module
        self.db.init_db()

    def tearDown(self):
        os.environ.pop("KIWOOM_DB_PATH", None)
        self.tmp.cleanup()

    def test_config_roundtrip(self):
        self.db.config_set("is_mock", "1")
        self.assertEqual(self.db.config_get("is_mock"), "1")

    def test_strategy_upsert_and_list(self):
        self.db.upsert_strategy(
            stock_code="005930",
            stock_name="삼성전자",
            strategy_type="day",
            params={"case": "A"},
            total_qty=10,
            reserved_qty=0,
        )
        all_items = self.db.list_strategies(active_only=True)
        self.assertEqual(len(all_items), 1)
        self.assertEqual(all_items[0]["stock_code"], "005930")
        self.assertEqual(all_items[0]["strategy_type"], "day")


if __name__ == "__main__":
    unittest.main()
