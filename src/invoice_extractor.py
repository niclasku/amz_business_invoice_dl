"""Invoice link extraction from order cards and popovers."""
import time
import logging
from typing import Optional, List, Dict
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException


logger = logging.getLogger(__name__)


class InvoiceExtractor:
    """Handles extraction of invoice links from order cards."""
    
    def __init__(self, driver, wait):
        """Initialize invoice extractor.
        
        Args:
            driver: Selenium WebDriver instance
            wait: WebDriverWait instance
        """
        self.driver = driver
        self.wait = wait
    
    def find_rechnung_link(self, card) -> Optional[object]:
        """Find the 'Rechnung' link in an order card."""
        invoice_links_candidates = card.find_elements(By.PARTIAL_LINK_TEXT, "Rechnung")
        if not invoice_links_candidates:
            invoice_links_candidates = card.find_elements(
                By.CSS_SELECTOR, 
                "a[href*='invoice'], a.popover-trigger, a[data-action='a-popover']"
            )
        
        for link in invoice_links_candidates:
            link_text = link.text.strip()
            link_href = link.get_attribute("href") or ""
            link_class = link.get_attribute("class") or ""
            
            if ("Rechnung" in link_text and "anfordern" not in link_text.lower()) or \
               ("invoice" in link_href.lower() and "popover" in link_class.lower()):
                return link
        
        return None
    
    def find_active_popover(self) -> Optional[object]:
        """Find the active/visible popover containing invoice links."""
        try:
            # Wait for popover to appear and be visible (use longer timeout for popover - up to 30 seconds)
            popover_wait = WebDriverWait(self.driver, 30)  # Wait up to 30 seconds for popover
            
            # Wait for a visible popover with invoice-list
            def popover_visible(driver):
                popovers = driver.find_elements(By.CSS_SELECTOR, ".a-popover")
                for popover in popovers:
                    try:
                        # Check if popover is visible
                        if not popover.is_displayed():
                            continue
                        
                        aria_hidden = popover.get_attribute("aria-hidden")
                        if aria_hidden == "true":
                            continue
                        
                        # Check if it has invoice-list
                        has_invoice_list = popover.find_elements(By.CSS_SELECTOR, "ul.invoice-list, .invoice-list")
                        if has_invoice_list:
                            return popover
                    except:
                        continue
                return False
            
            # Wait for visible popover
            popover_wait.until(popover_visible)
            time.sleep(0.3)  # Give it a moment to fully render
            
            # Find the visible popover with invoice-list
            popovers = self.driver.find_elements(By.CSS_SELECTOR, ".a-popover")
            for popover in popovers:
                try:
                    if not popover.is_displayed():
                        continue
                    
                    aria_hidden = popover.get_attribute("aria-hidden")
                    if aria_hidden == "true":
                        continue
                    
                    has_invoice_list = popover.find_elements(By.CSS_SELECTOR, "ul.invoice-list, .invoice-list")
                    if has_invoice_list:
                        return popover
                except Exception as e:
                    logger.debug(f"Error checking popover: {str(e)}")
                    continue
            
            logger.warning("No visible popover found after wait")
            return None
        except TimeoutException:
            logger.warning("Popover did not appear within timeout")
            return None
        except Exception as e:
            logger.error(f"Error finding popover: {str(e)}")
            return None
    
    def extract_invoice_links(self, card) -> List[Dict[str, str]]:
        """Extract invoice links from the popover after clicking 'Rechnung'."""
        invoice_links_list = []
        
        # Close any existing popovers
        try:
            self.driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            time.sleep(0.3)
        except:
            pass
        
        # Find and click the "Rechnung" link
        invoice_link = self.find_rechnung_link(card)
        if not invoice_link:
            logger.debug("Could not find Rechnung link")
            return invoice_links_list
        
        # Scroll to and click the link
        try:
            self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", invoice_link)
            time.sleep(0.2)
        except:
            pass
        
        invoice_link.click()
        time.sleep(1)
        
        # Find the active popover
        active_popover = self.find_active_popover()
        
        if not active_popover:
            logger.warning("Could not find active popover")
            return invoice_links_list
        
        # Search within the active popover for invoice links
        invoice_links = active_popover.find_elements(By.CSS_SELECTOR, "a[href*='invoice.pdf']")
        
        for link in invoice_links:
            try:
                href = link.get_attribute("href") or ""
                
                if href and "invoice.pdf" in href:
                    if any(inv['href'] == href for inv in invoice_links_list):
                        continue
                    
                    # Get link text
                    text = link.text.strip()
                    if not text:
                        text = link.get_attribute("textContent") or ""
                    if not text:
                        text = link.get_attribute("innerText") or ""
                    if not text:
                        try:
                            text_elem = link.find_element(By.TAG_NAME, "span")
                            if text_elem:
                                text = text_elem.text.strip()
                        except:
                            pass
                    
                    if not text:
                        text = f"Rechnung {len(invoice_links_list) + 1}"
                    
                    invoice_links_list.append({"text": text.strip(), "href": href})
            except Exception as e:
                logger.debug(f"Error processing invoice link: {str(e)}")
                continue
        
        # Remove duplicates
        seen_hrefs = set()
        unique_invoice_links = []
        for inv in invoice_links_list:
            if inv['href'] not in seen_hrefs:
                seen_hrefs.add(inv['href'])
                unique_invoice_links.append(inv)
        
        return unique_invoice_links
    
    def close_popover(self) -> None:
        """Close any open popovers."""
        try:
            self.driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            time.sleep(0.5)
        except:
            pass

