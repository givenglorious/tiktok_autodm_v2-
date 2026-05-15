FROM python:3.11-slim

# Install dependencies + Google Chrome + ChromeDriver
RUN apt-get update && apt-get install -y \
    wget gnupg curl unzip ca-certificates \
    fonts-liberation libappindicator3-1 libasound2 \
    libatk-bridge2.0-0 libatk1.0-0 libcups2 libdbus-1-3 \
    libgdk-pixbuf2.0-0 libnspr4 libnss3 libx11-xcb1 \
    libxcomposite1 libxdamage1 libxrandr2 xdg-utils \
    --no-install-recommends \
    && wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get install -y ./google-chrome-stable_current_amd64.deb \
    && rm google-chrome-stable_current_amd64.deb \
    && rm -rf /var/lib/apt/lists/*

# Install ChromeDriver yang versinya matching dengan Chrome
RUN CHROME_VER=$(google-chrome --version | grep -oP '\d+\.\d+\.\d+\.\d+') \
    && CHROME_MAJOR=$(echo $CHROME_VER | cut -d. -f1) \
    && echo "Chrome version: $CHROME_VER (major: $CHROME_MAJOR)" \
    && DRIVER_URL=$(curl -s "https://googlechromelabs.github.io/chrome-for-testing/known-good-versions-with-downloads.json" \
        | python3 -c " \
import sys, json; \
data = json.load(sys.stdin); \
major = '$CHROME_MAJOR'; \
versions = [v for v in data['versions'] if v['version'].startswith(major + '.')]; \
last = versions[-1] if versions else None; \
dl = last and next((d['url'] for d in last['downloads'].get('chromedriver', []) if d['platform'] == 'linux64'), None); \
print(dl or '') \
    ") \
    && echo "ChromeDriver URL: $DRIVER_URL" \
    && wget -q "$DRIVER_URL" -O /tmp/chromedriver.zip \
    && unzip /tmp/chromedriver.zip -d /tmp/chromedriver_dir \
    && find /tmp/chromedriver_dir -name "chromedriver" -exec mv {} /usr/local/bin/chromedriver \; \
    && chmod +x /usr/local/bin/chromedriver \
    && rm -rf /tmp/chromedriver.zip /tmp/chromedriver_dir

# Verifikasi
RUN google-chrome --version && chromedriver --version

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD uvicorn tiktok_dm:app --host 0.0.0.0 --port ${PORT:-8000}
