"""Database operations for tracking processed orders and invoices."""
import sqlite3
import logging
from typing import Optional, List
from file_handler import extract_uuid_from_url, get_hash_from_url

__all__ = ['Database']


logger = logging.getLogger(__name__)


class Database:
    """Handles all database operations for invoice tracking."""
    
    def __init__(self, db_path: str):
        """Initialize database connection and schema.
        
        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = db_path
        self.init_database()
    
    def init_database(self) -> None:
        """Initialize the SQLite database for tracking processed invoices."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Create orders table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                date TEXT,
                price TEXT,
                invoice_count INTEGER,
                processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_checked_at TIMESTAMP
            )
        ''')
        
        # Create invoices table with invoice_uuid as primary key
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS invoices (
                invoice_uuid TEXT PRIMARY KEY,
                invoice_url TEXT,
                invoice_hash TEXT,
                order_id TEXT,
                filename TEXT,
                downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (order_id) REFERENCES orders(order_id)
            )
        ''')
        
        # Migrate existing data: check if we need to migrate from old schema
        try:
            # Check if invoice_uuid column exists (for old databases)
            cursor.execute('PRAGMA table_info(invoices)')
            columns = [col[1] for col in cursor.fetchall()]
            
            if 'invoice_uuid' not in columns:
                # Old schema detected - add invoice_uuid column
                cursor.execute('ALTER TABLE invoices ADD COLUMN invoice_uuid TEXT')
                
                # Update existing rows with UUIDs extracted from URLs
                cursor.execute('SELECT invoice_url FROM invoices WHERE invoice_uuid IS NULL')
                rows = cursor.fetchall()
                for row in rows:
                    if row[0]:
                        uuid = extract_uuid_from_url(row[0])
                        if uuid:
                            cursor.execute('UPDATE invoices SET invoice_uuid = ? WHERE invoice_url = ? AND invoice_uuid IS NULL', 
                                         (uuid, row[0]))
            else:
                # Column exists, but update any NULL values
                cursor.execute('SELECT invoice_url FROM invoices WHERE invoice_uuid IS NULL')
                rows = cursor.fetchall()
                for row in rows:
                    if row[0]:
                        uuid = extract_uuid_from_url(row[0])
                        if uuid:
                            cursor.execute('UPDATE invoices SET invoice_uuid = ? WHERE invoice_url = ? AND invoice_uuid IS NULL', 
                                         (uuid, row[0]))
        except sqlite3.OperationalError as e:
            # Table might not exist yet or other error
            logger.debug(f"Migration check encountered: {e}")
        
        # Create unique index on invoice_uuid to ensure uniqueness (in case of old schema)
        try:
            cursor.execute('''
                CREATE UNIQUE INDEX IF NOT EXISTS idx_invoices_uuid ON invoices(invoice_uuid)
            ''')
        except sqlite3.OperationalError:
            # Index might already exist or invoice_uuid is already primary key
            pass
        
        # Create index on order_id for faster lookups
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_invoices_order_id ON invoices(order_id)
        ''')
        
        conn.commit()
        conn.close()
    
    def get_connection(self) -> sqlite3.Connection:
        """Get a database connection."""
        return sqlite3.connect(self.db_path)
    
    def get_stored_invoice_count(self, order_id: str) -> int:
        """Get the stored invoice count for an order from the database."""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT invoice_count FROM orders WHERE order_id = ?
        ''', (order_id,))
        
        result = cursor.fetchone()
        conn.close()
        
        return result[0] if result else 0
    
    def is_order_fully_processed(self, order_id: str, invoice_urls: List[str]) -> bool:
        """Check if all invoices for an order have already been downloaded."""
        if not invoice_urls:
            return False
        
        # Extract UUIDs from URLs
        invoice_uuids = []
        for url in invoice_urls:
            uuid = extract_uuid_from_url(url)
            if uuid:
                invoice_uuids.append(uuid)
            else:
                # If we can't extract UUID, fall back to URL-based check
                return False
        
        if not invoice_uuids:
            return False
        
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Check if all invoice UUIDs are in the database AND have been downloaded (have a filename)
        placeholders = ','.join(['?'] * len(invoice_uuids))
        cursor.execute(f'''
            SELECT COUNT(*) FROM invoices 
            WHERE invoice_uuid IN ({placeholders}) AND filename IS NOT NULL
        ''', invoice_uuids)
        
        count = cursor.fetchone()[0]
        conn.close()
        
        return count == len(invoice_uuids)
    
    def _get_invoice_primary_key(self, cursor) -> str:
        """Determine the primary key column for the invoices table."""
        try:
            cursor.execute('PRAGMA table_info(invoices)')
            columns = cursor.fetchall()
            for col in columns:
                # col[5] is pk (1 if primary key, 0 otherwise)
                if col[5] == 1:
                    return col[1]  # col[1] is the column name
            # Default to invoice_uuid for new tables
            return 'invoice_uuid'
        except:
            return 'invoice_uuid'
    
    def mark_order_processed(self, order_id: str, date: str, price: str, 
                           invoice_urls: List[str], invoice_count: int) -> None:
        """Mark an order and its invoices as processed in the database."""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Insert or update order
        cursor.execute('''
            INSERT OR REPLACE INTO orders (order_id, date, price, invoice_count, last_checked_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (order_id, date, price, invoice_count))
        
        # Determine primary key column
        pk_column = self._get_invoice_primary_key(cursor)
        
        # Insert invoice records
        for invoice_url in invoice_urls:
            invoice_uuid = extract_uuid_from_url(invoice_url)
            invoice_hash = get_hash_from_url(invoice_url)
            
            if pk_column == 'invoice_uuid' and invoice_uuid:
                # New schema: use invoice_uuid as primary key
                cursor.execute('''
                    INSERT OR IGNORE INTO invoices (invoice_uuid, invoice_url, invoice_hash, order_id)
                    VALUES (?, ?, ?, ?)
                ''', (invoice_uuid, invoice_url, invoice_hash, order_id))
            elif pk_column == 'invoice_url':
                # Old schema: use invoice_url as primary key, but also store UUID
                if invoice_uuid:
                    cursor.execute('''
                        INSERT OR IGNORE INTO invoices (invoice_url, invoice_uuid, invoice_hash, order_id)
                        VALUES (?, ?, ?, ?)
                    ''', (invoice_url, invoice_uuid, invoice_hash, order_id))
                else:
                    cursor.execute('''
                        INSERT OR IGNORE INTO invoices (invoice_url, invoice_hash, order_id)
                        VALUES (?, ?, ?)
                    ''', (invoice_url, invoice_hash, order_id))
            else:
                # Fallback: try with invoice_uuid
                if invoice_uuid:
                    try:
                        cursor.execute('''
                            INSERT OR IGNORE INTO invoices (invoice_uuid, invoice_url, invoice_hash, order_id)
                            VALUES (?, ?, ?, ?)
                        ''', (invoice_uuid, invoice_url, invoice_hash, order_id))
                    except sqlite3.OperationalError:
                        # If that fails, try with invoice_url
                        cursor.execute('''
                            INSERT OR IGNORE INTO invoices (invoice_url, invoice_uuid, invoice_hash, order_id)
                            VALUES (?, ?, ?, ?)
                        ''', (invoice_url, invoice_uuid, invoice_hash, order_id))
        
        conn.commit()
        conn.close()
    
    def mark_invoice_downloaded(self, invoice_url: str, order_id: str, filename: str) -> None:
        """Mark an invoice as downloaded."""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        invoice_uuid = extract_uuid_from_url(invoice_url)
        invoice_hash = get_hash_from_url(invoice_url)
        
        # Determine primary key column
        pk_column = self._get_invoice_primary_key(cursor)
        
        if pk_column == 'invoice_uuid' and invoice_uuid:
            # New schema: use invoice_uuid as primary key
            cursor.execute('''
                INSERT OR REPLACE INTO invoices (invoice_uuid, invoice_url, invoice_hash, order_id, filename, downloaded_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ''', (invoice_uuid, invoice_url, invoice_hash, order_id, filename))
        elif pk_column == 'invoice_url':
            # Old schema: use invoice_url as primary key
            if invoice_uuid:
                cursor.execute('''
                    INSERT OR REPLACE INTO invoices (invoice_url, invoice_uuid, invoice_hash, order_id, filename, downloaded_at)
                    VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ''', (invoice_url, invoice_uuid, invoice_hash, order_id, filename))
            else:
                cursor.execute('''
                    INSERT OR REPLACE INTO invoices (invoice_url, invoice_hash, order_id, filename, downloaded_at)
                    VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ''', (invoice_url, invoice_hash, order_id, filename))
        else:
            # Fallback: try with invoice_uuid first
            if invoice_uuid:
                try:
                    cursor.execute('''
                        INSERT OR REPLACE INTO invoices (invoice_uuid, invoice_url, invoice_hash, order_id, filename, downloaded_at)
                        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ''', (invoice_uuid, invoice_url, invoice_hash, order_id, filename))
                except sqlite3.OperationalError:
                    # If that fails, try with invoice_url
                    cursor.execute('''
                        INSERT OR REPLACE INTO invoices (invoice_url, invoice_uuid, invoice_hash, order_id, filename, downloaded_at)
                        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ''', (invoice_url, invoice_uuid, invoice_hash, order_id, filename))
            else:
                # No UUID available, use URL
                cursor.execute('''
                    INSERT OR REPLACE INTO invoices (invoice_url, invoice_hash, order_id, filename, downloaded_at)
                    VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ''', (invoice_url, invoice_hash, order_id, filename))
        
        conn.commit()
        conn.close()
    
    def get_processed_orders_count(self) -> int:
        """Get the count of processed orders."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(DISTINCT order_id) FROM orders')
        count = cursor.fetchone()[0]
        conn.close()
        return count
    
    def get_downloaded_invoices_count(self) -> int:
        """Get the count of downloaded invoices."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM invoices WHERE filename IS NOT NULL')
        count = cursor.fetchone()[0]
        conn.close()
        return count

