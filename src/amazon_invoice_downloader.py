"""Main orchestrator for Amazon Business invoice downloader."""
import os
import time
import argparse
import hashlib
import logging
import re
import signal
from datetime import date, timedelta
from typing import Optional, List
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from database import Database
from browser import Browser
from order_parser import OrderParser
from invoice_extractor import InvoiceExtractor
from file_handler import FileHandler
from mail_client import FailureNotifier


def configure_logging(level: str = "INFO") -> None:
    """Configure consistent console logging for CLI and container runs."""
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger().setLevel(numeric_level)
    # Selenium and urllib3 DEBUG output contains raw WebDriver responses,
    # including authenticated Amazon cookies. Never include it in application
    # debug logs.
    for noisy_logger in (
        "selenium",
        "urllib3",
        "requests",
    ):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)


def env_value(name: str) -> Optional[str]:
    """Return a non-empty environment value."""
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else None


def env_int(name: str) -> Optional[int]:
    """Return an optional integer environment value."""
    value = env_value(name)
    return int(value) if value is not None else None


def env_int_list(name: str) -> Optional[List[int]]:
    """Parse comma- or whitespace-separated integer values."""
    value = env_value(name)
    if value is None:
        return None
    return [int(item) for item in re.split(r"[\s,]+", value) if item]


def env_bool(name: str, default: bool = False) -> bool:
    """Parse a conventional boolean environment value."""
    value = env_value(name)
    if value is None:
        return default
    normalized = value.casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false, got {value!r}")


