# Amazon Business Invoice Downloader

Downloads actual invoices from Amazon Business, remembers completed downloads in
SQLite, and can optionally upload PDFs to Paperless-ngx. It supports Amazon email
OTP challenges, scheduled runs, and email alerts when a run fails.

## What it does

- Downloads invoices without downloading order summaries (`Bestellübersicht`)
- Avoids duplicate downloads with a persistent SQLite database
- Checks only orders inside a configurable rolling lookback window
- Follows order-history pagination until the entire window is covered
- Retrieves fresh Amazon verification codes from a dedicated IMAP mailbox
- Verifies a known invoice PDF and SHA-256 hash on every run
- Sends an SMTP alert for login, OTP, extraction, download, hash, or upload errors
- Saves locally, uploads to Paperless-ngx, or does both

## Recommended setup: Docker Compose

Requirements: Docker with the Compose plugin and a dedicated email mailbox for
OTP messages. The included example uses STRATO's mail servers.

1. Create the configuration file:

   ```bash
   cp .env.example .env
   ```

2. Edit `.env`. At minimum, set:

   ```dotenv
   AMAZON_EMAIL=your-amazon-account@example.com
   AMAZON_PASSWORD=your-amazon-password

   MAIL_USERNAME=amazon@example.com
   MAIL_PASSWORD=your-mailbox-password
   MAIL_NOTIFICATION_TO=your-alert-address@example.com
   ```

3. Create the persistent directories. On Linux or a NAS, set `PUID` and
   `PGID` in `.env` to the user that owns these directories (`id -u` and
   `id -g` print the appropriate values):

   ```bash
   mkdir -p invoices data
   ```

4. Build and start:

   ```bash
   docker compose up -d --build
   docker compose logs -f
   ```

Compose reads `.env` by default. For testing another file without renaming it,
run `ENV_FILE=.env.testing docker compose up`.

Invoices are written to `./invoices` and the database to
`./data/invoices.db`. Both survive container replacement.

Useful commands:

```bash
docker compose logs -f                    # follow logs
docker compose run --rm -e SCHEDULE= amazon-invoice-downloader  # run once
docker compose down                       # stop scheduled operation
```

To run once, set `SCHEDULE=` in `.env`. For daily operation use
`SCHEDULE=24h`; other supported examples are `12h`, `1d`, and `7d`.

> `CLEAR_OTP_INBOX=true` permanently deletes every message in the configured
> IMAP folder before login. Only enable it for a dedicated mailbox.

## Configuration

The application accepts environment variables and equivalent command-line
options. A supplied CLI option overrides its environment variable.

### Core settings

| Environment variable | CLI option | Required / default |
|---|---|---|
| `AMAZON_EMAIL` | `--email` | Required |
| `AMAZON_PASSWORD` | `--password` | Required |
| `LOOKBACK_DAYS` | `--lookback-days` | `56` |
| `OUTPUT_FOLDER` | `--output-folder` | Required unless Paperless is configured |
| `DB_PATH` | `--db-path` | `invoices.db` |
| `LOG_LEVEL` | `--log-level` | `INFO` |
| `SCHEDULE` | `--schedule` | Empty: run once |
| `CLEAR_OTP_INBOX` | `--clear-otp-inbox` | `false` |
| `CHROME_HEADLESS` | — | `true` |
| `PUID` / `PGID` | — | `1000` (Docker Compose only) |

Set `CHROME_HEADLESS=false` only when you want to inspect a visible local
browser. Local and Docker runs are headless by default.

### Debugging authentication

Normal logs do not contain page HTML or full URLs. To investigate a failed
headless login, enable debug logging for one run:

```bash
python src/amazon_invoice_downloader.py --log-level DEBUG ... 2>&1 | tee log.txt
```

Or set `LOG_LEVEL=DEBUG` in `.env`. When Selenium times out, the log includes
the URL path without its query string, browser state, input metadata, and the
current HTML source. The configured Amazon password is redacted, but Amazon's
HTML can still contain email addresses, session identifiers, and security
tokens. Treat a debug log as sensitive and return to `INFO` afterwards.
Third-party protocol loggers remain at `WARNING` even in debug mode so raw
WebDriver responses and authentication cookies are never intentionally logged.

### OTP mailbox and alerts

The same mailbox credentials can be shared between IMAP and SMTP:

