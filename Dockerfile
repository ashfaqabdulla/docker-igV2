FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Both the CLI and the API are in the same image.
COPY ig_scrap.py api.py health_check.py ./

RUN useradd -u 1000 -m scraper \
 && mkdir -p /app/out \
 && chown -R scraper:scraper /app
USER scraper

ENTRYPOINT ["python", "ig_scrap.py"]
CMD ["--help"]
