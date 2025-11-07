# Amazon Business Invoice Downloader

Automated tool to download invoices from Amazon Business and optionally upload them to paperless-ngx.

## Features

- Automatically downloads invoices from Amazon Business account
- Tracks downloaded invoices in SQLite database to avoid duplicates
- Supports year-based filtering
- Handles year transitions automatically (checks both previous and current year during first 8 weeks)
- Optional integration with paperless-ngx for document management
- Can save invoices locally and/or upload to paperless-ngx
- Runs in headless mode (no GUI required)

## Requirements

- Python 3.11+
- Chrome/Chromium browser
- ChromeDriver (automatically managed by Selenium 4.x)

## Installation

### Local Installation

1. Clone this repository:
```bash
git clone <repository-url>
cd amz_business_invoice_dl
```

2. (Optional) Create a virtual environment:
```bash
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

3. Install dependencies:
```bash
pip install -r src/requirements.txt
```

4. Install Chrome/Chromium browser (if not already installed):
   - **macOS**: `brew install --cask google-chrome` or `brew install chromium`
   - **Linux**: `sudo apt-get install chromium-browser` or `sudo apt-get install chromium`
   - **Windows**: Download from [Google Chrome](https://www.google.com/chrome/)

5. Run the script:
```bash
python src/amazon_invoice_downloader.py \
  --email your.email@example.com \
  --password your_password \
  --output-folder ./invoices \
  --db-path ./invoices.db
```

**Note:** ChromeDriver is automatically managed by Selenium 4.x, so you don't need to install it separately.

## Usage

### Basic Usage

Download invoices to a local folder:
```bash
python src/amazon_invoice_downloader.py \
  --email your.email@example.com \
  --password your_password \
  --output-folder ./invoices \
  --db-path ./invoices.db
```

### With Paperless-ngx

Upload invoices directly to paperless-ngx:
```bash
python src/amazon_invoice_downloader.py \
  --email your.email@example.com \
  --password your_password \
  --db-path ./invoices.db \
  --paperless-url https://paperless.example.com \
  --paperless-token your_api_token \
  --paperless-correspondent 1 \
  --paperless-document-type 2 \
  --paperless-tags 3 4 5
```

### Combined (Local + Paperless-ngx)

Save locally AND upload to paperless-ngx:
```bash
python src/amazon_invoice_downloader.py \
  --email your.email@example.com \
  --password your_password \
  --output-folder ./invoices \
  --db-path ./invoices.db \
  --paperless-url https://paperless.example.com \
  --paperless-token your_api_token
```

### Command Line Arguments

- `--email`: Amazon account email (required)
- `--password`: Amazon account password (required)
- `--min-year`: Minimum year to download invoices from (e.g., 2020)
- `--output-folder`: Folder to save downloaded invoices (optional if using paperless-ngx)
- `--db-path`: Path to SQLite database file (default: invoices.db)
- `--paperless-url`: Paperless-ngx instance URL
- `--paperless-token`: Paperless-ngx API token
- `--paperless-correspondent`: Paperless-ngx correspondent ID
- `--paperless-document-type`: Paperless-ngx document type ID
- `--paperless-tags`: Paperless-ngx tag IDs (can specify multiple)
- `--paperless-storage-path`: Paperless-ngx storage path ID

**Note:** Either `--output-folder` or `--paperless-url` and `--paperless-token` must be specified.

## Docker Usage

### Using Pre-built Image from GitHub Container Registry

The Docker image is automatically built and pushed to GitHub Container Registry (ghcr.io) on every push to the main branch.

**Pull and run the image:**
```bash
docker pull ghcr.io/niclasku/amz_business_invoice_dl:latest
```

**With local storage:**
```bash
docker run --rm \
  -v $(pwd)/invoices:/app/invoices \
  -v $(pwd)/data:/app/data \
  ghcr.io/niclasku/amz_business_invoice_dl:latest \
  python amazon_invoice_downloader.py \
    --email YOUR_EMAIL \
    --password YOUR_PASSWORD \
    --output-folder /app/invoices \
    --db-path /app/data/invoices.db \
    --min-year 2025
