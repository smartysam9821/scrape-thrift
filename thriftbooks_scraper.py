from __future__ import annotations

import argparse
import asyncio
import csv
import json
import random
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable
from urllib.parse import quote_plus, urljoin

import pymysql
from playwright.async_api import BrowserContext, Page, Response, Route, TimeoutError as PlaywrightTimeoutError, async_playwright
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn


BASE_URL = "https://www.thriftbooks.com"
SEARCH_URL = BASE_URL + "/browse/?b.search={isbn}"
DEFAULT_INPUT_DIR = Path("isbn")
DEFAULT_OUTPUT_DIR = Path("results")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0 Safari/537.36"
)

console = Console()


class RateLimiter:
    def __init__(self, requests_per_minute: int) -> None:
        self.min_interval = 60.0 / requests_per_minute if requests_per_minute > 0 else 0.0
        self.lock = asyncio.Lock()
        self.next_at = 0.0

    async def wait(self) -> None:
        if self.min_interval <= 0:
            return
        async with self.lock:
            now = time.monotonic()
            if now < self.next_at:
                await asyncio.sleep(self.next_at - now)
            self.next_at = time.monotonic() + self.min_interval


class BlockMonitor:
    def __init__(self, threshold: int) -> None:
        self.threshold = threshold
        self.count = 0
        self.lock = asyncio.Lock()
        self.stop_event = asyncio.Event()

    async def record(self, blocked: bool) -> None:
        async with self.lock:
            self.count = self.count + 1 if blocked else 0
            if self.threshold > 0 and self.count >= self.threshold:
                self.stop_event.set()


@dataclass
class IsbnRecord:
    isbn: str
    raw_value: str
    source_file: str


@dataclass
class ScrapeResult:
    id: int
    isbn: str
    publisher: str = ""
    price: Decimal = Decimal("0.00")
    format: str = ""
    condition: str = ""
    stock_status: str = "UNKNOWN"
    last_seen_timestamp: str = ""


