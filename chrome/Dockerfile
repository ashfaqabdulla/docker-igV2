FROM debian:bookworm-slim

# Google's apt repo + key, then Chrome itself.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates wget gnupg curl \
    && wget -q -O - https://dl.google.com/linux/linux_signing_key.pub \
       | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] \
       http://dl.google.com/linux/chrome/deb/ stable main" \
       > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update && apt-get install -y --no-install-recommends \
        google-chrome-stable \
        fonts-liberation fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -u 1000 -m chrome \
 && mkdir -p /home/chrome/profile \
 && chown -R chrome:chrome /home/chrome

USER chrome
WORKDIR /home/chrome
EXPOSE 9222

# --remote-debugging-address=0.0.0.0 is safe: the port is only reachable
# on the internal Docker network, never published to the host.
ENTRYPOINT ["google-chrome-stable", \
    "--remote-debugging-port=9222", \
    "--remote-debugging-address=0.0.0.0", \
    "--user-data-dir=/home/chrome/profile", \
    "--headless=new", \
    "--no-sandbox", \
    "--disable-gpu", \
    "--disable-dev-shm-usage", \
    "--no-first-run", \
    "--no-default-browser-check", \
    "--disable-background-networking", \
    "--disable-default-apps", \
    "--disable-sync"]
