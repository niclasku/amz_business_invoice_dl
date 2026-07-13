import logging
import os
import unittest
from datetime import date
from unittest.mock import patch

from amazon_invoice_downloader import (
    AmazonInvoiceDownloader,
    configure_logging,
    env_bool,
    env_int,
    env_int_list,
    env_value,
    parse_schedule_interval,
)


class ConfigurationTests(unittest.TestCase):
    @patch.dict(
        os.environ,
        {
            "TEXT_SETTING": " value ",
            "INT_SETTING": "2025",
            "LIST_SETTING": "1, 2 3",
            "TRUE_SETTING": "yes",
            "FALSE_SETTING": "off",
        },
        clear=True,
    )
    def test_environment_parsers(self):
        self.assertEqual(env_value("TEXT_SETTING"), "value")
        self.assertEqual(env_int("INT_SETTING"), 2025)
        self.assertEqual(env_int_list("LIST_SETTING"), [1, 2, 3])
        self.assertTrue(env_bool("TRUE_SETTING"))
        self.assertFalse(env_bool("FALSE_SETTING", default=True))
        self.assertIsNone(env_value("MISSING_SETTING"))

    def test_schedule_parser(self):
        self.assertEqual(parse_schedule_interval("12h"), 43_200)
        self.assertEqual(parse_schedule_interval("7d"), 604_800)
        with self.assertRaises(ValueError):
            parse_schedule_interval("daily")

    def test_rolling_window_selects_intersecting_years(self):
        self.assertEqual(
            AmazonInvoiceDownloader.years_for_window(
                date(2026, 1, 20), date(2025, 11, 25)
            ),
            [2026, 2025],
        )
        self.assertEqual(
            AmazonInvoiceDownloader.years_for_window(
                date(2026, 7, 13), date(2026, 5, 18)
            ),
            [2026],
        )

    def test_debug_logging_keeps_protocol_loggers_quiet(self):
        self.addCleanup(configure_logging, "INFO")
        configure_logging("DEBUG")
        self.assertGreaterEqual(
            logging.getLogger("selenium").getEffectiveLevel(), logging.WARNING
        )
        self.assertGreaterEqual(
            logging.getLogger("urllib3").getEffectiveLevel(), logging.WARNING
        )


if __name__ == "__main__":
    unittest.main()
