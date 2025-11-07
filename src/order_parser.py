"""Order information extraction and parsing."""
import re
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict
from selenium.webdriver.common.by import By


logger = logging.getLogger(__name__)


class OrderParser:
    """Handles extraction and parsing of order information."""
    
    @staticmethod
    def extract_order_info(card) -> Optional[Dict[str, str]]:
        """Extract order information from an order card element.
        
        Returns:
            Dictionary with order_id, date, and price, or None if extraction fails
        """
        # Extract order date
        date = None
        try:
            date_elements = card.find_elements(By.CSS_SELECTOR, "#orderCardHeader .a-size-base")
            for elem in date_elements:
                text = elem.text.strip()
                if any(month in text for month in [
                    'Januar', 'Februar', 'März', 'April', 'Mai', 'Juni',
                    'Juli', 'August', 'September', 'Oktober', 'November', 'Dezember',
                    'January', 'February', 'March', 'April', 'May', 'June',
                    'July', 'August', 'September', 'October', 'November', 'December'
                ]):
                    date = text
                    break
        except Exception as e:
            logger.debug(f"Could not extract date: {str(e)}")
        
        # Extract price
        price = None
        try:
            price_elements = card.find_elements(By.CSS_SELECTOR, "#orderCardHeader .a-size-base")
            for elem in price_elements:
                text = elem.text.strip()
                if '€' in text:
                    price = text
                    break
        except Exception as e:
            logger.debug(f"Could not extract price: {str(e)}")
        
        # Extract order ID
        order_id = None
        try:
            order_id_elem = None
            try:
                order_id_elem = card.find_element(By.ID, "orderIdField")
            except:
                order_id_fields = card.find_elements(By.CSS_SELECTOR, "*[id*='orderId'], *[id*='OrderId']")
                if order_id_fields:
                    order_id_elem = order_id_fields[0]
            
            if order_id_elem:
                order_id_text = order_id_elem.text.strip()
                parts = order_id_text.split()
                for part in parts:
                    if '-' in part and len(part) > 10:
                        order_id = part
                        break
        except Exception as e:
            logger.debug(f"Could not extract order ID: {str(e)}")
        
        # Check if order card is complete
        if date is None or order_id is None:
            # Silently skip incomplete order cards
            return None
        
        return {
            'date': date,
            'price': price,
            'order_id': order_id
        }
    
    @staticmethod
    def parse_order_date(date: str) -> Optional[datetime]:
        """Parse order date string to datetime object."""
        try:
            date_clean = date.replace('.', '').strip()
            month_map = {
                'Januar': 'January', 'Februar': 'February', 'März': 'March',
                'April': 'April', 'Mai': 'May', 'Juni': 'June',
                'Juli': 'July', 'August': 'August', 'September': 'September',
                'Oktober': 'October', 'November': 'November', 'Dezember': 'December'
            }
            
            for fmt in ['%d %B %Y', '%d %b %Y', '%d.%m.%Y', '%d %m %Y']:
                try:
                    date_clean_en = date_clean
                    for de, en in month_map.items():
                        date_clean_en = date_clean_en.replace(de, en)
                    
                    return datetime.strptime(date_clean_en, fmt)
                except:
                    continue
            
            return None
        except:
            return None
    
    @staticmethod
    def format_date_for_filename(date: str) -> str:
        """Format date string for use in filename (YYYYMMDD format)."""
        parsed_date = OrderParser.parse_order_date(date)
        if parsed_date:
            return parsed_date.strftime('%Y%m%d')
        return datetime.now().strftime('%Y%m%d')
    
    @staticmethod
    def parse_price(price: str) -> float:
        """Parse price string to float value."""
        try:
            # Remove currency symbol and whitespace
            price_clean = price.replace('€', '').replace(',', '.').strip()
            # Remove any non-digit characters except decimal point
            price_clean = ''.join(c for c in price_clean if c.isdigit() or c == '.')
            return float(price_clean)
        except:
            return 0.0
    
    @staticmethod
    def is_order_older_than_14_days(date: str) -> bool:
        """Check if an order is older than 14 days."""
        parsed_date = OrderParser.parse_order_date(date)
        if not parsed_date:
            return False
        
        days_ago = (datetime.now() - parsed_date).days
        return days_ago > 14

