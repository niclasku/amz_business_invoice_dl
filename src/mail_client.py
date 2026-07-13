"""Retrieve Amazon OTP messages and send downloader failure alerts."""
import email
import imaplib
import logging
import os
import re
import smtplib
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from html import unescape
from typing import Optional


logger = logging.getLogger(__name__)


class AmazonOtpMailbox:
    """Poll an IMAP mailbox for a newly delivered Amazon OTP message."""

    SUBJECTS = {
        "amazon.de: anmeldeversuch",
        "amazon.de: sign-in attempt",
    }
    CODE_PATTERNS = (
        re.compile(r"Verifizierungscode\s*:\s*(\d{6})", re.IGNORECASE),
        re.compile(r"verification code\s*(?:is|:)\s*(\d{6})", re.IGNORECASE),
    )

    def __init__(self, host: str, username: str, password: str,
                 port: int = 993, folder: str = "INBOX",
                 timeout: int = 120, poll_interval: int = 5):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.folder = folder
        self.timeout = timeout
        self.poll_interval = poll_interval

    @classmethod
    def from_environment(cls) -> Optional["AmazonOtpMailbox"]:
        """Create a mailbox client when the required environment is configured."""
        host = os.environ.get("MAIL_IMAP_HOST")
        username = os.environ.get("MAIL_IMAP_USERNAME") or os.environ.get("MAIL_USERNAME")
        password = os.environ.get("MAIL_IMAP_PASSWORD") or os.environ.get("MAIL_PASSWORD")
        if not (host and username and password):
            return None
        return cls(
            host=host,
            port=int(os.environ.get("MAIL_IMAP_PORT", "993")),
            username=username,
            password=password,
            folder=os.environ.get("MAIL_IMAP_FOLDER", "INBOX"),
            timeout=int(os.environ.get("AMAZON_OTP_TIMEOUT", "120")),
            poll_interval=int(os.environ.get("AMAZON_OTP_POLL_INTERVAL", "5")),
        )

    @staticmethod
    def _decoded_header(message, name: str) -> str:
        value = message.get(name, "")
        return str(make_header(decode_header(value))) if value else ""

    @staticmethod
    def _message_text(message) -> str:
        """Return decoded plain text, falling back to stripped HTML."""
        plain_parts = []
        html_parts = []
        parts = message.walk() if message.is_multipart() else (message,)
        for part in parts:
            if part.get_content_disposition() == "attachment":
                continue
            content_type = part.get_content_type()
            if content_type not in ("text/plain", "text/html"):
                continue
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if content_type == "text/plain":
                plain_parts.append(text)
            else:
                html_parts.append(text)
        if plain_parts:
            return "\n".join(plain_parts)
        html = "\n".join(html_parts)
        return unescape(re.sub(r"<[^>]+>", " ", html))

    @classmethod
    def extract_code(cls, raw_message: bytes, not_before: datetime) -> Optional[str]:
        """Validate an Amazon message and extract its contextual six-digit OTP."""
        message = email.message_from_bytes(raw_message)
        subject = cls._decoded_header(message, "Subject").strip().casefold()
        if subject not in cls.SUBJECTS:
            return None

        from_address = parseaddr(cls._decoded_header(message, "From"))[1].casefold()
        return_path = parseaddr(cls._decoded_header(message, "Return-Path"))[1].casefold()
        if from_address != "account-update@amazon.de":
            return None
        if not return_path.endswith("@bounces.amazon.de"):
            return None

        try:
            message_date = parsedate_to_datetime(message.get("Date"))
            if message_date.tzinfo is None:
                message_date = message_date.replace(tzinfo=timezone.utc)
            if message_date < not_before:
                return None
        except (TypeError, ValueError, OverflowError):
            return None

        body = cls._message_text(message)
        codes = []
        for pattern in cls.CODE_PATTERNS:
            codes.extend(pattern.findall(body))
        unique_codes = set(codes)
        return unique_codes.pop() if len(unique_codes) == 1 else None

    def wait_for_code(self, not_before: datetime) -> str:
        """Poll until a matching fresh OTP arrives or the timeout expires."""
        deadline = time.monotonic() + self.timeout
        # Email Date headers only have second precision and mail systems can have
        # small clock differences. Search newest-first and allow a narrow skew.
        effective_not_before = not_before - timedelta(seconds=30)
        since_date = effective_not_before.astimezone(timezone.utc).strftime("%d-%b-%Y")
        logger.info("Waiting for Amazon OTP email (timeout: %s seconds)...", self.timeout)

        mailbox = imaplib.IMAP4_SSL(self.host, self.port)
        try:
            mailbox.login(self.username, self.password)
            status, _ = mailbox.select(self.folder)
            if status != "OK":
                raise RuntimeError(f"Could not select IMAP folder {self.folder!r}")

            while time.monotonic() < deadline:
                status, data = mailbox.search(None, "SINCE", since_date)
                if status != "OK":
                    raise RuntimeError("IMAP search failed")

                message_ids = data[0].split() if data and data[0] else []
                for message_id in reversed(message_ids[-50:]):
                    status, fetched = mailbox.fetch(message_id, "(BODY.PEEK[])")
                    if status != "OK" or not fetched:
                        continue
                    raw_message = next(
                        (item[1] for item in fetched
                         if isinstance(item, tuple) and isinstance(item[1], bytes)),
                        None,
                    )
                    if not raw_message:
                        continue
                    code = self.extract_code(raw_message, effective_not_before)
                    if code:
                        mailbox.store(message_id, "+FLAGS", "\\Seen")
                        logger.info("Retrieved a fresh Amazon OTP email")
                        return code

                time.sleep(self.poll_interval)
                try:
                    mailbox.noop()
                except imaplib.IMAP4.abort:
                    raise RuntimeError("IMAP connection was closed while waiting for OTP")
        finally:
            try:
                mailbox.logout()
            except Exception:
                pass

        raise TimeoutError(
            f"No matching Amazon OTP email arrived within {self.timeout} seconds"
        )

    def clear_folder(self) -> int:
        """Permanently delete every message in the configured IMAP folder."""
        mailbox = imaplib.IMAP4_SSL(self.host, self.port)
        try:
            mailbox.login(self.username, self.password)
            status, _ = mailbox.select(self.folder)
            if status != "OK":
                raise RuntimeError(f"Could not select IMAP folder {self.folder!r}")
            status, data = mailbox.search(None, "ALL")
            if status != "OK":
                raise RuntimeError("IMAP search failed while clearing folder")
            message_ids = data[0].split() if data and data[0] else []
            if message_ids:
                mailbox.store(b",".join(message_ids), "+FLAGS", "\\Deleted")
                mailbox.expunge()
            return len(message_ids)
        finally:
            try:
                mailbox.logout()
            except Exception:
                pass


