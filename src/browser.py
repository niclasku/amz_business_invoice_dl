"""Browser setup and navigation operations."""
import os
import time
import logging
from urllib.parse import urlsplit
from datetime import datetime, timezone
from typing import Optional
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException
from mail_client import AmazonOtpMailbox


logger = logging.getLogger(__name__)


class Browser:
    """Handles WebDriver setup, login, and navigation."""
    
    def __init__(self, email: str, password: str,
                 clear_otp_inbox: bool = False):
        """Initialize browser handler.
        
        Args:
            email: Amazon account email
            password: Amazon account password
        """
        self.email = email
        self.password = password
        self.driver = None
        self.wait = None
        self.otp_mailbox = AmazonOtpMailbox.from_environment()
        self.login_attempt_started_at = None
        self.clear_otp_inbox = clear_otp_inbox

    def clear_configured_otp_inbox(self) -> None:
        """Clear the OTP mailbox when explicitly requested by the user."""
        if not self.clear_otp_inbox:
            return
        if not self.otp_mailbox:
            raise RuntimeError(
                "--clear-otp-inbox requires MAIL_IMAP_HOST, "
                "MAIL_IMAP_USERNAME and MAIL_IMAP_PASSWORD"
            )
        logger.warning(
            "Clearing every message from IMAP folder %r before login",
            self.otp_mailbox.folder,
        )
        deleted_count = self.otp_mailbox.clear_folder()
        logger.warning(
            "Permanently deleted %s message(s) from IMAP folder %r",
            deleted_count,
            self.otp_mailbox.folder,
        )

    def _wait_for_interactable(self, by: By, selector: str):
        """Return the first displayed and enabled element matching a selector.

        Amazon sometimes keeps hidden copies of login fields in the DOM. Selenium's
        usual presence check can return one of those copies, which cannot be cleared
        or typed into.
        """
        def find_interactable(driver):
            for element in driver.find_elements(by, selector):
                if element.is_displayed() and element.is_enabled():
                    return element
            return False

        try:
            return self.wait.until(find_interactable)
        except TimeoutException as error:
            self._log_debug_page_state()
            blocking_reason = self._blocking_page_reason()
            if blocking_reason:
                raise RuntimeError(blocking_reason) from error
            logger.error(
                "Timed out waiting for interactable element (%s=%r)",
                by,
                selector,
            )
            raise

    def _log_debug_page_state(self) -> None:
        """Log detailed transient page state only when DEBUG is enabled."""
        if not logger.isEnabledFor(logging.DEBUG):
            return

        try:
            url = urlsplit(self.driver.current_url)
            inputs = self.driver.execute_script(
                """
                return Array.from(document.querySelectorAll('input')).map(element => ({
                    id: element.id || null,
                    name: element.name || null,
                    type: element.type || null,
                    classes: element.className || null,
                    displayed: !!(element.offsetWidth || element.offsetHeight ||
                                  element.getClientRects().length),
                    disabled: element.disabled
                }));
                """
            )
            browser_state = self.driver.execute_script(
                """
                return {
                    readyState: document.readyState,
                    userAgent: navigator.userAgent,
                    webdriver: navigator.webdriver,
                    language: navigator.language,
                    width: window.innerWidth,
                    height: window.innerHeight
                };
                """
            )
            logger.debug(
                "Authentication page state: path=%r, title=%r, browser=%r, inputs=%r",
                url.path,
                self.driver.title,
                browser_state,
                inputs,
            )
        except Exception as state_error:
            logger.debug("Could not inspect authentication page state: %s", state_error)

        try:
            page_source = self.driver.page_source
            if self.password:
                page_source = page_source.replace(
                    self.password, "[REDACTED_AMAZON_PASSWORD]"
                )
            logger.debug("Authentication page HTML:\n%s", page_source)
        except Exception as source_error:
            logger.debug("Could not read authentication page HTML: %s", source_error)

    def _blocking_page_reason(self) -> Optional[str]:
        """Identify known Amazon challenge pages without logging page contents."""
        captcha_selectors = (
            "#captchacharacters",
            "input[name='cvf_captcha_input']",
            "form[action*='validateCaptcha']",
        )
        if any(
            self.driver.find_elements(By.CSS_SELECTOR, selector)
            for selector in captcha_selectors
        ):
            return "Amazon presented a CAPTCHA instead of the sign-in form"

        title = (self.driver.title or "").casefold()
        if "robot check" in title or "captcha" in title:
            return "Amazon presented a browser verification page instead of sign-in"
        return None

    def _dismiss_cookie_banner(self) -> None:
        """Dismiss Amazon's cookie banner when it covers the authentication form."""
        for selector in ("#sp-cc-accept", "input[name='accept']"):
            button = self._find_interactable(By.CSS_SELECTOR, selector)
            if button:
                button.click()
                logger.info("Accepted Amazon cookie settings")
                return

    def _find_interactable(self, by: By, selector: str):
        """Return an interactable matching element, or None."""
        for element in self.driver.find_elements(by, selector):
            if element.is_displayed() and element.is_enabled():
                return element
        return None

    def _is_otp_page(self) -> bool:
        """Return whether Amazon is displaying an OTP/code challenge."""
        current_url = self.driver.current_url.casefold()
        if any(marker in current_url for marker in ("/ap/cvf", "/ap/mfa", "otp")):
            return True

        otp_selectors = (
            "input[name='otpCode']",
            "input[name='code']",
            "input[id*='otp' i]",
            "input[autocomplete='one-time-code']",
        )
        if any(self.driver.find_elements(By.CSS_SELECTOR, selector) for selector in otp_selectors):
            return True

        try:
            body_text = self.driver.find_element(By.TAG_NAME, "body").text.casefold()
        except Exception:
            return False
        return any(
            phrase in body_text
            for phrase in (
                "one-time password",
                "one time password",
                "security code",
                "verification code",
                "einmalpasswort",
                "sicherheitscode",
                "bestätigungscode",
            )
        )

    def _raise_if_otp_required(self) -> None:
        """Handle an Amazon OTP challenge when IMAP is configured."""
        if not self._is_otp_page():
            return

        logger.warning("Amazon requires a one-time password")
        if not self.otp_mailbox:
            raise RuntimeError(
                "Amazon requires a one-time password. Configure "
                "MAIL_IMAP_HOST, MAIL_IMAP_USERNAME and "
                "MAIL_IMAP_PASSWORD to retrieve it automatically"
            )

        not_before = self.login_attempt_started_at or datetime.now(timezone.utc)
        otp_code = self.otp_mailbox.wait_for_code(not_before)
        otp_input = self._wait_for_interactable(
            By.CSS_SELECTOR, "#input-box-otp, input[name='otpCode']"
        )
        self._replace_input_value(otp_input, otp_code)
        submit_button = self._wait_for_interactable(
            By.CSS_SELECTOR, "#cvf-submit-otp-button input[type='submit']"
        )
        challenge_url = self.driver.current_url
        submit_button.click()
        self.wait.until(
            lambda driver: driver.current_url != challenge_url
            or not self._is_otp_page()
        )
        time.sleep(1)
        if self._is_otp_page():
            raise RuntimeError("Amazon rejected the retrieved one-time password")
        logger.info("Amazon OTP verification completed successfully")

    def _submit_email(self) -> None:
        """Enter and submit the configured email on an Amazon sign-in page."""
        email_input = self._wait_for_interactable(
            By.CSS_SELECTOR,
            "#ap_email, input[type='email'], input[name='email'], "
            "input[autocomplete='username']",
        )
        self._replace_input_value(email_input, self.email)

        if not self._find_interactable(
            By.CSS_SELECTOR, "#ap_password, input[type='password']"
        ):
            continue_button = self._wait_for_interactable(
                By.CSS_SELECTOR,
                "input#continue, #continue input[type='submit'], "
                "input[name='continue'], button[name='continue'], "
                "input[aria-labelledby='continue-announce']",
            )
            continue_button.click()

    def _wait_for_password_or_error(self):
        """Wait for the password form, returning its field or Amazon's error text."""
        def password_or_error(driver):
            self._raise_if_otp_required()
            password = self._find_interactable(
                By.CSS_SELECTOR, "#ap_password, input[type='password']"
            )
            if password:
                return (password, None)

            error = self._find_interactable(
                By.CSS_SELECTOR, "#auth-error-message-box, .auth-server-side-message-box"
            )
            if error and error.text.strip():
                return (None, error.text.strip())
            return False

        return self.wait.until(password_or_error)

    def _sign_in_on_current_page(self) -> None:
        """Complete the Amazon login form currently displayed by the browser."""
        self._submit_email()
        password_input, auth_error = self._wait_for_password_or_error()
        if auth_error:
            raise RuntimeError(f"Amazon sign-in failed: {auth_error}")

        self._replace_input_value(password_input, self.password)
        self.login_attempt_started_at = datetime.now(timezone.utc)
        sign_in_button = self._wait_for_interactable(
            By.CSS_SELECTOR,
            "#signInSubmit, input[name='signIn'], button[name='signIn']",
        )
        sign_in_button.click()

        # A successful password submission leaves the sign-in form. Amazon may
        # instead show OTP/CAPTCHA verification, which cannot be treated as a
        # completed login.
        self.wait.until(EC.staleness_of(sign_in_button))
        time.sleep(1)
        self._raise_if_otp_required()
        if "/ap/signin" in self.driver.current_url:
            error = self._find_interactable(
                By.CSS_SELECTOR,
                "#auth-error-message-box, .auth-server-side-message-box",
            )
            detail = error.text.strip() if error else "Amazon returned to sign-in"
            raise RuntimeError(f"Amazon sign-in failed: {detail}")

    @staticmethod
    def _replace_input_value(element, value: str) -> None:
        """Replace an input value without relying on WebElement.clear()."""
        element.click()
        element.send_keys(Keys.CONTROL, "a")
        element.send_keys(value)
    
    def setup_driver(self) -> webdriver.Chrome:
        """Configure and return a Chrome WebDriver instance."""
        chrome_options = Options()
        # Headless by default; set CHROME_HEADLESS=false for interactive debugging.
        headless = os.environ.get('CHROME_HEADLESS', 'true').strip().lower()
        if headless not in ('false', '0', 'no', 'off'):
            # Use Chrome's current headless implementation, which follows the
            # same rendering path as a normal browser window.
            chrome_options.add_argument('--headless=new')
        else:
            logger.info("Chrome is running in visible mode (CHROME_HEADLESS=%s)", headless)
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-blink-features=AutomationControlled')
        chrome_options.add_argument('--lang=en-GB')
        chrome_options.add_experimental_option(
            'excludeSwitches', ['enable-automation']
        )
        chrome_options.add_experimental_option('useAutomationExtension', False)
        # Set window size for headless mode
        chrome_options.add_argument('--window-size=1920,1080')
        
        # Use Chromium in Docker if CHROME_BIN environment variable is set
        chrome_bin = os.environ.get('CHROME_BIN')
        if chrome_bin:
            chrome_options.binary_location = chrome_bin
        
        # Use ChromeDriver path if specified (for Docker)
        # In Selenium 4.x, Service() can take the path directly or use Selenium Manager
        chromedriver_path = os.environ.get('CHROMEDRIVER_PATH')
        if chromedriver_path and os.path.exists(chromedriver_path):
            # Use explicit ChromeDriver path for Docker
            service = Service(chromedriver_path)
        else:
            # Use Service() without driver_path to let Selenium Manager handle ChromeDriver
            service = Service()
        
        driver = webdriver.Chrome(service=service, options=chrome_options)
        driver.set_script_timeout(60)
        # Also set window size via driver (redundant but ensures it's set)
        driver.set_window_size(1920, 1080)

        # Chrome identifies true headless sessions as "HeadlessChrome" in its
        # user agent. Some Amazon authentication variants then omit the normal
        # sign-in form. Keep the real browser version and platform while using
        # the standard Chrome product token.
        user_agent = driver.execute_script("return navigator.userAgent")
        if "HeadlessChrome/" in user_agent:
            driver.execute_cdp_cmd(
                "Network.setUserAgentOverride",
                {
                    "userAgent": user_agent.replace(
                        "HeadlessChrome/", "Chrome/"
                    ),
                    "acceptLanguage": "en-GB,en;q=0.9",
                    "platform": driver.execute_script("return navigator.platform"),
                },
            )
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": (
                    "Object.defineProperty(navigator, 'webdriver', "
                    "{get: () => undefined});"
                )
            },
        )
        return driver
    
    def login(self) -> None:
        """Handle the login process to Amazon Business."""
        logger.info("Logging in to Amazon Business...")
        self.driver.get("https://business.amazon.de")
        self.wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        
        # Amazon's JavaScript click handler turns this Business welcome link
        # into the real authentication flow. Following its raw href directly
        # leads to a /business/register/welcome 404 page. Force the real click
        # into this tab so headless Chrome cannot leave us on a transient blank
        # popup.
        sign_in_link = self.wait.until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "a[data-signin-link='true']"))
        )
        original_url = self.driver.current_url
        original_window = self.driver.current_window_handle
        existing_windows = set(self.driver.window_handles)
        self.driver.execute_script(
            "arguments[0].setAttribute('target', '_self');", sign_in_link
        )
        sign_in_link.click()

        self.wait.until(
            lambda driver: driver.current_url != original_url
            or bool(set(driver.window_handles) - existing_windows)
        )
        new_windows = list(set(self.driver.window_handles) - existing_windows)
        if new_windows:
            self.driver.switch_to.window(new_windows[-1])
        else:
            self.driver.switch_to.window(original_window)

        self.wait.until(
            lambda driver: driver.current_url != "about:blank"
            and driver.execute_script("return document.readyState") == "complete"
        )
        self._dismiss_cookie_banner()
        
        self._submit_email()
        password_input, auth_error = self._wait_for_password_or_error()

        # Amazon's Business landing page now sometimes asks specifically for
        # business credentials. Existing Amazon accounts are directed to the
        # regular Amazon sign-in flow via this link.
        if auth_error:
            retail_sign_in = self._find_interactable(
                By.CSS_SELECTOR, "#retail-signin-ingress-link"
            )
            if not retail_sign_in:
                raise RuntimeError(f"Amazon sign-in failed: {auth_error}")

            logger.info(
                "Business credentials were not recognized; retrying with the "
                "regular Amazon account sign-in flow..."
            )
            retail_sign_in.click()
            self.wait.until(EC.staleness_of(retail_sign_in))
            self._submit_email()
            password_input, auth_error = self._wait_for_password_or_error()
            if auth_error:
                raise RuntimeError(f"Amazon sign-in failed: {auth_error}")

        # Fill in password
        self._replace_input_value(password_input, self.password)
        self.login_attempt_started_at = datetime.now(timezone.utc)
        
        # Click sign-in button
        sign_in_button = self._wait_for_interactable(
            By.CSS_SELECTOR, "#signInSubmit, input[name='signIn'], button[name='signIn']"
        )
        sign_in_button.click()
        
        # Wait for navigation to complete after sign-in
        time.sleep(3)
        self.wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        self._raise_if_otp_required()
        
        logger.info("Amazon Business credential submission completed")
        
        # Dismiss passkey prompt if it appears
        self.dismiss_passkey_prompt()
    
    def dismiss_passkey_prompt(self) -> None:
        """Try to dismiss the passkey prompt if it appears."""
        try:
            time.sleep(2)
            
            skip_found = False
            for tag in ['button', 'a', 'span']:
                try:
                    elements = self.driver.find_elements(By.TAG_NAME, tag)
                    for element in elements:
                        text = element.text.lower()
                        if any(skip_word in text for skip_word in ['not now', 'skip', 'maybe later', 'no thanks', 'dismiss']):
                            if element.is_displayed() and element.is_enabled():
                                element.click()
                                skip_found = True
                                logger.info("Dismissed passkey prompt")
                                time.sleep(2)
                                break
                    if skip_found:
                        break
                except:
                    continue
            
            # Alternative: Look for close buttons
            if not skip_found:
                try:
                    close_buttons = self.driver.find_elements(
                        By.CSS_SELECTOR, 
                        "button[aria-label*='close'], button[aria-label*='Close'], .close-button, [data-action='close']"
                    )
                    for btn in close_buttons:
                        if btn.is_displayed():
                            btn.click()
                            logger.info("Dismissed passkey prompt")
                            time.sleep(2)
                            break
                except:
                    pass
        except Exception as e:
            logger.debug(f"Error while handling passkey prompt (may not be present): {str(e)}")
    
    def navigate_to_order_history(self, year: Optional[int] = None,
                                  page: int = 1) -> None:
        """Navigate to the order history page, optionally filtered by year."""
        # Amazon switches years through client-side hash routing. The existing
        # document and order cards can remain visible briefly after ``get``
        # returns, especially on slower container hosts. Remember one current
        # card so we can wait for that render to be replaced before callers
        # collect the new page's cards.
        previous_cards = self.driver.find_elements(By.ID, "orderCard")
        previous_first_card = previous_cards[0] if previous_cards else None

        if year:
            order_history_url = (
                "https://www.amazon.de/gp/css/order-history"
                f"#time/{year}/pagination/{page}/"
            )
            logger.info(
                "Navigating to order history for year %s, page %s...",
                year,
                page,
            )
            self.driver.get(order_history_url)
        else:
            logger.info("Navigating to order history page (no year filter - will process all visible orders)...")
            self.driver.get("https://www.amazon.de/gp/css/order-history")
        
        self.wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))

        # Business and retail authentication can use separate Amazon sessions.
        # If the order endpoint requests retail authentication, complete that
        # login in place; Amazon will then return to the requested order page.
        if "/ap/signin" in self.driver.current_url:
            logger.info(
                "Order history requires Amazon retail authentication; signing in..."
            )
            self._sign_in_on_current_page()
            self.wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))

        if "/ap/" in self.driver.current_url:
            self._raise_if_otp_required()
            raise RuntimeError(
                "Amazon requires an unsupported additional authentication step"
            )

        if previous_first_card:
            self.wait.until(EC.staleness_of(previous_first_card))

        # Require a short quiet period in which Amazon stops replacing the
        # order-card elements. An empty set is also a valid settled result for
        # a year with no orders.
        render_state = {"card_ids": None, "changed_at": time.monotonic()}

        def order_cards_settled(driver):
            card_ids = tuple(
                card.id for card in driver.find_elements(By.ID, "orderCard")
            )
            now = time.monotonic()
            if card_ids != render_state["card_ids"]:
                render_state["card_ids"] = card_ids
                render_state["changed_at"] = now
                return False
            return now - render_state["changed_at"] >= 1.0

        self.wait.until(order_cards_settled)
        
        # If no year specified, list available years
        if not year:
            self.list_available_years()

    def navigate_to_next_order_page(self) -> bool:
        """Click Amazon's enabled next-page control, returning False at the end."""
        selectors = (
            ".a-pagination li.a-last:not(.a-disabled) a",
            "li.a-last:not(.a-disabled) a",
            "a[aria-label='Next page']",
            "a[aria-label='Nächste Seite']",
        )
        next_link = None
        for selector in selectors:
            next_link = self._find_interactable(By.CSS_SELECTOR, selector)
            if next_link:
                break

        if not next_link:
            # Some layouts expose an unlabelled link whose visible text is the
            # only stable indication that it advances pagination.
            for link in self.driver.find_elements(By.CSS_SELECTOR, ".a-pagination a"):
                text = (link.text or "").strip().casefold()
                if text in {"next", "weiter", "nächste", "nächste seite"}:
                    if link.is_displayed() and link.is_enabled():
                        next_link = link
                        break

        if not next_link:
            logger.info("No enabled next order-history page is available")
            return False

        previous_url = self.driver.current_url
        first_cards = self.driver.find_elements(By.ID, "orderCard")
        first_card = first_cards[0] if first_cards else None
        self.driver.execute_script(
            "arguments[0].setAttribute('target', '_self');", next_link
        )
        next_link.click()

        def page_changed(driver):
            if driver.current_url != previous_url:
                return True
            if first_card:
                try:
                    return not first_card.is_enabled()
                except Exception:
                    return True
            return False

        self.wait.until(page_changed)
        self.wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(2)
        return True
    
    def list_available_years(self) -> None:
        """List available years from the time filter dropdown."""
        try:
            time_filter_dropdown = self.wait.until(EC.presence_of_element_located((By.ID, "timeFilterDropdown")))
            
            select = Select(time_filter_dropdown)
            options = select.options
            
            years = []
            for option in options:
                value = option.get_attribute("value")
                year = self._extract_year_from_value(value)
                if year:
                    years.append(year)
            
            if years:
                logger.info(f"Available years: {', '.join(sorted(years))}")
        except TimeoutException:
            logger.warning("Time filter dropdown not found on the page")
        except Exception as e:
            logger.error(f"Error while extracting time filter options: {str(e)}")
    
    def _extract_year_from_value(self, value: str) -> Optional[str]:
        """Extract year from dropdown value."""
        if value.isdigit() and len(value) == 4:
            return value
        elif value.startswith("timeFilterDropdown_") and len(value.split("_")[-1]) == 4:
            year = value.split("_")[-1]
            return year if year.isdigit() else None
        else:
            import re
            year_match = re.search(r'\b(19|20)\d{2}\b', value)
            return year_match.group() if year_match else None
