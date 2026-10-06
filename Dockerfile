FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl socat \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ig_scrap.py api.py health_check.py ./
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

RUN useradd -u 1000 -m scraper \
 && mkdir -p /app/out \
 && chown -R scraper:scraper /app
USER scraper

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "ig_scrap.py", "--help"]
