"""File operations for downloading invoices and uploading to paperless-ngx."""
import os
import re
import hashlib
import urllib.request
import requests
import logging
from typing import Optional
from datetime import datetime


logger = logging.getLogger(__name__)


def extract_uuid_from_url(url: str) -> Optional[str]:
    """Extract UUID from invoice download URL.
    
    Example: https://www.amazon.de/documents/download/19182d45-59f9-42ca-b9db-9c53853152a0
    Returns: 19182d45-59f9-42ca-b9db-9c53853152a0
    """
    if not url:
        return None
    
    # Pattern to match UUID in the URL (format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx)
    uuid_pattern = r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})'
    match = re.search(uuid_pattern, url, re.IGNORECASE)
    
    if match:
        return match.group(1).lower()
    
    return None


def get_hash_from_url(url: str) -> str:
    """Generate a hash from invoice URL for tracking."""
    return hashlib.md5(url.encode()).hexdigest()


class FileHandler:
    """Handles file download and paperless-ngx upload operations."""
    
    def __init__(self, driver, paperless_url: Optional[str] = None, 
                 paperless_token: Optional[str] = None,
                 paperless_correspondent: Optional[int] = None,
                 paperless_document_type: Optional[int] = None,
                 paperless_tags: Optional[list] = None,
                 paperless_storage_path: Optional[int] = None):
        """Initialize file handler.
        
        Args:
            driver: Selenium WebDriver instance
            paperless_url: Paperless-ngx instance URL
            paperless_token: Paperless-ngx API token
            paperless_correspondent: Paperless-ngx correspondent ID
            paperless_document_type: Paperless-ngx document type ID
            paperless_tags: List of paperless-ngx tag IDs
            paperless_storage_path: Paperless-ngx storage path ID
        """
        self.driver = driver
        self.paperless_url = paperless_url.rstrip('/') if paperless_url else None
        self.paperless_token = paperless_token
        self.paperless_correspondent = paperless_correspondent
        self.paperless_document_type = paperless_document_type
        self.paperless_tags = paperless_tags or []
        self.paperless_storage_path = paperless_storage_path
    
    def download_invoice(self, invoice_url: str, filename: str, output_folder: Optional[str] = None) -> Optional[bytes]:
        """Download a single invoice PDF and return the file data.
        
        Returns:
            bytes: PDF file data if successful, None otherwise
        """
        try:
            # Get cookies from selenium session
            cookies = self.driver.get_cookies()
            
            # Convert relative URL to absolute if needed
            if invoice_url.startswith('/'):
                invoice_url = f"https://www.amazon.de{invoice_url}"
            
            # Build cookie header string
            cookie_header = '; '.join([f"{cookie['name']}={cookie['value']}" for cookie in cookies])
            
            # Create request with cookies and user agent
            req = urllib.request.Request(invoice_url)
            req.add_header('Cookie', cookie_header)
            req.add_header('User-Agent', self.driver.execute_script("return navigator.userAgent;"))
            
            # Download the PDF
            with urllib.request.urlopen(req) as response:
                pdf_data = response.read()
            
            # Save to file if output folder is specified
            if output_folder:
                filepath = os.path.join(output_folder, filename)
                with open(filepath, 'wb') as f:
                    f.write(pdf_data)
            
            return pdf_data
        except Exception as e:
            logger.error(f"Error downloading {filename}: {str(e)}")
            return None
    
    def upload_to_paperless(self, pdf_data: bytes, filename: str, title: Optional[str] = None, 
                           created: Optional[datetime] = None) -> Optional[str]:
        """Upload a document to paperless-ngx via REST API.
        
        Args:
            pdf_data: PDF file data as bytes
            filename: Original filename
            title: Optional title for the document
            created: Optional creation date/time for the document
            
        Returns:
            str: Task UUID if successful, None otherwise
        """
        if not self.paperless_url or not self.paperless_token:
            return None
        
        try:
            url = f"{self.paperless_url}/api/documents/post_document/"
            
            # Prepare headers
            headers = {
                'Authorization': f'Token {self.paperless_token}'
            }
            
            # Prepare form data
            files = {
                'document': (filename, pdf_data, 'application/pdf')
            }
            
            data = {}
            if title:
                data['title'] = title
            if created:
                # Format datetime according to paperless-ngx format
                if isinstance(created, datetime):
                    # Format: "2016-04-19" or "2016-04-19 06:15:00+02:00"
                    if created.tzinfo:
                        data['created'] = created.strftime('%Y-%m-%d %H:%M:%S%z')
                    else:
                        # Use date only if no timezone info
                        data['created'] = created.strftime('%Y-%m-%d')
                else:
                    data['created'] = created
            if self.paperless_correspondent:
                data['correspondent'] = self.paperless_correspondent
            if self.paperless_document_type:
                data['document_type'] = self.paperless_document_type
            if self.paperless_storage_path:
                data['storage_path'] = self.paperless_storage_path
            # Handle tags - paperless-ngx expects multiple 'tags' fields with the same name
            # requests library will send multiple form fields when a list is provided
            if self.paperless_tags:
                data['tags'] = self.paperless_tags
            
            # POST to paperless-ngx
            # requests will automatically handle lists in data dict by sending multiple form fields
            response = requests.post(url, headers=headers, files=files, data=data, timeout=30)
            
            if response.status_code == 200:
                task_uuid = response.json()
                logger.info(f"Successfully uploaded {filename} to paperless-ngx. Task UUID: {task_uuid}")
                return task_uuid
            else:
                logger.error(f"Failed to upload {filename} to paperless-ngx. Status: {response.status_code}, Response: {response.text}")
                return None
                
        except Exception as e:
            logger.error(f"Error uploading {filename} to paperless-ngx: {str(e)}")
            return None

