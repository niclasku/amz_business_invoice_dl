import os
import sqlite3
import tempfile
import unittest
import logging
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from amazon_invoice_downloader import AmazonInvoiceDownloader
from database import Database
from mail_client import AmazonOtpMailbox, FailureNotifier
from order_parser import OrderParser


class HealthMonitoringTests(unittest.TestCase):
    def _empty_sentinel_downloader(self, path):
        downloader = AmazonInvoiceDownloader.__new__(AmazonInvoiceDownloader)
        downloader.database = Database(path)
        downloader.lookback_days = 56
        downloader.order_parser = OrderParser()
        downloader.file_handler = MagicMock()
        downloader.logger = logging.getLogger("sentinel-test")
        downloader.run_active_sentinel = None
        downloader.run_candidate_sentinel = None
        downloader.run_candidate_preexisting = False
        downloader.run_active_verified = False
        downloader.run_candidate_verified = False
        downloader.run_sentinel_observations = []
        return downloader

    def test_missing_extraction_cannot_silently_bootstrap_health_check(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            downloader = self._empty_sentinel_downloader(path)
            with self.assertRaisesRegex(RuntimeError, "cannot establish"):
                downloader._finish_sentinel_run()
        finally:
            os.unlink(path)

    def test_orphaned_candidate_is_a_failure(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            downloader = self._empty_sentinel_downloader(path)
            downloader.run_candidate_sentinel = {
                "order_id": "missing-order",
                "order_date": date.today().isoformat(),
            }
            downloader.run_candidate_preexisting = True
            with self.assertRaisesRegex(RuntimeError, "no active sentinel"):
                downloader._finish_sentinel_run()
        finally:
            os.unlink(path)

    def test_automatic_candidate_requires_a_later_run_before_promotion(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            downloader = AmazonInvoiceDownloader.__new__(AmazonInvoiceDownloader)
            downloader.database = Database(path)
            downloader.lookback_days = 56
            downloader.order_parser = OrderParser()
            downloader.file_handler = MagicMock()
            downloader.file_handler.download_invoice.return_value = b"%PDF-baseline"
            downloader.logger = logging.getLogger("sentinel-test")
            downloader.run_active_sentinel = None
            downloader.run_candidate_sentinel = None
            downloader.run_candidate_preexisting = False
            downloader.run_active_verified = False
            downloader.run_candidate_verified = False
            downloader.run_sentinel_observations = [{
                "order_info": {
                    "order_id": "order-new",
                    "date": (date.today() - timedelta(days=14)).strftime("%d %B %Y"),
                },
                "order_date": date.today() - timedelta(days=14),
                "invoice": {
                    "text": "Invoice 1",
                    "href": "https://example.test/invoice",
                },
                "position": 0,
            }]

            downloader._finish_sentinel_run()
            candidate = downloader.database.get_rolling_sentinel("candidate")
            self.assertIsNotNone(candidate)
            self.assertIsNone(downloader.database.get_rolling_sentinel("active"))

            downloader.run_candidate_sentinel = candidate
            downloader.run_candidate_preexisting = True
            downloader.run_candidate_verified = True
            downloader.run_sentinel_observations = []
            downloader._finish_sentinel_run()
            self.assertIsNone(downloader.database.get_rolling_sentinel("candidate"))
            self.assertEqual(
                downloader.database.get_rolling_sentinel("active")["order_id"],
                "order-new",
            )
        finally:
            os.unlink(path)

    def test_legacy_health_check_schema_is_migrated(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            connection = sqlite3.connect(path)
            connection.execute(
                """
                CREATE TABLE invoice_health_checks (
                    year INTEGER PRIMARY KEY,
                    order_id TEXT NOT NULL,
                    invoice_url TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_verified_at TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                INSERT INTO invoice_health_checks
                    (year, order_id, invoice_url, content_sha256)
                VALUES (2026, 'order-1', 'https://example.test/old', 'hash')
                """
            )
            connection.commit()
            connection.close()

            database = Database(path)
            rolling = database.get_rolling_sentinel("active")
            self.assertEqual(rolling["order_id"], "order-1")
            self.assertEqual(rolling["invoice_position"], 0)
            self.assertIsNone(database.get_invoice_health_check(2026))
        finally:
            os.unlink(path)

    def test_rolling_candidate_verification_and_promotion(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            database = Database(path)
            database.save_rolling_sentinel(
                "candidate",
                "order-2",
                "2026-06-20",
                0,
                "Invoice 1",
                "https://example.test/initial",
                "abc123",
            )
            database.verify_rolling_sentinel(
                "candidate",
                "2026-06-20",
                "https://example.test/fresh",
                "Invoice 1",
            )
            candidate = database.get_rolling_sentinel("candidate")
            self.assertEqual(candidate["verification_count"], 1)
            self.assertEqual(
                candidate["invoice_url"], "https://example.test/fresh"
            )

            database.promote_rolling_candidate()
            self.assertIsNone(database.get_rolling_sentinel("candidate"))
            self.assertEqual(
                database.get_rolling_sentinel("active")["order_id"], "order-2"
            )
        finally:
            os.unlink(path)

    def test_health_check_round_trip(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            database = Database(path)
            self.assertIsNone(database.get_invoice_health_check(2026))
            database.create_invoice_health_check(
                2026, "order-1", "https://example.test/invoice", "abc123"
            )
            self.assertEqual(
                database.get_invoice_health_check(2026),
                {
                    "order_id": "order-1",
                    "invoice_url": "https://example.test/invoice",
                    "invoice_position": 0,
                    "invoice_label": None,
                    "sha256": "abc123",
                },
            )
            database.mark_invoice_health_check_verified(
                2026, "https://example.test/fresh-invoice", "Invoice 1"
            )
            refreshed = database.get_invoice_health_check(2026)
            self.assertEqual(
                refreshed["invoice_url"],
                "https://example.test/fresh-invoice",
            )
            self.assertEqual(refreshed["invoice_label"], "Invoice 1")
        finally:
            os.unlink(path)

    def test_contextual_otp_parser(self):
        raw_message = (
            b"Date: Sun, 12 Jul 2026 19:52:32 +0000\r\n"
            b"From: amazon.de <account-update@amazon.de>\r\n"
            b"Return-Path: <message@bounces.amazon.de>\r\n"
            b"Subject: amazon.de: Anmeldeversuch\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            b"Wenn du das warst, lautet dein Verifizierungscode:\r\n123456\r\n"
        )
        code = AmazonOtpMailbox.extract_code(
            raw_message,
            datetime(2026, 7, 12, 19, 52, tzinfo=timezone.utc),
        )
        self.assertEqual(code, "123456")

    @patch("mail_client.smtplib.SMTP_SSL")
    def test_failure_notification(self, smtp_ssl):
        notifier = FailureNotifier(
            "smtp.example.test", 465, "user", "password",
            "from@example.test", "to@example.test",
        )
        notifier.send_failure(RuntimeError("test failure"))
        smtp = smtp_ssl.return_value.__enter__.return_value
        smtp.login.assert_called_once_with("user", "password")
        smtp.send_message.assert_called_once()


if __name__ == "__main__":
    unittest.main()
