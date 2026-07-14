import unittest
from unittest.mock import MagicMock

from selenium.webdriver.common.by import By

from browser import Browser


class BrowserAuthenticationTests(unittest.TestCase):
    def test_submit_email_supports_nested_continue_input(self):
        browser = Browser.__new__(Browser)
        browser.email = "account@example.com"
        email_input = MagicMock()
        continue_button = MagicMock()
        browser._wait_for_interactable = MagicMock(
            side_effect=[email_input, continue_button]
        )
        browser._replace_input_value = MagicMock()
        browser._find_interactable = MagicMock(return_value=None)

        browser._submit_email()

        browser._replace_input_value.assert_called_once_with(
            email_input, "account@example.com"
        )
        continue_selector = browser._wait_for_interactable.call_args_list[1].args
        self.assertEqual(continue_selector[0], By.CSS_SELECTOR)
        self.assertIn("#continue input[type='submit']", continue_selector[1])
        self.assertIn(
            "input[aria-labelledby='continue-announce']", continue_selector[1]
        )
        continue_button.click.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
