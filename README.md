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

Fresh server setup from GitHub:

```powershell
git clone https://github.com/smartysam9821/scrape-thrift.git
cd scrape-thrift
```

If the repo already exists on the server:

```powershell
cd scrape-thrift
git pull
```

Create Python virtual environment and install dependencies:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
copy config.example.json config.json
```

On Linux:

```bash
python3.12 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python -m playwright install chromium
cp config.example.json config.json
```

Edit `config.json` on each server before running jobs. This file stores local login, MySQL, and scraper defaults. It is ignored by git because it can contain passwords.

Runtime data that should stay local:

```text
config.json        local settings and credentials
ui_state.db        web console UI state, including job history
results/           scraper logs, CSV, and JSONL output
isbn/              input text files
```

## MySQL

Start MySQL 8.0 with Docker:

```powershell
docker run --name scrape-mysql `
  -e MYSQL_ROOT_PASSWORD=scrape_root_password `
  -e MYSQL_DATABASE=scrape_db `
  -e MYSQL_USER=scrape_user `
  -e MYSQL_PASSWORD=scrape_password `
  -p 3306:3306 `
  -d mysql:8.0
```

On Linux/macOS:

```bash
docker run --name scrape-mysql \
  -e MYSQL_ROOT_PASSWORD=scrape_root_password \
  -e MYSQL_DATABASE=scrape_db \
  -e MYSQL_USER=scrape_user \
  -e MYSQL_PASSWORD=scrape_password \
  -p 3306:3306 \
  -d mysql:8.0
```

If container already exists:

```powershell
docker start scrape-mysql
```

The scraper creates or updates the `thriftbooks_inv` table automatically.

## Run

Add `.txt` files inside `isbn`, then run:

```powershell
.\.venv\Scripts\python.exe thriftbooks_scraper.py
```

To also insert/update rows in MySQL:

```powershell
.\.venv\Scripts\python.exe thriftbooks_scraper.py --write-mysql
```

To force a remote MySQL server from the command line:

```powershell
.\.venv\Scripts\python.exe thriftbooks_scraper.py --write-mysql --mysql-host 192.168.1.254 --mysql-port 3306 --mysql-database scrape_db --mysql-user rakuten_user --mysql-password password123
```

Linux:

```bash
./.venv/bin/python thriftbooks_scraper.py --write-mysql
```

Default MySQL connection:

```text
host: 127.0.0.1
port: 3306
database: scrape_db
user: scrape_user
password: scrape_password
```

## Web Console

Start the FastAPI web console:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000
```

For access from another machine on the network:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app:app --host 0.0.0.0 --port 8000
```

Open:

```text
http://127.0.0.1:8000
```

Login:

```text
username: admin
password: scrape123
```

The frontend uses plain HTML, CSS, and JavaScript from `frontend/`. FastAPI serves the page and exposes small APIs for login, summary, progress, job status, and starting one scrape job.

The web console persists UI-related state in `ui_state.db`. Job history survives FastAPI restarts.

On Windows, start FastAPI from a normal PowerShell or service account session. If it is launched from a restricted/sandboxed session, Playwright can fail with `PermissionError: [WinError 5] Access is denied` when it tries to start its browser driver.

## Services

Service templates are in `deploy/`.

Linux systemd:

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin scrape
sudo mkdir -p /opt/scrape-thrift
sudo cp -R . /opt/scrape-thrift
sudo chown -R scrape:scrape /opt/scrape-thrift
sudo cp deploy/thriftbooks-console.service /etc/systemd/system/thriftbooks-console.service
sudo systemctl daemon-reload
sudo systemctl enable thriftbooks-console
sudo systemctl start thriftbooks-console
sudo systemctl status thriftbooks-console
```

Edit `deploy/thriftbooks-console.service` first if your project path, user, host, or port is different.

Windows service:

Install NSSM, then run PowerShell as Administrator:

```powershell
.\deploy\install-windows-service.ps1 -ProjectDir "C:\scrape-thrift" -Port 8000
```

If `nssm.exe` is not in `PATH`:

```powershell
.\deploy\install-windows-service.ps1 -ProjectDir "C:\scrape-thrift" -NssmPath "C:\tools\nssm\nssm.exe"
```

Logs:

```text
results/windows-service.out.log
results/windows-service.err.log
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
.\.venv\Scripts\python.exe thriftbooks_scraper.py --write-mysql --batch-size 25
```

By default, MySQL runs skip ISBNs scraped in the last 12 hours. If an ISBN is older than 12 hours, it is scraped again and the existing ISBN row is updated. New ISBNs receive the next available id. The scraper retries once on block-like HTTP responses, backs off before retrying, and stops after 3 consecutive blocked responses.

The scraper flushes every 25 ISBNs by default:

- inserts/updates MySQL
- rewrites `results/thriftbooks_results.csv`
- rewrites `results/thriftbooks_results.jsonl`
- appends a checkpoint line to `results/progress.log`

This makes the job resumable. If the process stops, run the same command again. Already scraped ISBNs from the last 12 hours are skipped.