```dotenv
MAIL_USERNAME=amazon@example.com
MAIL_PASSWORD=your-mailbox-password

MAIL_IMAP_HOST=imap.strato.de
MAIL_IMAP_PORT=993
MAIL_IMAP_FOLDER=INBOX
AMAZON_OTP_TIMEOUT=120
AMAZON_OTP_POLL_INTERVAL=5

MAIL_SMTP_HOST=smtp.strato.de
MAIL_SMTP_PORT=465
MAIL_NOTIFICATION_TO=your-alert-address@example.com
```

You may instead provide separate `MAIL_IMAP_USERNAME`, `MAIL_IMAP_PASSWORD`,
`MAIL_SMTP_USERNAME`, and `MAIL_SMTP_PASSWORD` values. The notification sender
defaults to the SMTP username and can be overridden with
`MAIL_NOTIFICATION_FROM`.

Only fresh messages from Amazon's expected sender and sign-in subject are used
for OTP extraction. Normal logs do not contain passwords, OTPs, page HTML,
screenshots, or full authentication URLs.

### Paperless-ngx (optional)

```dotenv
PAPERLESS_URL=https://paperless.example.com
PAPERLESS_TOKEN=your-api-token
PAPERLESS_CORRESPONDENT=1
PAPERLESS_DOCUMENT_TYPE=2
PAPERLESS_TAGS=3,4,5
PAPERLESS_STORAGE_PATH=1
```

Only `PAPERLESS_URL` and `PAPERLESS_TOKEN` are required for upload. If
`OUTPUT_FOLDER` is also configured, every invoice must succeed both locally and
in Paperless before it is marked complete.

## Local Python setup

Requirements: Python 3.11+ and Chrome or Chromium.

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r src/requirements.txt
```

You can export environment variables or use CLI options:

```bash
python src/amazon_invoice_downloader.py \
  --email your-amazon-account@example.com \
  --password 'your-password' \
  --output-folder ./invoices \
  --db-path ./invoices.db
```

See all options with:

```bash
python src/amazon_invoice_downloader.py --help
```

Selenium manages the matching ChromeDriver automatically for local runs. The
Docker image includes Chromium and ChromeDriver.

## Rolling window and health check

`LOOKBACK_DAYS` controls the complete processing range. The downloader derives
the required calendar years, follows pagination, skips orders before the cutoff,
and performs no separate backfill. SQLite deduplication prevents repeated
downloads inside overlapping runs. Amazon document UUIDs can rotate, so invoice
completion is matched by order and invoice label/position rather than assuming
the download URL is permanent.

The health check is automatic and has no sentinel-specific settings. SQLite
stores an active sentinel's order ID, date, invoice position, label, last
observed URL, and SHA-256 hash. Every run rediscovers the order and extracts its
current invoice URL rather than assuming Amazon's document UUID is permanent.

Before the active sentinel leaves the lookback window, the downloader creates a
new candidate from a stable recent invoice. The candidate must pass verification
on a later run before it replaces the active sentinel. This overlap prevents a
broken extractor from silently creating its own successful baseline. On a new
installation, the first suitable run creates a candidate and a subsequent run
activates it. Hash baselines are never silently updated after creation.

A failed health check or another fatal processing error:

1. writes the error and traceback to the log;
2. sends one email when SMTP is configured; and
3. makes a one-time run exit non-zero.

If no active sentinel exists and no invoice can be extracted to establish one,
the run fails instead of silently operating without health coverage. A brand-new
account whose only invoices are too recent for a stable baseline logs a temporary
warning until one becomes eligible.

## Tests

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
python -m compileall -q src tests
```

The image publishing workflow runs both checks before building or pushing an
image.

## Build the image manually

```bash
docker build -t amazon-invoice-downloader:latest .
```

Docker automatically selects the host architecture. To build explicitly for a
NAS or another host, add `--platform linux/amd64` or `--platform linux/arm64`.

## Project layout

```text
src/amazon_invoice_downloader.py  Main workflow and configuration
src/browser.py                    Chrome login, OTP, and navigation
src/mail_client.py                IMAP OTP retrieval and SMTP alerts
src/invoice_extractor.py          Invoice-only link extraction
src/file_handler.py               PDF download and Paperless upload
src/database.py                   SQLite persistence and sentinel hashes
docker-compose.yml                Ready-to-run container service
.env.example                      Configuration template
```

## License

[GNU General Public License v3.0](LICENSE)