def iter_input_values(input_dir: Path) -> Iterable[tuple[str, Path]]:
    for path in sorted(input_dir.rglob("*.txt")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for raw in re.split(r"[\s,;|]+", text):
            raw = raw.strip()
            if raw:
                yield raw, path


def extract_isbn(raw_value: str) -> str | None:
    parts = [part.strip() for part in raw_value.split("_") if part.strip()]
    if not parts:
        return None

    numeric_parts = []
    for part in parts:
        digits = re.sub(r"\D", "", part)
        if digits and digits == part:
            numeric_parts.append(digits)

    if not numeric_parts:
        numeric_parts = [digits for part in parts if (digits := re.sub(r"\D", "", part))]

    preferred = [part for part in numeric_parts if len(part) in (10, 13)]
    candidates = preferred or numeric_parts
    return max(candidates, key=len) if candidates else None


def load_isbns(input_dir: Path) -> list[IsbnRecord]:
    seen: set[str] = set()
    records: list[IsbnRecord] = []
    for raw_value, source_path in iter_input_values(input_dir):
        isbn = extract_isbn(raw_value)
        if not isbn or isbn in seen:
            continue
        seen.add(isbn)
        records.append(IsbnRecord(isbn=isbn, raw_value=raw_value, source_file=str(source_path)))
    return records


async def accept_cookie_banner(page: Page) -> None:
    labels = ["Accept All", "Accept Cookies", "I Accept", "Got it", "OK"]
    for label in labels:
        try:
            button = page.get_by_role("button", name=re.compile(label, re.I)).first
            if await button.count():
                await button.click(timeout=1_500)
                return
        except Exception:
            continue


async def block_heavy_assets(route: Route) -> None:
    if route.request.resource_type in {"image", "media", "font"}:
        await route.abort()
    else:
        await route.continue_()


async def text_or_empty(page: Page, selector: str) -> str:
    try:
        value = await page.locator(selector).first.inner_text(timeout=1_500)
        return clean_text(value)
    except Exception:
        return ""


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def first_price(text: str) -> str:
    match = re.search(r"\$\s?\d+(?:\.\d{2})?", text)
    return clean_text(match.group(0)) if match else ""


def parse_price(value: str) -> Decimal:
    cleaned = re.sub(r"[^\d.]", "", value or "")
    if not cleaned:
        return Decimal("0.00")
    try:
        return Decimal(cleaned).quantize(Decimal("0.01"))
    except InvalidOperation:
        return Decimal("0.00")


async def json_ld_products(page: Page) -> list[dict]:
    return await page.evaluate(
        """
        () => Array.from(document.querySelectorAll('script[type="application/ld+json"]'))
          .flatMap((node) => {
            try {
              const parsed = JSON.parse(node.textContent || 'null');
              return Array.isArray(parsed) ? parsed : [parsed];
            } catch {
              return [];
            }
          })
          .flatMap((entry) => {
            if (!entry) return [];
            if (entry['@graph']) return entry['@graph'];
            if (entry.itemListElement) return entry.itemListElement.map((item) => item.item || item);
            return [entry];
          })
          .filter((entry) => entry && /Product|Book/i.test(String(entry['@type'] || '')));
        """
    )


async def parse_first_result(page: Page) -> dict[str, str]:
    sidebar = await parse_work_selector_sidebar(page)
    if sidebar["price"] or sidebar["format"] or sidebar["condition"]:
        return sidebar

    product_data = await json_ld_products(page)
    for product in product_data:
        name = clean_text(product.get("name"))
        if not name:
            continue
        offers = product.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        author = product.get("author") or ""
        if isinstance(author, dict):
            author = author.get("name", "")
        image = product.get("image") or ""
        if isinstance(image, list):
            image = image[0] if image else ""
        return {
            "title": name,
            "publisher": await parse_publisher(page) or clean_text(author),
            "price": clean_text(str(offers.get("price", ""))) if isinstance(offers, dict) else "",
            "format": clean_text(product.get("bookFormat") or product.get("format") or ""),
            "condition": "",
            "stock_status": "IN_STOCK",
            "product_url": urljoin(BASE_URL, clean_text(product.get("url") or "")),
            "image_url": urljoin(BASE_URL, clean_text(image)),
        }

    product_link = page.locator("a[href*='/w/'], a[href*='/p/']").first
    product_url = ""
    title = ""
    try:
        if await product_link.count():
            href = await product_link.get_attribute("href", timeout=1_500)
            product_url = urljoin(BASE_URL, href or "")
            title = clean_text(await product_link.inner_text(timeout=1_500))
    except Exception:
        pass

    body_text = clean_text(await page.locator("body").inner_text(timeout=3_000))
    if not title:
        title = await text_or_empty(page, "h1, h2, h3")

    return {
        "title": title,
        "publisher": await parse_publisher(page),
        "price": first_price(body_text),
        "format": await text_or_empty(page, "[class*='format' i]"),
        "condition": await text_or_empty(page, "[class*='condition' i]"),
        "stock_status": stock_status_from_text(body_text),
        "product_url": product_url,
        "image_url": await first_image_url(page),
    }


async def parse_work_selector_sidebar(page: Page) -> dict[str, str]:
    data = await page.evaluate(
        """
        () => {
          const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
          const root = document.querySelector('.WorkSelector-sidebar, .WorkPriceSidebar');
          if (!root) return null;

          const text = clean(root.innerText || root.textContent || '');
          const labelValue = (label) => {
            const paragraphs = Array.from(root.querySelectorAll('p'));
            const paragraph = paragraphs.find((node) => clean(node.textContent).toLowerCase().startsWith(label.toLowerCase() + ':'));
            if (!paragraph) return '';
            const bold = paragraph.querySelector('.WorkSelector-bold');
            if (bold) return clean(bold.textContent);
            return clean(paragraph.textContent)
              .replace(new RegExp('^' + label + ':\\\\s*', 'i'), '')
              .split(/Temporarily Unavailable|Currently Unavailable|Add to Cart|Add to Wish List|Save |List Price|\\$/i)[0]
              .trim();
          };

          const priceNode = root.querySelector('.WorkSelector-price .price, .WorkSelector-price');
          let price = clean(priceNode ? priceNode.textContent : '');
          if (price && !price.includes('$')) price = '$' + price;

          return {
            title: '',
            publisher: '',
            price,
            format: labelValue('Format'),
            condition: labelValue('Condition'),
            stock_status: /add to cart/i.test(text)
              ? 'IN_STOCK'
              : (/out of stock|unavailable|sold out/i.test(text) ? 'OUT_OF_STOCK' : 'UNKNOWN'),
            product_url: window.location.href,
            image_url: ''
          };
        }
        """
    )
    if not data:
        return empty_parsed_result()
    data["publisher"] = await parse_publisher(page)
    return {key: clean_text(value) for key, value in data.items()}


async def parse_publisher(page: Page) -> str:
    try:
        publisher = await page.evaluate(
            """
            () => {
              const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
              const rows = Array.from(document.querySelectorAll('.WorkMeta-detailsRow'));
              const row = rows.find((node) => {
                const label = clean(node.querySelector('.WorkMeta-detailTitle')?.textContent);
                return /^Publisher:?$/i.test(label);
              });
              return clean(row?.querySelector('.WorkMeta-detailValue')?.textContent || '');
            }
            """
        )
        if publisher:
            return clean_text(publisher)
    except Exception:
        pass

    try:
        body_text = await page.locator("body").inner_text(timeout=2_000)
        match = re.search(r"(?:^|\n)\s*Publisher:\s*([^\n]+)", body_text, re.I)
        if match:
            return clean_text(match.group(1))
    except Exception:
        pass
    return ""


def empty_parsed_result() -> dict[str, str]:
    return {
        "title": "",
        "publisher": "",
        "price": "",
        "format": "",
        "condition": "",
        "stock_status": "UNKNOWN",
        "product_url": "",
        "image_url": "",
    }


def stock_status_from_text(value: str) -> str:
    if re.search(r"out of stock|currently unavailable|sold out", value, re.I):
        return "OUT_OF_STOCK"
    if re.search(r"add to cart|in stock|available", value, re.I):
        return "IN_STOCK"
    return "UNKNOWN"


async def first_image_url(page: Page) -> str:
    try:
        src = await page.locator("img[src*='images-na.ssl-images-amazon'], img[src*='thriftbooks']").first.get_attribute(
            "src",
            timeout=1_500,
        )
        return urljoin(BASE_URL, src or "")
    except Exception:
        return ""


async def wait_for_useful_content(page: Page) -> None:
    try:
        await page.wait_for_selector(
            ".WorkSelector-sidebar, .WorkPriceSidebar, .WorkMeta-detailsRow, a[href*='/w/'], body",
            timeout=6_000,
        )
    except PlaywrightTimeoutError:
        pass


def is_block_response(response: Response | None) -> bool:
    return response is not None and response.status in {403, 407, 408, 409, 425, 429, 500, 502, 503, 504}


async def page_looks_blocked(page: Page) -> bool:
    try:
        body_text = clean_text(await page.locator("body").inner_text(timeout=2_000))
    except Exception:
        return False
    return bool(re.search(r"captcha|access denied|blocked|too many requests|verify you are human", body_text, re.I))


async def scrape_one(
    page: Page,
    row_id: int,
    record: IsbnRecord,
    args: argparse.Namespace,
    limiter: RateLimiter,
) -> ScrapeResult:
    search_url = SEARCH_URL.format(isbn=quote_plus(record.isbn))
    result = ScrapeResult(
        id=row_id,
        isbn=record.isbn,
        last_seen_timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )

    try:
        response: Response | None = None
        for attempt in range(args.retries + 1):
            await limiter.wait()
            response = await page.goto(search_url, wait_until="domcontentloaded", timeout=args.timeout_ms)
            if not is_block_response(response):
                break
            if attempt < args.retries:
                await page.wait_for_timeout(args.backoff_ms * (attempt + 1))

        if is_block_response(response):
            result.stock_status = f"BLOCKED_HTTP_{response.status if response else 'UNKNOWN'}"
            return result

        await accept_cookie_banner(page)
        await wait_for_useful_content(page)

        if await page_looks_blocked(page):
            result.stock_status = "BLOCKED_PAGE"
            return result

        page_text = clean_text(await page.locator("body").inner_text(timeout=5_000))
        if re.search(r"no results|did not match|couldn't find|0 results", page_text, re.I):
            result.stock_status = "NOT_FOUND"
            return result

        parsed = await parse_first_result(page)
        result.publisher = parsed["publisher"]
        result.price = parse_price(parsed["price"])
        result.format = parsed["format"]
        result.condition = parsed["condition"]
        result.stock_status = parsed["stock_status"] if parsed["title"] or parsed["product_url"] else "UNKNOWN"
        return result
    except Exception as exc:
        result.stock_status = f"ERROR: {type(exc).__name__}"[:50]
        return result


async def scrape_worker(
    worker_id: int,
    context: BrowserContext,
    queue: asyncio.Queue[tuple[int, IsbnRecord]],
    results_by_index: dict[int, ScrapeResult],
    progress: Progress,
    task_id: int,
    args: argparse.Namespace,
    limiter: RateLimiter,
    block_monitor: BlockMonitor,
) -> None:
    page = await context.new_page()
    try:
        while True:
            if block_monitor.stop_event.is_set():
                return
            try:
                index, record = queue.get_nowait()
            except asyncio.QueueEmpty:
                return

            progress.update(task_id, description=f"Worker {worker_id}: {record.isbn}")
            row_id = args.start_id + index
            result = await scrape_one(page, row_id, record, args, limiter)
            results_by_index[index] = result
            await block_monitor.record(result.stock_status.startswith("BLOCKED"))
            progress.advance(task_id)
            queue.task_done()

            if args.max_delay_ms > 0:
                delay_ms = random.randint(args.min_delay_ms, args.max_delay_ms)
                await page.wait_for_timeout(delay_ms)
    finally:
        await page.close()


def write_outputs(results: list[ScrapeResult], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "thriftbooks_results.csv"
    jsonl_path = output_dir / "thriftbooks_results.jsonl"

    fields = list(asdict(results[0]).keys()) if results else list(ScrapeResult(0, "").__dict__.keys())
    with csv_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fields)
        writer.writeheader()
        for result in results:
            writer.writerow(serialize_result(result))

    with jsonl_path.open("w", encoding="utf-8") as jsonl_file:
        for result in results:
            jsonl_file.write(json.dumps(serialize_result(result), ensure_ascii=False) + "\n")

    return csv_path, jsonl_path


def serialize_result(result: ScrapeResult) -> dict[str, object]:
    row = asdict(result)
    row["price"] = f"{result.price:.2f}"
    row["publisher"] = truncate(row["publisher"], 255) or "Unknown"
    row["isbn"] = truncate(row["isbn"], 20)
    row["format"] = truncate(row["format"], 50) or None
    row["condition"] = truncate(row["condition"], 50) or None
    row["stock_status"] = truncate(row["stock_status"], 50) or None
    return row


def truncate(value: object, max_length: int) -> str:
    return str(value or "").strip()[:max_length]


def ensure_table(connection: pymysql.connections.Connection) -> None:
    ddl = """
    CREATE TABLE IF NOT EXISTS thriftbooks_inv (
      id int NOT NULL,
      isbn varchar(20) NOT NULL,
      publisher varchar(255) NOT NULL,
      price decimal(10,2) NOT NULL,
      format varchar(50) DEFAULT NULL,
      `condition` varchar(50) DEFAULT NULL,
      stock_status varchar(50) DEFAULT NULL,
      last_seen_timestamp datetime DEFAULT NULL,
      PRIMARY KEY (id),
      KEY ISBN (isbn)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
    """
    with connection.cursor() as cursor:
        cursor.execute(ddl)
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = 'thriftbooks_inv'
              AND column_name = 'author'
            """
        )
        has_author = cursor.fetchone()[0] > 0
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = 'thriftbooks_inv'
              AND column_name = 'publisher'
            """
        )
        has_publisher = cursor.fetchone()[0] > 0
        if has_author and not has_publisher:
            cursor.execute("ALTER TABLE thriftbooks_inv CHANGE author publisher varchar(255) NOT NULL")
    connection.commit()