```

**With paperless-ngx upload:**
```bash
docker run --rm \
  -v $(pwd)/data:/app/data \
  ghcr.io/niclasku/amz_business_invoice_dl:latest \
  python amazon_invoice_downloader.py \
    --email YOUR_EMAIL \
    --password YOUR_PASSWORD \
    --db-path /app/data/invoices.db \
    --paperless-url https://paperless.example.com \
    --paperless-token YOUR_API_TOKEN \
    --paperless-correspondent 1 \
    --paperless-document-type 2 \
    --paperless-tags 3 4 5
```

### Build the Image Locally

For your architecture (Docker will auto-detect):
```bash
docker build -t amazon-invoice-downloader:latest .
```

For specific architecture:
```bash
# For x86_64 (Synology NAS)
docker build --platform linux/amd64 -t amazon-invoice-downloader:latest .

# For ARM64 (M2 Mac)
docker build --platform linux/arm64 -t amazon-invoice-downloader:latest .
```

### Run with Docker (Local Build)

**With local storage:**
```bash
docker run --rm \
  -v $(pwd)/invoices:/app/invoices \
  -v $(pwd)/data:/app/data \
  amazon-invoice-downloader:latest \
  python amazon_invoice_downloader.py \
    --email YOUR_EMAIL \
    --password YOUR_PASSWORD \
    --output-folder /app/invoices \
    --db-path /app/data/invoices.db \
    --min-year 2025
```

**With paperless-ngx upload:**
```bash
docker run --rm \
  -v $(pwd)/data:/app/data \
  amazon-invoice-downloader:latest \
  python amazon_invoice_downloader.py \
    --email YOUR_EMAIL \
    --password YOUR_PASSWORD \
    --db-path /app/data/invoices.db \
    --paperless-url https://paperless.example.com \
    --paperless-token YOUR_API_TOKEN \
    --paperless-correspondent 1 \
    --paperless-document-type 2 \
    --paperless-tags 3 4 5
```

### Docker Compose

1. Create a `.env` file:
```bash
AMAZON_EMAIL=your.email@example.com
AMAZON_PASSWORD=your_password
PAPERLESS_URL=https://paperless.example.com
PAPERLESS_TOKEN=your_api_token
```

2. Update `docker-compose.yml` to uncomment and customize the `command` section.

3. Run:
```bash
docker-compose up
```

## Paperless-ngx Integration

### Getting Your API Token

1. Log in to your paperless-ngx instance
2. Go to **Settings** → **API Tokens**
3. Create a new API token
4. Copy the token for use in the script

### Finding IDs

You can find correspondent, document type, tag, and storage path IDs by:
- Using the paperless-ngx web interface and checking the URL when viewing an item
- Using the API: `GET /api/correspondents/`, `/api/document_types/`, `/api/tags/`, `/api/storage_paths/`
- Using curl: `curl -H "Authorization: Token YOUR_TOKEN" https://paperless.example.com/api/correspondents/`

## Project Structure

```
.
├── src/
│   ├── amazon_invoice_downloader.py  # Main orchestrator
│   ├── database.py                    # Database operations
│   ├── browser.py                     # WebDriver and navigation
│   ├── order_parser.py                # Order information extraction
│   ├── invoice_extractor.py           # Invoice link extraction
│   ├── file_handler.py                # File download/upload operations
│   └── requirements.txt               # Python dependencies
├── Dockerfile
├── docker-compose.yml
└── README.md
```

## How It Works

1. Logs into Amazon Business account
2. Navigates to order history (optionally filtered by year)
3. For each order:
   - Extracts order information (ID, date, price)
   - Finds and clicks "Rechnung" (invoice) link
   - Extracts invoice links from popover
   - Downloads only new invoices (tracks count per order)
   - Optionally uploads to paperless-ngx
   - Marks invoices as downloaded in database

## Year Transition Handling

During the first 8 weeks of a new year, the script automatically checks both the previous and current year to catch any late invoices. After 8 weeks, it only checks the current year.

## License

[Add your license here]

## Contributing

[Add contribution guidelines here]

