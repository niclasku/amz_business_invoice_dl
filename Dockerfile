# Multi-architecture Dockerfile for Amazon Invoice Downloader
# Supports both x86_64 (Synology NAS) and ARM64 (M2 Mac)

FROM python:3.11-slim-bookworm@sha256:f5cf0344c9886ff24d34797578d5d7dd6e8911ae0fe5962bb55d0f89603ec361

# Install Chrome/Chromium and dependencies for headless operation
# This works for both x86_64 and ARM64 architectures
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    chromium-driver \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libx11-6 \
    libx11-xcb1 \
    libxcb1 \
    libxcomposite1 \
    libxcursor1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxi6 \
    libxrandr2 \
    libxrender1 \
    libxss1 \
    libxtst6 \
    xdg-utils \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies globally (works for both root and non-root users)
COPY src/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create the default runtime user. Compose can override this with PUID/PGID so
# bind mounts remain writable on Linux and NAS hosts.
RUN useradd -m -u 1000 appuser

# Set working directory
WORKDIR /app

# Copy application files
COPY src/ .

# Create directories for invoices and database
RUN mkdir -p /app/invoices /app/data && \
    chown -R appuser:appuser /app

# Set environment variables for Chrome
ENV CHROME_BIN=/usr/bin/chromium
ENV CHROMEDRIVER_PATH=/usr/bin/chromedriver
ENV CHROMIUM_FLAGS="--no-sandbox --disable-dev-shm-usage --disable-gpu"

# Switch to non-root user
USER appuser

# Configuration is supplied through environment variables or optional CLI flags.
ENTRYPOINT ["python", "amazon_invoice_downloader.py"]