def write_mysql(args: argparse.Namespace, results: list[ScrapeResult]) -> None:
    connection = pymysql.connect(
        host=args.mysql_host,
        port=args.mysql_port,
        user=args.mysql_user,
        password=args.mysql_password,
        database=args.mysql_database,
        charset="utf8mb4",
        autocommit=False,
    )
    try:
        ensure_table(connection)
        assign_mysql_ids(connection, results)
        sql = """
        INSERT INTO thriftbooks_inv
          (id, isbn, publisher, price, format, `condition`, stock_status, last_seen_timestamp)
        VALUES
          (%(id)s, %(isbn)s, %(publisher)s, %(price)s, %(format)s, %(condition)s, %(stock_status)s, %(last_seen_timestamp)s)
        ON DUPLICATE KEY UPDATE
          isbn = VALUES(isbn),
          publisher = VALUES(publisher),
          price = VALUES(price),
          format = VALUES(format),
          `condition` = VALUES(`condition`),
          stock_status = VALUES(stock_status),
          last_seen_timestamp = VALUES(last_seen_timestamp)
        """
        rows = [serialize_result(result) for result in results]
        with connection.cursor() as cursor:
            cursor.executemany(sql, rows)
        connection.commit()
    finally:
        connection.close()


def assign_mysql_ids(connection: pymysql.connections.Connection, results: list[ScrapeResult]) -> None:
    if not results:
        return

    isbns = [result.isbn for result in results]
    placeholders = ", ".join(["%s"] * len(isbns))
    existing_ids: dict[str, int] = {}

    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT isbn, MIN(id) AS id
            FROM thriftbooks_inv
            WHERE isbn IN ({placeholders})
            GROUP BY isbn
            """,
            isbns,
        )
        existing_ids = {isbn: row_id for isbn, row_id in cursor.fetchall()}

        cursor.execute("SELECT COALESCE(MAX(id), 0) FROM thriftbooks_inv")
        next_id = max(args_start_id_floor(results), cursor.fetchone()[0] + 1)

    for result in results:
        if result.isbn in existing_ids:
            result.id = existing_ids[result.isbn]
        else:
            result.id = next_id
            next_id += 1


def args_start_id_floor(results: list[ScrapeResult]) -> int:
    return min((result.id for result in results), default=1)


def load_recent_isbns(args: argparse.Namespace) -> set[str]:
    if args.rescrape_hours <= 0:
        return set()

    connection = pymysql.connect(
        host=args.mysql_host,
        port=args.mysql_port,
        user=args.mysql_user,
        password=args.mysql_password,
        database=args.mysql_database,
        charset="utf8mb4",
        autocommit=True,
    )
    try:
        ensure_table(connection)
        sql = """
        SELECT isbn
        FROM thriftbooks_inv
        WHERE last_seen_timestamp >= NOW() - INTERVAL %s HOUR
        """
        with connection.cursor() as cursor:
            cursor.execute(sql, (args.rescrape_hours,))
            return {row[0] for row in cursor.fetchall()}
    finally:
        connection.close()


async def run(args: argparse.Namespace) -> int:
    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)

    if not input_dir.exists():
        console.print(f"[red]Input folder not found:[/red] {input_dir}")
        return 1

    records = load_isbns(input_dir)
    if args.write_mysql and args.skip_recent:
        recent_isbns = load_recent_isbns(args)
        if recent_isbns:
            before = len(records)
            records = [record for record in records if record.isbn not in recent_isbns]
            console.print(f"Skipped [bold]{before - len(records)}[/bold] ISBNs scraped in the last {args.rescrape_hours} hours")

    if args.limit:
        records = records[: args.limit]

    if not records:
        console.print(f"[yellow]No ISBNs found in {input_dir}. Add .txt files and run again.[/yellow]")
        return 0

    if args.max_delay_ms < args.min_delay_ms:
        args.max_delay_ms = args.min_delay_ms
    if args.concurrency < 1:
        args.concurrency = 1

    console.print(f"Loaded [bold]{len(records)}[/bold] unique ISBNs from {input_dir}")
    results: list[ScrapeResult] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not args.headed)
        context = await browser.new_context(user_agent=USER_AGENT, viewport={"width": 1365, "height": 900})
        if args.block_assets:
            await context.route("**/*", block_heavy_assets)
        limiter = RateLimiter(args.requests_per_minute)
        block_monitor = BlockMonitor(args.stop_after_blocks)

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Searching ThriftBooks", total=len(records))
            queue: asyncio.Queue[tuple[int, IsbnRecord]] = asyncio.Queue()
            for index, record in enumerate(records):
                queue.put_nowait((index, record))

            results_by_index: dict[int, ScrapeResult] = {}
            worker_count = min(args.concurrency, len(records))
            await asyncio.gather(
                *[
                    scrape_worker(worker_id, context, queue, results_by_index, progress, task, args, limiter, block_monitor)
                    for worker_id in range(1, worker_count + 1)
                ]
            )
            results = [results_by_index[index] for index in sorted(results_by_index)]

        await context.close()
        await browser.close()

    if args.write_mysql:
        write_mysql(args, results)
        console.print(f"MySQL: inserted/updated {len(results)} rows in thriftbooks_inv")

    csv_path, jsonl_path = write_outputs(results, output_dir)

    found_count = sum(1 for result in results if result.stock_status == "IN_STOCK")
    blocked_count = sum(1 for result in results if result.stock_status.startswith("BLOCKED"))
    console.print(f"[green]Done.[/green] Found {found_count}/{len(results)} in-stock products.")
    if blocked_count:
        console.print(f"[red]Blocked responses:[/red] {blocked_count}. Reduce rate or pause before continuing.")
    console.print(f"CSV: {csv_path}")
    console.print(f"JSONL: {jsonl_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scrape ThriftBooks search results from ISBN text files.")
    parser.add_argument("--input-dir", default=str(DEFAULT_INPUT_DIR), help="Folder containing .txt ISBN/ASIN files.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Folder for CSV and JSONL output.")
    parser.add_argument("--limit", type=int, default=0, help="Optional max number of ISBNs to process.")
    parser.add_argument("--delay-ms", type=int, default=0, help="Deprecated. Use --min-delay-ms and --max-delay-ms.")
    parser.add_argument("--min-delay-ms", type=int, default=400, help="Minimum per-worker delay between searches.")
    parser.add_argument("--max-delay-ms", type=int, default=1_200, help="Maximum per-worker delay between searches.")
    parser.add_argument("--concurrency", type=int, default=3, help="Number of browser pages to scrape with.")
    parser.add_argument("--block-assets", action=argparse.BooleanOptionalAction, default=True, help="Block images, media, and fonts.")
    parser.add_argument("--requests-per-minute", type=int, default=20, help="Global request rate across all workers. Use 0 to disable.")
    parser.add_argument("--retries", type=int, default=1, help="Retries for transient/block-like HTTP responses.")
    parser.add_argument("--backoff-ms", type=int, default=15_000, help="Base retry backoff for transient/block-like HTTP responses.")
    parser.add_argument("--stop-after-blocks", type=int, default=3, help="Stop workers after this many consecutive blocked responses. Use 0 to disable.")
    parser.add_argument("--skip-recent", action=argparse.BooleanOptionalAction, default=True, help="Skip ISBNs already scraped recently when --write-mysql is used.")
    parser.add_argument("--rescrape-hours", type=int, default=12, help="Recent scrape window used by --skip-recent.")
    parser.add_argument("--timeout-ms", type=int, default=30_000, help="Page load timeout.")
    parser.add_argument("--headed", action="store_true", help="Show the browser window.")
    parser.add_argument("--start-id", type=int, default=1, help="First id value for thriftbooks_inv rows.")
    parser.add_argument("--write-mysql", action="store_true", help="Insert/update results into MySQL.")
    parser.add_argument("--mysql-host", default="127.0.0.1")
    parser.add_argument("--mysql-port", type=int, default=3306)
    parser.add_argument("--mysql-database", default="scrape_db")
    parser.add_argument("--mysql-user", default="scrape_user")
    parser.add_argument("--mysql-password", default="scrape_password")
    return parser


def main() -> int:
    return asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