class FailureNotifier:
    """Send downloader failure notifications through an SMTP SSL server."""

    def __init__(self, host: str, port: int, username: str, password: str,
                 sender: str, recipient: str):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.sender = sender
        self.recipient = recipient

    @classmethod
    def from_environment(cls) -> Optional["FailureNotifier"]:
        host = os.environ.get("MAIL_SMTP_HOST")
        username = os.environ.get("MAIL_SMTP_USERNAME") or os.environ.get("MAIL_USERNAME")
        password = os.environ.get("MAIL_SMTP_PASSWORD") or os.environ.get("MAIL_PASSWORD")
        recipient = os.environ.get("MAIL_NOTIFICATION_TO")
        sender = os.environ.get("MAIL_NOTIFICATION_FROM") or username
        if not (host and username and password and sender and recipient):
            return None
        return cls(
            host=host,
            port=int(os.environ.get("MAIL_SMTP_PORT", "465")),
            username=username,
            password=password,
            sender=sender,
            recipient=recipient,
        )

    def send_failure(self, error: Exception) -> None:
        message = EmailMessage()
        message["Subject"] = "Amazon invoice downloader failed"
        message["From"] = self.sender
        message["To"] = self.recipient
        message.set_content(
            "The Amazon invoice downloader failed.\n\n"
            f"Time (UTC): {datetime.now(timezone.utc).isoformat()}\n"
            f"Error type: {type(error).__name__}\n"
            f"Error: {error}\n\n"
            "Check the downloader logs for details.\n"
            "No password, OTP, cookies, or page HTML is included in this message."
        )
        with smtplib.SMTP_SSL(self.host, self.port, timeout=30) as smtp:
            smtp.login(self.username, self.password)
            smtp.send_message(message)
