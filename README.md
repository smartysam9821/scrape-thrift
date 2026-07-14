# ThriftBooks Scraper

Searches ThriftBooks for ISBNs from text files under `isbn`.

Each raw value is split on `_`, then the numeric part is used as the ISBN. Results are written to:

- `results/thriftbooks_results.csv`
- `results/thriftbooks_results.jsonl`

The output columns match this MySQL table:

```sql
thriftbooks_inv (
  id,
  isbn,
  publisher,
  price,
  format,
  condition,
  stock_status,
  last_seen_timestamp
)
```

## Setup

The `.venv` and Playwright Chromium browser are already installed.

To reinstall later:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

## Run

Add `.txt` files inside `isbn`, then run:

```powershell
.\.venv\Scripts\python.exe thriftbooks_scraper.py
```

To also insert/update rows in MySQL:

```powershell
.\.venv\Scripts\python.exe thriftbooks_scraper.py --write-mysql
```

Default MySQL connection:

```text
host: 127.0.0.1
port: 3306
database: scrape_db
user: scrape_user
password: scrape_password
```

Useful options:

```powershell
.\.venv\Scripts\python.exe thriftbooks_scraper.py --limit 10
.\.venv\Scripts\python.exe thriftbooks_scraper.py --headed
.\.venv\Scripts\python.exe thriftbooks_scraper.py --concurrency 3 --min-delay-ms 400 --max-delay-ms 1200
.\.venv\Scripts\python.exe thriftbooks_scraper.py --no-block-assets
.\.venv\Scripts\python.exe thriftbooks_scraper.py --write-mysql --start-id 1000
```

## Speed and Rate

The scraper uses 3 concurrent browser pages by default and blocks images, media, and fonts. That is much faster than one page at a time while still keeping request volume modest.

Recommended production range:

```powershell
.\.venv\Scripts\python.exe thriftbooks_scraper.py --write-mysql --concurrency 3 --requests-per-minute 20 --min-delay-ms 1000 --max-delay-ms 3000
```

Avoid very high concurrency. It can overload the site, reduce data quality, and trigger anti-bot defenses.

Safety controls:

```powershell
.\.venv\Scripts\python.exe thriftbooks_scraper.py --write-mysql --requests-per-minute 15
.\.venv\Scripts\python.exe thriftbooks_scraper.py --write-mysql --stop-after-blocks 3
.\.venv\Scripts\python.exe thriftbooks_scraper.py --write-mysql --rescrape-hours 12
.\.venv\Scripts\python.exe thriftbooks_scraper.py --write-mysql --no-skip-recent
```

By default, MySQL runs skip ISBNs scraped in the last 12 hours. If an ISBN is older than 12 hours, it is scraped again and the existing ISBN row is updated. New ISBNs receive the next available id. The scraper retries once on block-like HTTP responses, backs off before retrying, and stops after 3 consecutive blocked responses.