class AmazonInvoiceDownloader:
    """Main class for downloading Amazon Business invoices."""
    
    def __init__(self, email: str, password: str, lookback_days: int = 56,
                 output_folder: Optional[str] = None, db_path: str = "invoices.db",
                 paperless_url: Optional[str] = None, paperless_token: Optional[str] = None,
                 paperless_correspondent: Optional[int] = None, paperless_document_type: Optional[int] = None,
                 paperless_tags: Optional[List[int]] = None, paperless_storage_path: Optional[int] = None,
                 clear_otp_inbox: bool = False):
        """Initialize the invoice downloader.
        
        Args:
            email: Amazon account email
            password: Amazon account password
            lookback_days: Number of recent calendar days to inspect
            output_folder: Folder to save downloaded invoices
            db_path: Path to SQLite database file
            paperless_url: Paperless-ngx instance URL
            paperless_token: Paperless-ngx API token
            paperless_correspondent: Paperless-ngx correspondent ID
            paperless_document_type: Paperless-ngx document type ID
            paperless_tags: List of paperless-ngx tag IDs
            paperless_storage_path: Paperless-ngx storage path ID
        """
        self.email = email
        self.password = password
        self.lookback_days = lookback_days
        self.output_folder = output_folder
        self.db_path = db_path
        self.paperless_url = paperless_url.rstrip('/') if paperless_url else None
        self.paperless_token = paperless_token
        self.paperless_correspondent = paperless_correspondent
        self.paperless_document_type = paperless_document_type
        self.paperless_tags = paperless_tags or []
        self.paperless_storage_path = paperless_storage_path
        
        self.logger = logging.getLogger(__name__)
        
        # Initialize modules
        self.database = Database(db_path)
        self.browser = Browser(email, password, clear_otp_inbox)
        self.order_parser = OrderParser()
        self.driver = None
        self.wait = None
        self.invoice_extractor = None
        self.file_handler = None
        self.failure_notifier = FailureNotifier.from_environment()

    @staticmethod
    def _validated_pdf_sha256(pdf_data: bytes) -> str:
        if not pdf_data or not pdf_data.startswith(b"%PDF-"):
            raise RuntimeError("Sentinel invoice download did not return a PDF")
        return hashlib.sha256(pdf_data).hexdigest()

    @staticmethod
    def years_for_window(today: date, cutoff: date) -> List[int]:
        """Return descending calendar years intersecting a rolling window."""
        return list(range(today.year, cutoff.year - 1, -1))

    def _verify_rolling_sentinel(self, slot: str, sentinel: dict,
                                 order_info: dict,
                                 invoice_links: List[dict]) -> None:
        """Verify a rolling sentinel using a freshly extracted invoice URL."""
        position = sentinel["invoice_position"]
        if position >= len(invoice_links):
            raise RuntimeError(
                f"The {slot} sentinel invoice #{position + 1} is no longer "
                f"available for order {sentinel['order_id']}"
            )

        invoice = invoice_links[position]
        self.logger.info(
            "Verifying %s sentinel for order %s using a fresh link...",
            slot,
            sentinel["order_id"],
        )
        pdf_data = self.file_handler.download_invoice(
            invoice["href"], "invoice-health-check.pdf", None
        )
        actual_hash = self._validated_pdf_sha256(pdf_data)
        if actual_hash != sentinel["sha256"]:
            raise RuntimeError(
                f"The {slot} sentinel invoice hash changed for order "
                f"{sentinel['order_id']}"
            )
        order_date = self.order_parser.parse_order_date(order_info["date"])
        self.database.verify_rolling_sentinel(
            slot,
            order_date.date().isoformat(),
            invoice["href"],
            invoice.get("text"),
        )
        self.logger.info("%s sentinel health check passed", slot.capitalize())

    def _create_rolling_candidate(self, observation: dict) -> None:
        """Create a candidate baseline; it must pass on a later run."""
        order_info = observation["order_info"]
        invoice = observation["invoice"]
        position = observation["position"]
        self.logger.info(
            "Creating rolling sentinel candidate from order %s...",
            order_info["order_id"],
        )
        pdf_data = self.file_handler.download_invoice(
            invoice["href"], "invoice-health-check.pdf", None
        )
        content_hash = self._validated_pdf_sha256(pdf_data)
        self.database.save_rolling_sentinel(
            "candidate",
            order_info["order_id"],
            observation["order_date"].isoformat(),
            position,
            invoice.get("text"),
            invoice["href"],
            content_hash,
        )
        self.logger.info(
            "Stored rolling sentinel candidate; it will be verified next run"
        )

    def _sentinel_date(self, sentinel: Optional[dict]) -> Optional[date]:
        if not sentinel or not sentinel.get("order_date"):
            return None
        try:
            return date.fromisoformat(sentinel["order_date"])
        except ValueError:
            parsed = self.order_parser.parse_order_date(sentinel["order_date"])
            return parsed.date() if parsed else None

    def _finish_sentinel_run(self) -> None:
        """Validate active state, promote candidates, and rotate automatically."""
        active = self.run_active_sentinel
        candidate = self.run_candidate_sentinel

        if active and not self.run_active_verified:
            raise RuntimeError(
                f"Active sentinel order {active['order_id']} was not found "
                "inside the configured lookback window"
            )

        if candidate and self.run_candidate_preexisting:
            if self.run_candidate_verified:
                self.database.promote_rolling_candidate()
                self.logger.info("Promoted verified rolling sentinel candidate")
                active = self.database.get_rolling_sentinel("active")
                candidate = None
            elif active:
                self.logger.warning(
                    "Discarding a candidate that was not found in the lookback window"
                )
                self.database.delete_rolling_sentinel("candidate")
                candidate = None
            else:
                raise RuntimeError(
                    "The rolling sentinel candidate was not found inside the "
                    "configured lookback window, and no active sentinel is "
                    "available"
                )

        active_date = self._sentinel_date(active)
        active_age = (date.today() - active_date).days if active_date else None
        stable_age = max(1, min(14, self.lookback_days // 4))
        rotation_age = min(
            max(stable_age + 1, int(self.lookback_days * 0.65)),
            max(1, self.lookback_days - 2),
        )
        needs_candidate = active is None or (
            active_age is not None and active_age >= rotation_age
        )

        if needs_candidate and candidate is None:
            eligible = [
                observation
                for observation in self.run_sentinel_observations
                if (date.today() - observation["order_date"]).days >= stable_age
                and (
                    active is None
                    or observation["order_info"]["order_id"] != active["order_id"]
                )
            ]
            eligible.sort(key=lambda item: item["order_date"], reverse=True)
            if eligible:
                self._create_rolling_candidate(eligible[0])
            elif active is None:
                if self.run_sentinel_observations:
                    self.logger.warning(
                        "No sufficiently stable invoice is available for an "
                        "initial sentinel yet"
                    )
                else:
                    raise RuntimeError(
                        "No invoice links were extracted, so the downloader "
                        "cannot establish a health sentinel"
                    )
            else:
                self.logger.warning(
                    "The active sentinel is due for rotation, but no suitable "
                    "replacement invoice is available"
                )

    def process_order_cards(self, year: int, page: int, cutoff: date,
                            seen_order_ids: set) -> dict:
        """Process one Amazon order page within the rolling date window."""
        time.sleep(3)  # Wait for page to fully load
        
        try:
            # Find all order cards
            order_cards = self.driver.find_elements(By.ID, "orderCard")
            if not order_cards:
                order_cards = self.driver.find_elements(By.CSS_SELECTOR, "div[id='orderCard']")
            
            if not order_cards:
                return {"cards": 0, "new_orders": 0, "reached_cutoff": False}
            
            self.logger.info(f"Found {len(order_cards)} order card(s) to process")
            
            # Create output folder if specified
            if self.output_folder:
                os.makedirs(self.output_folder, exist_ok=True)
                self.logger.info(f"Output folder: {self.output_folder}")

            new_orders = 0
            reached_cutoff = False
            
            for idx, card in enumerate(order_cards, 1):
                try:
                    # Scroll to card
                    try:
                        self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", card)
                        time.sleep(0.3)
                    except:
                        pass
                    
                    # Extract order info
                    order_info = self.order_parser.extract_order_info(card)
                    if not order_info:
                        # Silently skip incomplete orders
                        continue

                    parsed_order_date = self.order_parser.parse_order_date(
                        order_info['date']
                    )
                    if not parsed_order_date:
                        self.logger.warning(
                            "Skipping order %s because its date could not be parsed",
                            order_info['order_id'],
                        )
                        continue
                    order_date = parsed_order_date.date()
                    if order_date < cutoff:
                        reached_cutoff = True
                        continue
                    if order_info['order_id'] in seen_order_ids:
                        continue
                    seen_order_ids.add(order_info['order_id'])
                    new_orders += 1
                    
                    # Log order info only for complete orders
                    self.logger.info(f"Processing order {order_info['order_id']} - Date: {order_info['date']}, Price: {order_info['price']}")
                    
                    # Format date for filename
                    date_formatted = self.order_parser.format_date_for_filename(order_info['date'])
                    
                    # Extract invoice links
                    invoice_links_list = self.invoice_extractor.extract_invoice_links(card)
                    current_invoice_count = len(invoice_links_list)

                    if current_invoice_count == 0:
                        self.logger.warning(
                            "Order %s: No invoice links could be extracted; "
                            "the order was not marked as processed",
                            order_info['order_id'],
                        )
                        continue

                    self.run_extracted_invoice_orders += 1
                    self.run_sentinel_observations.append({
                        "order_info": order_info,
                        "order_date": order_date,
                        "invoice": invoice_links_list[0],
                        "position": 0,
                    })

                    if (
                        self.run_active_sentinel
                        and order_info['order_id']
                        == self.run_active_sentinel['order_id']
                    ):
                        self._verify_rolling_sentinel(
                            "active", self.run_active_sentinel,
                            order_info, invoice_links_list,
                        )
                        self.run_active_verified = True
                    if (
                        self.run_candidate_sentinel
                        and order_info['order_id']
                        == self.run_candidate_sentinel['order_id']
                    ):
                        self._verify_rolling_sentinel(
                            "candidate", self.run_candidate_sentinel,
                            order_info, invoice_links_list,
                        )
                        self.run_candidate_verified = True
                    
                    require_file = bool(self.output_folder)
                    require_paperless = bool(
                        self.file_handler.paperless_url
                        and self.file_handler.paperless_token
                    )
                    normalized_labels = [
                        (invoice.get('text') or '').strip().casefold()
                        for invoice in invoice_links_list
                    ]
                    label_counts = {
                        label: normalized_labels.count(label)
                        for label in normalized_labels if label
                    }
                    new_invoice_links = [
                        (position, invoice)
                        for position, invoice in enumerate(invoice_links_list)
                        if not self.database.is_invoice_complete(
                            order_info['order_id'],
                            invoice['href'],
                            position,
                            invoice.get('text'),
                            label_counts.get(normalized_labels[position], 0) == 1,
                            require_file,
                            require_paperless,
                        )
                    ]
                    
                    if not new_invoice_links:
                        self.logger.info(f"Order {order_info['order_id']}: All {current_invoice_count} invoice(s) already downloaded - skipping")
                        # Still update the stored count in case it changed
                        self.database.mark_order_processed(
                            order_info['order_id'], 
                            order_info['date'], 
                            order_info['price'],
                            current_invoice_count
                        )
                        continue
                    
                    self.logger.info(
                        "Order %s: Found %s invoice(s) that still require processing",
                        order_info['order_id'],
                        len(new_invoice_links),
                    )
                    
                    # Download invoices if output folder or paperless is configured
                    if (self.output_folder or (self.file_handler.paperless_url and self.file_handler.paperless_token)) and new_invoice_links:
                        # Sanitize order_id for filename
                        order_id_safe = order_info['order_id'].replace('/', '-').replace('\\', '-').replace(':', '-')
                        
                        for original_position, inv in new_invoice_links:
                            original_idx = original_position + 1
                            
                            # Generate filename
                            if len(invoice_links_list) > 1:
                                filename = f"AMZ_{date_formatted}_{order_id_safe}_{original_idx}.pdf"
                            else:
                                filename = f"AMZ_{date_formatted}_{order_id_safe}.pdf"
                            
                            # Track success status
                            download_success = False
                            paperless_success = False
                            
                            # Download invoice if output folder is configured
                            pdf_data = None
                            if self.output_folder:
                                self.logger.info(f"Downloading invoice: {inv['text']} -> {filename}")
                                pdf_data = self.file_handler.download_invoice(inv['href'], filename, self.output_folder)
                                if pdf_data and pdf_data.startswith(b"%PDF-"):
                                    download_success = True
                                    self.logger.info(f"Successfully downloaded: {filename}")
                                else:
                                    self.logger.error(f"Failed to download: {filename}")
                                    raise RuntimeError(f"Failed to download invoice {filename}")
                            else:
                                # If only paperless is configured, download to memory only
                                self.logger.info(f"Downloading invoice for paperless upload: {inv['text']} -> {filename}")
                                pdf_data = self.file_handler.download_invoice(inv['href'], filename, None)
                                if pdf_data and pdf_data.startswith(b"%PDF-"):
                                    download_success = True  # Download succeeded (to memory)
                                    self.logger.info(f"Successfully downloaded to memory: {filename}")
                                else:
                                    self.logger.error(f"Failed to download: {filename}")
                                    raise RuntimeError(f"Failed to download invoice {filename}")
                            
                            # Upload to paperless-ngx if configured
                            if self.file_handler.paperless_url and self.file_handler.paperless_token:
                                if pdf_data:
                                    # Parse order date for paperless created field
                                    order_date = self.order_parser.parse_order_date(order_info['date'])
                                    title = f"Amazon Invoice {order_info['order_id']} - {order_info['date']}"
                                    task_uuid = self.file_handler.upload_to_paperless(
                                        pdf_data, 
                                        filename, 
                                        title=title,
                                        created=order_date
                                    )
                                    if task_uuid:
                                        paperless_success = True
                                        self.logger.info(f"Successfully uploaded to paperless-ngx: {filename}")
                                    else:
                                        self.logger.warning(f"Failed to upload to paperless-ngx: {filename}")
                                        raise RuntimeError(f"Paperless upload failed for {filename}")
                                else:
                                    self.logger.warning(f"Cannot upload to paperless-ngx: download failed for {filename}")
                            
                            # Determine if invoice should be marked as complete based on configuration
                            should_mark_complete = False
                            
                            if self.output_folder and (self.file_handler.paperless_url and self.file_handler.paperless_token):
                                # Both methods configured: both must succeed
                                should_mark_complete = download_success and paperless_success
                                if should_mark_complete:
                                    self.logger.info(f"Successfully processed (both download and paperless): {filename}")
                                else:
                                    self.logger.warning(f"Incomplete processing for {filename}: download={download_success}, paperless={paperless_success}")
                            elif self.file_handler.paperless_url and self.file_handler.paperless_token:
                                # Only paperless configured: paperless must succeed
                                should_mark_complete = paperless_success
                                if should_mark_complete:
                                    self.logger.info(f"Successfully processed (paperless): {filename}")
                                else:
                                    self.logger.warning(f"Incomplete processing for {filename}: paperless upload failed")
                            elif self.output_folder:
                                # Only local download configured: download must succeed
                                should_mark_complete = download_success
                                if should_mark_complete:
                                    self.logger.info(f"Successfully processed (local download): {filename}")
                                else:
                                    self.logger.warning(f"Incomplete processing for {filename}: download failed")
                            
                            # Mark invoice in database with appropriate status
                            if should_mark_complete:
                                # Mark as downloaded with paperless status
                                self.database.mark_invoice_downloaded(
                                    inv['href'], 
                                    order_info['order_id'], 
                                    filename if self.output_folder else None,
                                    paperless_uploaded=paperless_success,
                                    invoice_position=original_position,
                                    invoice_label=inv.get('text'),
                                )
                                self.logger.info(f"Marked as complete in database: {filename}")
                            else:
                                # Don't mark as complete, but log the status
                                self.logger.warning(f"Not marking as complete in database due to failed requirements: {filename}")
                    
                    # Mark order as processed with updated invoice count
                    self.database.mark_order_processed(
                        order_info['order_id'], 
                        order_info['date'], 
                        order_info['price'],
                        current_invoice_count
                    )
                    
                    if not invoice_links_list:
                        # Check if order should have invoices (price > 0 and older than 14 days)
                        price_value = self.order_parser.parse_price(order_info['price'])
                        is_old = self.order_parser.is_order_older_than_14_days(order_info['date'])
                        
                        if price_value > 0 and is_old:
                            self.logger.warning(f"Order {order_info['order_id']} has price €{price_value:.2f} and is older than 14 days, but no invoices found!")
                    
                    # Close popover before processing next card
                    self.invoice_extractor.close_popover()
                    time.sleep(0.5)
                    
                except Exception as e:
                    self.logger.error(f"Error processing order card {idx}: {str(e)}")
                    import traceback
                    traceback.print_exc()
                    self.invoice_extractor.close_popover()
                    raise

            self.logger.info(
                "Finished year %s page %s: %s new order(s) inside lookback",
                year, page, new_orders,
            )
            return {
                "cards": len(order_cards),
                "new_orders": new_orders,
                "reached_cutoff": reached_cutoff,
            }
        except Exception as e:
            self.logger.error(f"Error while processing order cards: {str(e)}")
            import traceback
            traceback.print_exc()
            raise
    
    def run(self) -> None:
        """Run the complete invoice download process."""
        global shutdown_requested
        try:
            # Check for shutdown before starting
            if shutdown_requested:
                self.logger.info("Shutdown requested before starting. Exiting...")
                return
                
            self.logger.info("Setting up Chrome driver...")
            self.driver = self.browser.setup_driver()
            # Amazon authentication pages can load noticeably slower in
            # headless/container environments than in a visible local browser.
            self.wait = WebDriverWait(self.driver, 30)
            
            # Check for shutdown after driver setup
            if shutdown_requested:
                self.logger.info("Shutdown requested. Closing browser and exiting...")
                if self.driver:
                    self.driver.quit()
                return
            
            # Initialize modules that need driver
            self.invoice_extractor = InvoiceExtractor(self.driver, self.wait)
            self.file_handler = FileHandler(
                self.driver,
                paperless_url=self.paperless_url,
                paperless_token=self.paperless_token,
                paperless_correspondent=self.paperless_correspondent,
                paperless_document_type=self.paperless_document_type,
                paperless_tags=self.paperless_tags,
                paperless_storage_path=self.paperless_storage_path
            )
            
            # Set driver and wait in browser module
            self.browser.driver = self.driver
            self.browser.wait = self.wait
            
            # Login
            self.browser.clear_configured_otp_inbox()
            self.browser.login()
            
            cutoff = date.today() - timedelta(days=self.lookback_days)
            years_to_check = self.years_for_window(date.today(), cutoff)
            self.logger.info(
                "Checking orders from %s through %s (%s days)",
                cutoff.isoformat(),
                date.today().isoformat(),
                self.lookback_days,
            )

            self.run_active_sentinel = self.database.get_rolling_sentinel("active")
            self.run_candidate_sentinel = self.database.get_rolling_sentinel(
                "candidate"
            )
            for slot, sentinel in (
                ("active", self.run_active_sentinel),
                ("candidate", self.run_candidate_sentinel),
            ):
                sentinel_date = self._sentinel_date(sentinel)
                if sentinel_date and sentinel_date < cutoff:
                    self.logger.info(
                        "Retiring %s sentinel outside the lookback window", slot
                    )
                    self.database.delete_rolling_sentinel(slot)
                    if slot == "active":
                        self.run_active_sentinel = None
                    else:
                        self.run_candidate_sentinel = None

            self.run_candidate_preexisting = bool(self.run_candidate_sentinel)
            self.run_active_verified = False
            self.run_candidate_verified = False
            self.run_extracted_invoice_orders = 0
            self.run_sentinel_observations = []
            seen_order_ids = set()
            total_cards = 0

            # Process every page needed to cover the rolling cutoff. A repeated
            # page produces no new IDs and safely terminates pagination.
            for year in years_to_check:
                page = 1
                self.browser.navigate_to_order_history(year, page)
                while not shutdown_requested:
                    if page > 100:
                        raise RuntimeError(
                            "Order-history pagination exceeded 100 pages"
                        )
                    result = self.process_order_cards(
                        year, page, cutoff, seen_order_ids
                    )
                    total_cards += result["cards"]
                    if result["cards"] == 0 or result["reached_cutoff"]:
                        break
                    if result["new_orders"] == 0:
                        raise RuntimeError(
                            f"Order-history pagination repeated year {year} "
                            f"page {page} before reaching the cutoff"
                        )
                    if not self.browser.navigate_to_next_order_page():
                        break
                    page += 1

            if shutdown_requested:
                self.logger.info("Shutdown requested. Stopping immediately...")
                return
            if total_cards == 0:
                raise RuntimeError("No order cards were returned for the lookback window")

            self._finish_sentinel_run()

            processed_orders = self.database.get_processed_orders_count()
            downloaded_invoices = self.database.get_downloaded_invoices_count()
            self.logger.info(
                "Database Statistics: %s processed orders, %s downloaded invoices",
                processed_orders,
                downloaded_invoices,
            )
            
            if not shutdown_requested:
                self.logger.info("Finished processing the configured lookback window")
            
        except Exception as e:
            self.logger.error(f"An error occurred: {str(e)}")
            import traceback
            traceback.print_exc()
            if self.failure_notifier:
                try:
                    self.failure_notifier.send_failure(e)
                    self.logger.info("Sent failure notification email")
                except Exception as notification_error:
                    self.logger.error(
                        "Could not send failure notification: %s",
                        notification_error,
                    )
            else:
                self.logger.warning("SMTP failure notifications are not configured")
            raise
        finally:
            if self.driver:
                self.logger.info("Closing browser...")
                self.driver.quit()


def parse_schedule_interval(schedule_str: str) -> int:
    """Parse schedule interval string to seconds.
    
    Examples:
        "1h" -> 3600 seconds
        "24h" -> 86400 seconds
        "1d" -> 86400 seconds
        "7d" -> 604800 seconds
        "12h" -> 43200 seconds
    """
    if not schedule_str:
        return 0
    
    # Match pattern: number followed by 'h' (hours) or 'd' (days)
    match = re.match(r'^(\d+)([hd])$', schedule_str.lower())
    if not match:
        raise ValueError(f"Invalid schedule format: {schedule_str}. Use format like '1h', '24h', '1d', '7d'")
    
    value = int(match.group(1))
    unit = match.group(2)
    
    if unit == 'h':
        return value * 3600  # Convert hours to seconds
    elif unit == 'd':
        return value * 86400  # Convert days to seconds
    
    raise ValueError(f"Invalid schedule unit: {unit}. Use 'h' for hours or 'd' for days")


# Global flag for immediate shutdown
shutdown_requested = False
downloader_instance = None  # Reference to downloader instance for immediate shutdown


def signal_handler(signum, frame):
    """Handle shutdown signals - stop immediately."""
    global shutdown_requested, downloader_instance
    shutdown_requested = True
    logger = logging.getLogger(__name__)
    logger.info("Shutdown signal received. Stopping immediately...")
    
    # Close browser immediately if it exists
    if downloader_instance and downloader_instance.driver:
        try:
            logger.info("Closing browser immediately...")
            downloader_instance.driver.quit()
            downloader_instance.driver = None
        except:
            pass


def main():
    """Main entry point."""
    global shutdown_requested, downloader_instance
    
    configure_logging(env_value("LOG_LEVEL") or "INFO")

    # Set up signal handlers for immediate shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    configured_lookback = env_int('LOOKBACK_DAYS')
    parser = argparse.ArgumentParser(
        description="Download Amazon Business invoices. CLI options override environment variables."
    )
    parser.add_argument(
        '--email', default=env_value('AMAZON_EMAIL'),
        help='Amazon account email (env: AMAZON_EMAIL)',
    )
    parser.add_argument(
        '--password', default=env_value('AMAZON_PASSWORD'),
        help='Amazon account password (env: AMAZON_PASSWORD)',
    )
    parser.add_argument(
        '--lookback-days', type=int,
        default=configured_lookback if configured_lookback is not None else 56,
        help='Recent calendar days to inspect (env: LOOKBACK_DAYS; default: 56)',
    )
    parser.add_argument(
        '--output-folder', default=env_value('OUTPUT_FOLDER'),
        help='Local invoice folder (env: OUTPUT_FOLDER)',
    )
    parser.add_argument(
        '--db-path', default=env_value('DB_PATH') or 'invoices.db',
        help='SQLite database path (env: DB_PATH; default: invoices.db)',
    )
    parser.add_argument(
        '--log-level',
        type=str.upper,
        choices=('DEBUG', 'INFO', 'WARNING', 'ERROR'),
        default=(env_value('LOG_LEVEL') or 'INFO').upper(),
        help='Console log verbosity (env: LOG_LEVEL; default: INFO)',
    )
    
    # Paperless-ngx arguments
    parser.add_argument('--paperless-url', default=env_value('PAPERLESS_URL'), help='Paperless URL (env: PAPERLESS_URL)')
    parser.add_argument('--paperless-token', default=env_value('PAPERLESS_TOKEN'), help='Paperless API token (env: PAPERLESS_TOKEN)')
    parser.add_argument('--paperless-correspondent', type=int, default=env_int('PAPERLESS_CORRESPONDENT'), help='Correspondent ID (env: PAPERLESS_CORRESPONDENT)')
    parser.add_argument('--paperless-document-type', type=int, default=env_int('PAPERLESS_DOCUMENT_TYPE'), help='Document type ID (env: PAPERLESS_DOCUMENT_TYPE)')
    parser.add_argument('--paperless-tags', type=int, nargs='+', default=env_int_list('PAPERLESS_TAGS'), help='Tag IDs (env: PAPERLESS_TAGS, comma-separated)')
    parser.add_argument('--paperless-storage-path', type=int, default=env_int('PAPERLESS_STORAGE_PATH'), help='Storage path ID (env: PAPERLESS_STORAGE_PATH)')
    
    # Scheduling argument
    parser.add_argument('--schedule', default=env_value('SCHEDULE'), help='Repeat interval such as 24h or 7d (env: SCHEDULE)')
    parser.add_argument(
        '--clear-otp-inbox',
        action=argparse.BooleanOptionalAction,
        default=env_bool('CLEAR_OTP_INBOX'),
        help='Delete every message in the OTP folder before login (env: CLEAR_OTP_INBOX)',
    )
    
    args = parser.parse_args()
    configure_logging(args.log_level)

    if not args.email:
        parser.error("Amazon email is required (--email or AMAZON_EMAIL)")
    if not args.password:
        parser.error("Amazon password is required (--password or AMAZON_PASSWORD)")
    if args.lookback_days <= 0:
        parser.error("--lookback-days must be greater than zero")
    
    # Validate that either output folder or paperless is configured
    if not args.output_folder and not (args.paperless_url and args.paperless_token):
        parser.error("Either --output-folder or --paperless-url and --paperless-token must be specified")
    
    # Parse schedule interval if provided
    schedule_seconds = 0
    if args.schedule:
        try:
            schedule_seconds = parse_schedule_interval(args.schedule)
            logger = logging.getLogger(__name__)
            logger.info(f"Scheduled mode enabled. Running every {args.schedule} ({schedule_seconds} seconds)")
        except ValueError as e:
            parser.error(str(e))
    
    downloader = AmazonInvoiceDownloader(
        email=args.email,
        password=args.password,
        lookback_days=args.lookback_days,
        output_folder=args.output_folder,
        db_path=args.db_path,
        paperless_url=args.paperless_url,
        paperless_token=args.paperless_token,
        paperless_correspondent=args.paperless_correspondent,
        paperless_document_type=args.paperless_document_type,
        paperless_tags=args.paperless_tags,
        paperless_storage_path=args.paperless_storage_path,
        clear_otp_inbox=args.clear_otp_inbox
    )
    
    # Store reference for signal handler
    downloader_instance = downloader
    
    # Run once or on schedule
    if schedule_seconds > 0:
        # Scheduled mode: run continuously
        logger = logging.getLogger(__name__)
        logger.info("Starting scheduled mode. Container will run continuously.")
        
        run_count = 0
        while not shutdown_requested:
            run_count += 1
            logger.info(f"Starting scheduled run #{run_count}")
            
            try:
                downloader.run()
            except KeyboardInterrupt:
                # Handle keyboard interrupt (Ctrl+C) immediately
                logger.info("Interrupted. Stopping immediately...")
                break
            except Exception as e:
                logger.error(f"Error during scheduled run: {str(e)}")
                import traceback
                traceback.print_exc()
            
            # Check for shutdown immediately after run
            if shutdown_requested:
                logger.info("Shutdown requested. Exiting immediately...")
                break
            
            # Wait for next run - exit immediately if shutdown requested
            if shutdown_requested:
                logger.info("Shutdown requested during wait. Exiting immediately...")
                break
                
            logger.info(f"Waiting {args.schedule} until next run...")
            elapsed = 0
            while elapsed < schedule_seconds and not shutdown_requested:
                # Sleep in smaller chunks (10 seconds) for faster response to shutdown
                sleep_time = min(10, schedule_seconds - elapsed)
                time.sleep(sleep_time)
                elapsed += sleep_time
                
                if shutdown_requested:
                    logger.info("Shutdown requested during wait. Exiting immediately...")
                    break
                    
                if elapsed < schedule_seconds and not shutdown_requested:
                    remaining = schedule_seconds - elapsed
                    hours = remaining // 3600
                    minutes = (remaining % 3600) // 60
                    if hours > 0:
                        logger.debug(f"Next run in {hours}h {minutes}m")
                    else:
                        logger.debug(f"Next run in {minutes}m")
        
        logger.info("Scheduled mode stopped.")
    else:
        # One-time run
        try:
            downloader.run()
        except KeyboardInterrupt:
            logger = logging.getLogger(__name__)
            logger.info("Interrupted. Stopping immediately...")
            if downloader.driver:
                try:
                    downloader.driver.quit()
                except:
                    pass


if __name__ == "__main__":
    main()
