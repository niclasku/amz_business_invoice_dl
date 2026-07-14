import unittest
from unittest.mock import MagicMock, patch

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

    @patch("browser.EC.staleness_of", return_value="old-card-stale")
    @patch("browser.EC.presence_of_element_located", return_value="body-present")
    @patch("browser.time.monotonic", side_effect=[0.0, 0.0, 1.1])
    def test_year_navigation_waits_for_previous_cards_to_be_replaced(
        self, monotonic, presence_of_element_located, staleness_of
    ):
        browser = Browser.__new__(Browser)
        browser.driver = MagicMock()
        browser.driver.current_url = (
            "https://www.amazon.de/gp/css/order-history"
            "#time/2025/pagination/1/"
        )
        previous_card = MagicMock()
        replacement_card = MagicMock()
        browser.driver.find_elements.side_effect = [
            [previous_card],
            [replacement_card],
            [replacement_card],
        ]
        browser.wait = MagicMock()

        browser.navigate_to_order_history(2025, 1)

        staleness_of.assert_called_once_with(previous_card)
        self.assertEqual(
            browser.wait.until.call_args_list[1].args[0], "old-card-stale"
        )
        replacement_settled = browser.wait.until.call_args_list[2].args[0]
        self.assertFalse(replacement_settled(browser.driver))
        self.assertTrue(replacement_settled(browser.driver))


if __name__ == "__main__":
    unittest.main()
