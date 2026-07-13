import os
import tempfile
import unittest

from database import Database


class InvoiceIdentityTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.database = Database(self.path)

    def tearDown(self):
        os.unlink(self.path)

    def test_rotated_uuid_matches_stable_order_invoice_slot(self):
        original = (
            "https://www.amazon.de/documents/download/"
            "19182d45-59f9-42ca-b9db-9c53853152a0?old=1"
        )
        refreshed = (
            "https://www.amazon.de/documents/download/"
            "29182d45-59f9-42ca-b9db-9c53853152a0"
        )
        other = (
            "https://www.amazon.de/documents/download/"
            "39182d45-59f9-42ca-b9db-9c53853152a0"
        )
        self.database.mark_invoice_downloaded(
            original, "order-1", "invoice.pdf", paperless_uploaded=False,
            invoice_position=0, invoice_label="Rechnung 1",
        )

        self.assertTrue(
            self.database.is_invoice_complete(
                "order-1", refreshed, 0, "Rechnung 1", True, True, False
            )
        )
        self.assertFalse(
            self.database.is_invoice_complete(
                "order-1", other, 1, "Rechnung 2", True, True, False
            )
        )

    def test_unique_label_survives_link_reordering(self):
        original = (
            "https://www.amazon.de/documents/download/"
            "49182d45-59f9-42ca-b9db-9c53853152a0"
        )
        rotated = (
            "https://www.amazon.de/documents/download/"
            "59182d45-59f9-42ca-b9db-9c53853152a0"
        )
        self.database.mark_invoice_downloaded(
            original, "order-1", "invoice.pdf", invoice_position=0,
            invoice_label="Rechnung 1",
        )

        self.assertTrue(
            self.database.is_invoice_complete(
                "order-1", rotated, 1, "Rechnung 1", True, True, False
            )
        )

    def test_legacy_position_match_backfills_label(self):
        original = (
            "https://www.amazon.de/documents/download/"
            "69182d45-59f9-42ca-b9db-9c53853152a0"
        )
        rotated = (
            "https://www.amazon.de/documents/download/"
            "79182d45-59f9-42ca-b9db-9c53853152a0"
        )
        self.database.mark_invoice_downloaded(
            original, "order-1", "invoice.pdf", invoice_position=0,
            invoice_label=None,
        )

        self.assertTrue(
            self.database.is_invoice_complete(
                "order-1", rotated, 0, "Rechnung", True, True, False
            )
        )
        with self.database.get_connection() as connection:
            label = connection.execute(
                "SELECT invoice_label FROM invoices WHERE order_id = 'order-1'"
            ).fetchone()[0]
        self.assertEqual(label, "Rechnung")

    def test_both_destinations_must_be_complete_when_both_are_configured(self):
        url = (
            "https://www.amazon.de/documents/download/"
            "39182d45-59f9-42ca-b9db-9c53853152a0"
        )
        self.database.mark_invoice_downloaded(
            url, "order-1", "invoice.pdf", paperless_uploaded=False,
            invoice_position=0, invoice_label="Rechnung",
        )

        self.assertTrue(
            self.database.is_invoice_complete(
                "order-1", url, 0, "Rechnung", True, True, False
            )
        )
        self.assertFalse(
            self.database.is_invoice_complete(
                "order-1", url, 0, "Rechnung", True, True, True
            )
        )


if __name__ == "__main__":
    unittest.main()
