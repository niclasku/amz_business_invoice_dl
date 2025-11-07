"""Browser setup and navigation operations."""
import os
import time
import logging
from datetime import datetime
from typing import Optional, List
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException


logger = logging.getLogger(__name__)


class Browser:
    """Handles WebDriver setup, login, and navigation."""
    
    def __init__(self, email: str, password: str, min_year: Optional[int] = None):
        """Initialize browser handler.
        
        Args:
            email: Amazon account email
            password: Amazon account password
            min_year: Minimum year to filter orders
        """
        self.email = email
        self.password = password
        self.min_year = min_year
        self.driver = None
        self.wait = None
    
    def setup_driver(self) -> webdriver.Chrome:
        """Configure and return a Chrome WebDriver instance."""
        chrome_options = Options()
        # Run in headless mode
        chrome_options.add_argument('--headless')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
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
        # Also set window size via driver (redundant but ensures it's set)
        driver.set_window_size(1920, 1080)
        return driver
    
    def login(self) -> None:
        """Handle the login process to Amazon Business."""
        logger.info("Logging in to Amazon Business...")
        self.driver.get("https://business.amazon.de")
        self.wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        
        # Store the current window handle
        original_window = self.driver.current_window_handle
        
        # Click the sign-in link
        sign_in_link = self.wait.until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "a[data-signin-link='true']"))
        )
        sign_in_link.click()
        
        # Wait for the new window/tab to open and switch to it
        self.wait.until(lambda d: len(d.window_handles) > 1)
        
        # Switch to the new window
        for window_handle in self.driver.window_handles:
            if window_handle != original_window:
                self.driver.switch_to.window(window_handle)
                break
        
        # Wait for the new page to load
        self.wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        
        # Fill in email
        email_input = self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='email']")))
        email_input.clear()
        email_input.send_keys(self.email)
        
        # Fill in password
        password_input = self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='password']")))
        password_input.clear()
        password_input.send_keys(self.password)
        
        # Click sign-in button
        sign_in_button = self.wait.until(EC.element_to_be_clickable((By.ID, "signInSubmit")))
        sign_in_button.click()
        
        # Wait for navigation to complete after sign-in
        time.sleep(3)
        self.wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        
        logger.info("Sign-in completed successfully")
        
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
    
    def get_years_to_check(self) -> List[int]:
        """Determine which years to check based on current date.
        
        During the first 8 weeks of a new year, check both previous and current year.
        After 8 weeks, only check the current year.
        If min_year is specified, only check years from min_year onwards.
        """
        current_date = datetime.now()
        current_year = current_date.year
        year_start = datetime(current_year, 1, 1)
        days_since_year_start = (current_date - year_start).days
        weeks_since_year_start = days_since_year_start / 7
        
        years_to_check = []
        
        if weeks_since_year_start < 8:
            # First 8 weeks of the year - check both previous and current year
            previous_year = current_year - 1
            
            # If min_year is specified, only include years >= min_year
            if self.min_year:
                if previous_year >= self.min_year:
                    years_to_check.append(previous_year)
                if current_year >= self.min_year:
                    years_to_check.append(current_year)
            else:
                years_to_check = [previous_year, current_year]
            
            if years_to_check:
                logger.info(f"Within first 8 weeks of {current_year}. Checking years: {', '.join(map(str, years_to_check))}.")
        else:
            # After 8 weeks - only check current year (if >= min_year)
            if self.min_year:
                if current_year >= self.min_year:
                    years_to_check = [current_year]
                else:
                    logger.warning(f"Current year {current_year} is before min_year {self.min_year}. No years to check.")
            else:
                years_to_check = [current_year]
            
            if years_to_check:
                logger.info(f"More than 8 weeks into {current_year}. Checking year(s): {', '.join(map(str, years_to_check))}.")
        
        return years_to_check
    
    def navigate_to_order_history(self, year: Optional[int] = None) -> None:
        """Navigate to the order history page, optionally filtered by year."""
        if year:
            order_history_url = f"https://www.amazon.de/gp/css/order-history#time/{year}/pagination/1/"
            logger.info(f"Navigating to order history page (filtered for year {year})...")
            self.driver.get(order_history_url)
        else:
            logger.info("Navigating to order history page (no year filter - will process all visible orders)...")
            self.driver.get("https://www.amazon.de/gp/css/order-history")
        
        self.wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        
        # If no year specified, list available years
        if not year:
            self.list_available_years()
    
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
                logger.info("Use --min-year to filter invoices from a specific year onwards.")
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

