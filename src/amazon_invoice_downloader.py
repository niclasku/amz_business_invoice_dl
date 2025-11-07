"""Main orchestrator for Amazon Business invoice downloader."""
import os
import time
import argparse
import logging
import re
import signal
from typing import Optional, List
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from database import Database
from browser import Browser
from order_parser import OrderParser
from invoice_extractor import InvoiceExtractor
from file_handler import FileHandler


class AmazonInvoiceDownloader:
    """Main class for downloading Amazon Business invoices."""
    
    def __init__(self, email: str, password: str, min_year: Optional[int] = None, 
                 output_folder: Optional[str] = None, db_path: str = "invoices.db",
                 paperless_url: Optional[str] = None, paperless_token: Optional[str] = None,
                 paperless_correspondent: Optional[int] = None, paperless_document_type: Optional[int] = None,
                 paperless_tags: Optional[List[int]] = None, paperless_storage_path: Optional[int] = None):
        """Initialize the invoice downloader.
        
        Args:
            email: Amazon account email
            password: Amazon account password
            min_year: Minimum year to download invoices from
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
        self.min_year = min_year
        self.output_folder = output_folder
        self.db_path = db_path
        self.paperless_url = paperless_url.rstrip('/') if paperless_url else None
        self.paperless_token = paperless_token
        self.paperless_correspondent = paperless_correspondent
        self.paperless_document_type = paperless_document_type
        self.paperless_tags = paperless_tags or []
        self.paperless_storage_path = paperless_storage_path
        
        # Setup logging
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        self.logger = logging.getLogger(__name__)
        
        # Initialize modules
        self.database = Database(db_path)
        self.browser = Browser(email, password, min_year)
        self.order_parser = OrderParser()
        self.driver = None
        self.wait = None
        self.invoice_extractor = None
        self.file_handler = None
    
    def process_order_cards(self) -> None:
        """Process all order cards and download invoices."""
        time.sleep(3)  # Wait for page to fully load
        
        try:
            # Find all order cards
            order_cards = self.driver.find_elements(By.ID, "orderCard")
            if not order_cards:
                order_cards = self.driver.find_elements(By.CSS_SELECTOR, "div[id='orderCard']")
            
            if not order_cards:
                self.logger.warning("No order cards found on the page")
                return
            
            self.logger.info(f"Found {len(order_cards)} order card(s) to process")
            
            # Create output folder if specified
            if self.output_folder:
                os.makedirs(self.output_folder, exist_ok=True)
                self.logger.info(f"Output folder: {self.output_folder}")
            
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
                    
                    # Log order info only for complete orders
                    self.logger.info(f"Processing order {order_info['order_id']} - Date: {order_info['date']}, Price: {order_info['price']}")
                    
                    # Format date for filename
                    date_formatted = self.order_parser.format_date_for_filename(order_info['date'])
                    
                    # Extract invoice links
                    invoice_links_list = self.invoice_extractor.extract_invoice_links(card)
                    current_invoice_count = len(invoice_links_list)
                    
                    # Get stored invoice count for this order
                    stored_invoice_count = self.database.get_stored_invoice_count(order_info['order_id'])
                    
                    # Only download invoices beyond the stored count
                    # If stored count is 1 and current is 2, only download invoice #2 (index 1)
                    new_invoice_links = invoice_links_list[stored_invoice_count:]
                    
                    if not new_invoice_links:
                        self.logger.info(f"Order {order_info['order_id']}: All {current_invoice_count} invoice(s) already downloaded - skipping")
                        # Still update the stored count in case it changed
                        self.database.mark_order_processed(
                            order_info['order_id'], 
                            order_info['date'], 
                            order_info['price'],
                            [inv['href'] for inv in invoice_links_list],
                            current_invoice_count
                        )
                        continue
                    
                    self.logger.info(f"Order {order_info['order_id']}: Found {len(new_invoice_links)} new invoice(s) to download (invoices {stored_invoice_count + 1} to {current_invoice_count})")
                    
                    # Get invoice URLs for database update
                    invoice_urls = [inv['href'] for inv in invoice_links_list]
                    
                    # Download invoices if output folder or paperless is configured
                    if (self.output_folder or (self.file_handler.paperless_url and self.file_handler.paperless_token)) and new_invoice_links:
                        # Sanitize order_id for filename
                        order_id_safe = order_info['order_id'].replace('/', '-').replace('\\', '-').replace(':', '-')
                        
                        for inv in new_invoice_links:
                            # Find original index in full list for numbering (1-based)
                            original_idx = invoice_links_list.index(inv) + 1
                            
                            # Generate filename
                            if len(invoice_links_list) > 1:
                                filename = f"AMZ_{date_formatted}_{order_id_safe}_{original_idx}.pdf"
                            else:
                                filename = f"AMZ_{date_formatted}_{order_id_safe}.pdf"
                            
                            self.logger.info(f"Downloading invoice: {inv['text']} -> {filename}")
                            pdf_data = self.file_handler.download_invoice(inv['href'], filename, self.output_folder)
                            
                            if pdf_data:
                                # Upload to paperless-ngx if configured
                                if self.file_handler.paperless_url and self.file_handler.paperless_token:
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
                                        self.logger.info(f"Uploaded to paperless-ngx: {filename}")
                                    else:
                                        self.logger.warning(f"Failed to upload to paperless-ngx: {filename}")
                                
                                # Mark as downloaded in database
                                self.database.mark_invoice_downloaded(inv['href'], order_info['order_id'], filename)
                                self.logger.info(f"Successfully processed: {filename}")
                            else:
                                self.logger.error(f"Failed to download: {filename}")
                    
                    # Mark order as processed with updated invoice count
                    self.database.mark_order_processed(
                        order_info['order_id'], 
                        order_info['date'], 
                        order_info['price'],
                        invoice_urls,
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
            
            self.logger.info(f"Finished processing {len(order_cards)} order card(s)")
            
            # Print database statistics
            processed_orders = self.database.get_processed_orders_count()
            downloaded_invoices = self.database.get_downloaded_invoices_count()
            self.logger.info(f"Database Statistics: {processed_orders} processed orders, {downloaded_invoices} downloaded invoices")
        except Exception as e:
            self.logger.error(f"Error while processing order cards: {str(e)}")
            import traceback
            traceback.print_exc()
    
    def run(self) -> None:
        """Run the complete invoice download process."""
        try:
            self.logger.info("Setting up Chrome driver...")
            self.driver = self.browser.setup_driver()
            self.wait = WebDriverWait(self.driver, 10)
            
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
            self.browser.login()
            
            # Get years to check (handles year transition automatically)
            years_to_check = self.browser.get_years_to_check()
            
            # Process each year sequentially
            for year in years_to_check:
                self.logger.info(f"Processing orders for year {year}...")
                
                # Navigate to order history for this year
                self.browser.navigate_to_order_history(year)
                
                # Process order cards for this year
                self.process_order_cards()
                
                # Add a small delay between years
                if len(years_to_check) > 1 and year != years_to_check[-1]:
                    self.logger.info(f"Finished processing year {year}, moving to next year...")
                    time.sleep(2)
            
            self.logger.info("Finished processing all years")
            
        except Exception as e:
            self.logger.error(f"An error occurred: {str(e)}")
            import traceback
            traceback.print_exc()
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


# Global flag for graceful shutdown
shutdown_requested = False


def signal_handler(signum, frame):
    """Handle shutdown signals gracefully."""
    global shutdown_requested
    shutdown_requested = True
    logging.getLogger(__name__).info("Shutdown signal received. Finishing current run...")


def main():
    """Main entry point."""
    global shutdown_requested
    
    # Set up signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    parser = argparse.ArgumentParser(description='Amazon sign-in automation and invoice download')
    parser.add_argument('--email', required=True, help='Email address for Amazon account')
    parser.add_argument('--password', required=True, help='Password for Amazon account')
    parser.add_argument('--min-year', type=int, help='Minimum year to download invoices from (e.g., 2020)')
    parser.add_argument('--output-folder', type=str, help='Folder to save downloaded invoices (e.g., ./invoices)')
    parser.add_argument('--db-path', type=str, default='invoices.db', help='Path to SQLite database file (default: invoices.db)')
    
    # Paperless-ngx arguments
    parser.add_argument('--paperless-url', type=str, help='Paperless-ngx instance URL (e.g., https://paperless.example.com)')
    parser.add_argument('--paperless-token', type=str, help='Paperless-ngx API token')
    parser.add_argument('--paperless-correspondent', type=int, help='Paperless-ngx correspondent ID')
    parser.add_argument('--paperless-document-type', type=int, help='Paperless-ngx document type ID')
    parser.add_argument('--paperless-tags', type=int, nargs='+', help='Paperless-ngx tag IDs (can specify multiple)')
    parser.add_argument('--paperless-storage-path', type=int, help='Paperless-ngx storage path ID')
    
    # Scheduling argument
    parser.add_argument('--schedule', type=str, help='Run on a schedule. Format: "1h" (hours) or "1d" (days). Example: "24h" for daily, "1d" for daily, "12h" for twice daily')
    
    args = parser.parse_args()
    
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
        min_year=args.min_year,
        output_folder=args.output_folder,
        db_path=args.db_path,
        paperless_url=args.paperless_url,
        paperless_token=args.paperless_token,
        paperless_correspondent=args.paperless_correspondent,
        paperless_document_type=args.paperless_document_type,
        paperless_tags=args.paperless_tags,
        paperless_storage_path=args.paperless_storage_path
    )
    
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
            except Exception as e:
                logger.error(f"Error during scheduled run: {str(e)}")
                import traceback
                traceback.print_exc()
            
            if shutdown_requested:
                logger.info("Shutdown requested. Exiting...")
                break
            
            # Wait for next run
            logger.info(f"Waiting {args.schedule} until next run...")
            elapsed = 0
            while elapsed < schedule_seconds and not shutdown_requested:
                time.sleep(min(60, schedule_seconds - elapsed))  # Sleep in 1-minute chunks
                elapsed += min(60, schedule_seconds - elapsed)
                if elapsed < schedule_seconds:
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
        downloader.run()


if __name__ == "__main__":
    main()
