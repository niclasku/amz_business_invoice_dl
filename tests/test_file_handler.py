import base64
import unittest
from unittest.mock import MagicMock, patch

from file_handler import FileHandler


class FileHandlerTests(unittest.TestCase):
    @patch("file_handler.urllib.request.urlopen")
    def test_non_pdf_direct_response_retries_in_browser(self, urlopen):
        response = MagicMock()
        response.read.return_value = b"<html>sign-in</html>"
        urlopen.return_value.__enter__.return_value = response

        driver = MagicMock()
        driver.get_cookies.return_value = []
        driver.current_url = "https://www.amazon.de/gp/css/order-history"
        driver.execute_script.return_value = "Mozilla/5.0 Chrome/150.0"
        driver.execute_async_script.return_value = {
            "ok": True,
            "status": 200,
            "contentType": "application/pdf",
            "data": base64.b64encode(b"%PDF-browser-result").decode("ascii"),
        }

        result = FileHandler(driver).download_invoice(
            "/documents/example/invoice.pdf", "invoice.pdf"
        )

        self.assertEqual(result, b"%PDF-browser-result")
        driver.execute_async_script.assert_called_once()


if __name__ == "__main__":
    unittest.main()
