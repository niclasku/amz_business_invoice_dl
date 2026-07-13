"""Database operations for tracking processed orders and invoices."""
import sqlite3
import logging
import re
from typing import Optional
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
                invoice_position INTEGER,
                invoice_label TEXT,
                filename TEXT,
                downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                paperless_uploaded_at TIMESTAMP,
                FOREIGN KEY (order_id) REFERENCES orders(order_id)
            )
        ''')
        
        # Add completion and logical-identity columns for existing databases.
        try:
            cursor.execute('PRAGMA table_info(invoices)')
            columns = [col[1] for col in cursor.fetchall()]
            if 'paperless_uploaded_at' not in columns:
                cursor.execute('ALTER TABLE invoices ADD COLUMN paperless_uploaded_at TIMESTAMP')
            if 'invoice_position' not in columns:
                cursor.execute('ALTER TABLE invoices ADD COLUMN invoice_position INTEGER')
            if 'invoice_label' not in columns:
                cursor.execute('ALTER TABLE invoices ADD COLUMN invoice_label TEXT')
        except sqlite3.OperationalError:
            pass

        # Old rows did not store a logical slot. Recover it from filenames such
        # as ..._<order-id>_2.pdf; a single invoice or _1 maps to position zero.
        cursor.execute('''
            SELECT rowid, order_id, filename FROM invoices
            WHERE invoice_position IS NULL
        ''')
        for rowid, order_id, filename in cursor.fetchall():
            position = 0
            if order_id and filename:
                match = re.search(
                    rf"{re.escape(order_id)}_(\d+)\.pdf$", filename,
                    re.IGNORECASE,
                )
                if match:
                    position = max(0, int(match.group(1)) - 1)
            cursor.execute(
                "UPDATE invoices SET invoice_position = ? WHERE rowid = ?",
                (position, rowid),
            )
        
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
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_invoices_order_position
            ON invoices(order_id, invoice_position)
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS invoice_health_checks (
                year INTEGER PRIMARY KEY,
                order_id TEXT NOT NULL,
                invoice_url TEXT NOT NULL,
                invoice_position INTEGER NOT NULL DEFAULT 0,
                invoice_label TEXT,
                content_sha256 TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_verified_at TIMESTAMP
            )
        ''')

        # Existing databases predate position/label tracking. Their sentinel was
        # always created from the first extracted invoice, so position 0 is the
        # correct migration default.
        health_columns = {
            row[1]
            for row in cursor.execute(
                "PRAGMA table_info(invoice_health_checks)"
            ).fetchall()
        }
        if "invoice_position" not in health_columns:
            cursor.execute('''
                ALTER TABLE invoice_health_checks
                ADD COLUMN invoice_position INTEGER NOT NULL DEFAULT 0
            ''')
        if "invoice_label" not in health_columns:
            cursor.execute('''
                ALTER TABLE invoice_health_checks ADD COLUMN invoice_label TEXT
            ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS rolling_invoice_sentinels (
                slot TEXT PRIMARY KEY CHECK (slot IN ('active', 'candidate')),
                order_id TEXT NOT NULL,
                order_date TEXT,
                invoice_position INTEGER NOT NULL DEFAULT 0,
                invoice_label TEXT,
                invoice_url TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                selected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_verified_at TIMESTAMP,
                verification_count INTEGER NOT NULL DEFAULT 0
            )
        ''')

        # Preserve the newest legacy yearly sentinel as the initial rolling
        # active sentinel. Its date is refreshed from the order card when it is
        # first rediscovered.
        rolling_count = cursor.execute(
            "SELECT COUNT(*) FROM rolling_invoice_sentinels"
        ).fetchone()[0]
        if rolling_count == 0:
            legacy = cursor.execute('''
                SELECT h.order_id, o.date, h.invoice_position, h.invoice_label,
                       h.invoice_url, h.content_sha256, h.created_at,
                       h.last_verified_at
                FROM invoice_health_checks h
                LEFT JOIN orders o ON o.order_id = h.order_id
                ORDER BY h.year DESC
                LIMIT 1
            ''').fetchone()
            if legacy:
                cursor.execute('''
                    INSERT INTO rolling_invoice_sentinels
                        (slot, order_id, order_date, invoice_position,
                         invoice_label, invoice_url, content_sha256,
                         selected_at, last_verified_at, verification_count)
                    VALUES ('active', ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ''', legacy)
        # The rolling table is now authoritative. Clearing legacy rows prevents
        # an intentionally retired sentinel from being imported again after a
        # restart.
        cursor.execute("DELETE FROM invoice_health_checks")
        
        conn.commit()
        conn.close()
    
    def get_connection(self) -> sqlite3.Connection:
        """Get a database connection."""
        return sqlite3.connect(self.db_path)
    
    def is_invoice_complete(self, order_id: str, invoice_url: str,
                            invoice_position: int, invoice_label: str,
                            label_is_unique: bool, require_file: bool,
                            require_paperless: bool) -> bool:
        """Return whether this exact invoice satisfied configured destinations.

        Amazon rotates document UUIDs and URLs. Prefer an exact endpoint match,
        then match a unique label within the order. For repeated/legacy labels,
        use the persisted position as the least ambiguous fallback.
        """
        invoice_uuid = extract_uuid_from_url(invoice_url)
        endpoint_conditions = []
        parameters = []
        if invoice_uuid:
            endpoint_conditions.append("invoice_uuid = ?")
            parameters.append(invoice_uuid)
        endpoint_conditions.append("invoice_url = ?")
        parameters.append(invoice_url)

        normalized_label = (invoice_label or "").strip().casefold()
        if label_is_unique and normalized_label:
            logical_condition = """
                order_id = ? AND (
                    LOWER(TRIM(invoice_label)) = ?
                    OR (invoice_label IS NULL AND invoice_position = ?)
                )
            """
            logical_parameters = [order_id, normalized_label, invoice_position]
        else:
            logical_condition = "order_id = ? AND invoice_position = ?"
            logical_parameters = [order_id, invoice_position]

        completion_conditions = []
        if require_file:
            completion_conditions.append("filename IS NOT NULL")
        if require_paperless:
            completion_conditions.append("paperless_uploaded_at IS NOT NULL")
        if not completion_conditions:
            return False

        conn = self.get_connection()
        try:
            row = conn.execute(
                f"""
                SELECT rowid FROM invoices
                WHERE (({' OR '.join(endpoint_conditions)})
                       OR ({logical_condition}))
                  AND {' AND '.join(completion_conditions)}
                LIMIT 1
                """,
                parameters + logical_parameters,
            ).fetchone()
            if row is None:
                return False
            # Backfill legacy rows and track benign link reordering without
            # changing the completion timestamps or stored document endpoint.
            conn.execute('''
                UPDATE invoices
                SET invoice_position = ?,
                    invoice_label = COALESCE(invoice_label, ?)
                WHERE rowid = ?
            ''', (invoice_position, invoice_label, row[0]))
            conn.commit()
            return True
        finally:
            conn.close()
    
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
                             invoice_count: int) -> None:
        """Update order metadata after every required invoice is complete."""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Insert or update order
        cursor.execute('''
            INSERT OR REPLACE INTO orders (order_id, date, price, invoice_count, last_checked_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (order_id, date, price, invoice_count))
        
        conn.commit()
        conn.close()
    
    def mark_invoice_downloaded(self, invoice_url: str, order_id: str,
                                filename: Optional[str] = None,
                                paperless_uploaded: bool = False,
                                invoice_position: int = 0,
                                invoice_label: Optional[str] = None) -> None:
        """Mark an invoice as downloaded and optionally as uploaded to paperless.
        
        Args:
            invoice_url: Invoice URL
            order_id: Order ID
            filename: Filename if downloaded locally (None if only uploaded to paperless)
            paperless_uploaded: True if successfully uploaded to paperless
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        
        invoice_uuid = extract_uuid_from_url(invoice_url)
        invoice_hash = get_hash_from_url(invoice_url)
        
        # Determine primary key column
        pk_column = self._get_invoice_primary_key(cursor)
        
        if pk_column == 'invoice_uuid' and invoice_uuid:
            # New schema: use invoice_uuid as primary key
            if paperless_uploaded:
                cursor.execute('''
                    INSERT OR REPLACE INTO invoices (invoice_uuid, invoice_url, invoice_hash, order_id, filename, downloaded_at, paperless_uploaded_at)
                    VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ''', (invoice_uuid, invoice_url, invoice_hash, order_id, filename))
            else:
                cursor.execute('''
                    INSERT OR REPLACE INTO invoices (invoice_uuid, invoice_url, invoice_hash, order_id, filename, downloaded_at, paperless_uploaded_at)
                    VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, NULL)
                ''', (invoice_uuid, invoice_url, invoice_hash, order_id, filename))
        elif pk_column == 'invoice_url':
            # Old schema: use invoice_url as primary key
            if invoice_uuid:
                if paperless_uploaded:
                    cursor.execute('''
                        INSERT OR REPLACE INTO invoices (invoice_url, invoice_uuid, invoice_hash, order_id, filename, downloaded_at, paperless_uploaded_at)
                        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ''', (invoice_url, invoice_uuid, invoice_hash, order_id, filename))
                else:
                    cursor.execute('''
                        INSERT OR REPLACE INTO invoices (invoice_url, invoice_uuid, invoice_hash, order_id, filename, downloaded_at, paperless_uploaded_at)
                        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, NULL)
                    ''', (invoice_url, invoice_uuid, invoice_hash, order_id, filename))
            else:
                if paperless_uploaded:
                    cursor.execute('''
                        INSERT OR REPLACE INTO invoices (invoice_url, invoice_hash, order_id, filename, downloaded_at, paperless_uploaded_at)
                        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ''', (invoice_url, invoice_hash, order_id, filename))
                else:
                    cursor.execute('''
                        INSERT OR REPLACE INTO invoices (invoice_url, invoice_hash, order_id, filename, downloaded_at, paperless_uploaded_at)
                        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, NULL)
                    ''', (invoice_url, invoice_hash, order_id, filename))
        else:
            # Fallback: try with invoice_uuid first
            if invoice_uuid:
                try:
                    if paperless_uploaded:
                        cursor.execute('''
                            INSERT OR REPLACE INTO invoices (invoice_uuid, invoice_url, invoice_hash, order_id, filename, downloaded_at, paperless_uploaded_at)
                            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                        ''', (invoice_uuid, invoice_url, invoice_hash, order_id, filename))
                    else:
                        cursor.execute('''
                            INSERT OR REPLACE INTO invoices (invoice_uuid, invoice_url, invoice_hash, order_id, filename, downloaded_at, paperless_uploaded_at)
                            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, NULL)
                        ''', (invoice_uuid, invoice_url, invoice_hash, order_id, filename))
                except sqlite3.OperationalError:
                    # If that fails, try with invoice_url
                    if paperless_uploaded:
                        cursor.execute('''
                            INSERT OR REPLACE INTO invoices (invoice_url, invoice_uuid, invoice_hash, order_id, filename, downloaded_at, paperless_uploaded_at)
                            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                        ''', (invoice_url, invoice_uuid, invoice_hash, order_id, filename))
                    else:
                        cursor.execute('''
                            INSERT OR REPLACE INTO invoices (invoice_url, invoice_uuid, invoice_hash, order_id, filename, downloaded_at, paperless_uploaded_at)
                            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, NULL)
                        ''', (invoice_url, invoice_uuid, invoice_hash, order_id, filename))
            else:
                # No UUID available, use URL
                if paperless_uploaded:
                    cursor.execute('''
                        INSERT OR REPLACE INTO invoices (invoice_url, invoice_hash, order_id, filename, downloaded_at, paperless_uploaded_at)
                        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ''', (invoice_url, invoice_hash, order_id, filename))
                else:
                    cursor.execute('''
                        INSERT OR REPLACE INTO invoices (invoice_url, invoice_hash, order_id, filename, downloaded_at, paperless_uploaded_at)
                        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, NULL)
                    ''', (invoice_url, invoice_hash, order_id, filename))
        
        if invoice_uuid:
            cursor.execute('''
                UPDATE invoices SET invoice_position = ?, invoice_label = ?
                WHERE invoice_uuid = ?
            ''', (invoice_position, invoice_label, invoice_uuid))
        else:
            cursor.execute('''
                UPDATE invoices SET invoice_position = ?, invoice_label = ?
                WHERE invoice_url = ?
            ''', (invoice_position, invoice_label, invoice_url))

        conn.commit()
        conn.close()
    
    def mark_paperless_uploaded(self, invoice_url: str) -> None:
        """Mark an invoice as successfully uploaded to paperless."""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        invoice_uuid = extract_uuid_from_url(invoice_url)
        
        # Determine primary key column
        pk_column = self._get_invoice_primary_key(cursor)
        
        if pk_column == 'invoice_uuid' and invoice_uuid:
            cursor.execute('''
                UPDATE invoices SET paperless_uploaded_at = CURRENT_TIMESTAMP
                WHERE invoice_uuid = ?
            ''', (invoice_uuid,))
        else:
            cursor.execute('''
                UPDATE invoices SET paperless_uploaded_at = CURRENT_TIMESTAMP
                WHERE invoice_url = ?
            ''', (invoice_url,))
        
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

    def get_invoice_health_check(self, year: int):
        """Return the persisted sentinel invoice for a year, if configured."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT order_id, invoice_url, invoice_position, invoice_label,
                   content_sha256
            FROM invoice_health_checks WHERE year = ?
        ''', (year,))
        row = cursor.fetchone()
        conn.close()
        if not row:
            return None
        return {
            "order_id": row[0],
            "invoice_url": row[1],
            "invoice_position": row[2],
            "invoice_label": row[3],
            "sha256": row[4],
        }

    def create_invoice_health_check(self, year: int, order_id: str,
                                    invoice_url: str, content_sha256: str,
                                    invoice_position: int = 0,
                                    invoice_label: Optional[str] = None) -> None:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO invoice_health_checks
                (year, order_id, invoice_url, invoice_position, invoice_label,
                 content_sha256, last_verified_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (
            year, order_id, invoice_url, invoice_position, invoice_label,
            content_sha256,
        ))
        conn.commit()
        conn.close()

    def mark_invoice_health_check_verified(self, year: int, invoice_url: str,
                                           invoice_label: Optional[str]) -> None:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE invoice_health_checks
            SET invoice_url = ?, invoice_label = ?,
                last_verified_at = CURRENT_TIMESTAMP
            WHERE year = ?
        ''', (invoice_url, invoice_label, year))
        conn.commit()
        conn.close()

    def get_rolling_sentinel(self, slot: str):
        """Return the active or candidate rolling sentinel."""
        conn = self.get_connection()
        row = conn.execute('''
            SELECT order_id, order_date, invoice_position, invoice_label,
                   invoice_url, content_sha256, selected_at,
                   last_verified_at, verification_count
            FROM rolling_invoice_sentinels WHERE slot = ?
        ''', (slot,)).fetchone()
        conn.close()
        if not row:
            return None
        return {
            "slot": slot,
            "order_id": row[0],
            "order_date": row[1],
            "invoice_position": row[2],
            "invoice_label": row[3],
            "invoice_url": row[4],
            "sha256": row[5],
            "selected_at": row[6],
            "last_verified_at": row[7],
            "verification_count": row[8],
        }

    def save_rolling_sentinel(self, slot: str, order_id: str,
                              order_date: str, invoice_position: int,
                              invoice_label: Optional[str], invoice_url: str,
                              content_sha256: str) -> None:
        """Create or replace a rolling sentinel slot."""
        conn = self.get_connection()
        conn.execute('''
            INSERT OR REPLACE INTO rolling_invoice_sentinels
                (slot, order_id, order_date, invoice_position, invoice_label,
                 invoice_url, content_sha256, selected_at,
                 last_verified_at, verification_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, NULL, 0)
        ''', (
            slot, order_id, order_date, invoice_position, invoice_label,
            invoice_url, content_sha256,
        ))
        conn.commit()
        conn.close()

    def verify_rolling_sentinel(self, slot: str, order_date: str,
                                invoice_url: str,
                                invoice_label: Optional[str]) -> None:
        """Record a successful verification using the latest extracted link."""
        conn = self.get_connection()
        conn.execute('''
            UPDATE rolling_invoice_sentinels
            SET order_date = ?, invoice_url = ?, invoice_label = ?,
                last_verified_at = CURRENT_TIMESTAMP,
                verification_count = verification_count + 1
            WHERE slot = ?
        ''', (order_date, invoice_url, invoice_label, slot))
        conn.commit()
        conn.close()

    def promote_rolling_candidate(self) -> None:
        """Atomically replace the active sentinel with the verified candidate."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM rolling_invoice_sentinels WHERE slot = 'active'")
        cursor.execute('''
            UPDATE rolling_invoice_sentinels SET slot = 'active'
            WHERE slot = 'candidate'
        ''')
        conn.commit()
        conn.close()

    def delete_rolling_sentinel(self, slot: str) -> None:
        conn = self.get_connection()
        conn.execute(
            "DELETE FROM rolling_invoice_sentinels WHERE slot = ?", (slot,)
        )
        conn.commit()
        conn.close()
