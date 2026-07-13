import unittest
from unittest.mock import MagicMock

from browser import Browser


class BrowserPaginationTests(unittest.TestCase):
    def test_no_next_control_ends_pagination(self):
        browser = Browser.__new__(Browser)
        browser.driver = MagicMock()
        browser.driver.find_elements.return_value = []
        browser._find_interactable = MagicMock(return_value=None)

        self.assertFalse(browser.navigate_to_next_order_page())

    def test_enabled_next_control_is_clicked(self):
        browser = Browser.__new__(Browser)
        browser.driver = MagicMock()
        browser.driver.current_url = "https://www.amazon.de/orders"
        browser.driver.find_elements.return_value = []
        browser.wait = MagicMock()
        next_link = MagicMock()
        browser._find_interactable = MagicMock(return_value=next_link)

        self.assertTrue(browser.navigate_to_next_order_page())
        next_link.click.assert_called_once_with()
        browser.driver.execute_script.assert_called_once()


if __name__ == "__main__":
    unittest.main()
