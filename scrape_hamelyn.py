from __future__ import annotations

import argparse
import asyncio
import csv
import json
import random
import re
import time
import sys

sys.stdout.reconfigure(encoding='utf-8')
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote_plus, urljoin

import pymysql
from playwright.async_api import BrowserContext, Page, Response, Route, TimeoutError as PlaywrightTimeoutError, async_playwright
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn


BASE_URL = "https://www.hamelyn.co.uk"
DEFAULT_URLS_FILE = Path("urls.txt")
DEFAULT_OUTPUT_DIR = Path("results")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0 Safari/537.36"
)

class TimestampConsole(Console):
    def print(self, *args, **kwargs):
        timestamp = datetime.now().strftime('%H:%M:%S')
        if args and isinstance(args[0], str):
            args = (f"[dim]{timestamp}[/dim] " + args[0],) + args[1:]
        super().print(*args, **kwargs)

console = TimestampConsole()


@dataclass
class BookDetails:
    """Data class for scraped book details"""
    isbn: str = ""
    price: str = ""
    format: str = ""
    condition: str = ""
    stock_status: str = "UNKNOWN"
    author: str = ""
    last_seen_timestamp: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


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


class BookScraper:
    def __init__(self, mysql_config: dict[str, Any], output_dir: Path, rate_limiter: RateLimiter) -> None:
        self.mysql_config = mysql_config
        self.output_dir = output_dir
        self.rate_limiter = rate_limiter
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.results: list[BookDetails] = []
        self.lock = asyncio.Lock()
        self.scraped_pages: set[str] = set()  # Track already-scraped page URLs
        self._load_scraped_pages()  # Load previously scraped pages from disk

    def _scraped_pages_file(self) -> Path:
        return self.output_dir / "hamelyn_scraped_pages.txt"

    def _load_scraped_pages(self) -> None:
        """Load previously scraped pages from disk to support resuming"""
        f = self._scraped_pages_file()
        if f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    self.scraped_pages.add(line)

    def _mark_page_scraped(self, url: str) -> None:
        """Persist a scraped page URL so future runs can skip it"""
        self.scraped_pages.add(url)
        with self._scraped_pages_file().open("a", encoding="utf-8") as f:
            f.write(url + "\n")

    async def scrape_page(self, page: Page, base_url: str) -> list[BookDetails]:
        """Scrape all paginated pages for a category URL using ?page=N"""
        books = []
        page_num = 1

        # Strip any existing ?page= param from base_url
        from urllib.parse import urlparse, urlencode, parse_qs, urlunparse
        parsed = urlparse(base_url)
        qs = parse_qs(parsed.query, keep_blank_values=True)
        qs.pop('page', None)
        clean_base = urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))

        consecutive_skips = 0
        while True:
            # Build page URL
            if page_num == 1:
                page_url = clean_base
            else:
                sep = '&' if '?' in clean_base else '?'
                page_url = f"{clean_base}{sep}page={page_num}"

            # Skip if already scraped in a previous run
            if page_url in self.scraped_pages:
                console.print(f"[dim]↷ Skipping already scraped: {page_url}[/dim]")
                page_num += 1
                consecutive_skips += 1
                if consecutive_skips > 500:
                    console.print(f"[yellow]⚠[/yellow] Too many consecutive skips — stopping")
                    break
                continue

            consecutive_skips = 0

            try:
                console.print(f"[blue]→[/blue] Loading page {page_num}: {page_url}")
                response = await page.goto(page_url, wait_until="domcontentloaded", timeout=30000)

                # Stop if page not found or redirected away from category
                if response and response.status >= 400:
                    console.print(f"[yellow]⚠[/yellow] HTTP {response.status} on {page_url} — stopping pagination")
                    break

                # Wait for product articles
                try:
                    await page.wait_for_selector('article[itemtype="https://schema.org/Product"]', timeout=8000)
                except Exception:
                    console.print(f"[yellow]⚠[/yellow] No articles found on page {page_num} — pagination complete")
                    break

                await asyncio.sleep(random.uniform(1, 2))

                books_on_page = await self._extract_books(page)
                if not books_on_page:
                    console.print(f"[yellow]⚠[/yellow] No books extracted on page {page_num} — stopping")
                    break

                # Add to results buffer
                async with self.lock:
                    self.results.extend(books_on_page)

                # Flush this page immediately to CSV + DB
                await self.flush_to_csv()
                await self.flush_to_database()
                async with self.lock:
                    self.results.clear()

                books.extend(books_on_page)
                self._mark_page_scraped(page_url)
                console.print(f"[green]✓[/green] Page {page_num}: scraped {len(books_on_page)} books (total so far: {len(books)})")
                page_num += 1

            except PlaywrightTimeoutError:
                console.print(f"[red]✗[/red] Timeout on page {page_num} — stopping pagination")
                break
            except Exception as e:
                console.print(f"[red]✗[/red] Error on page {page_num}: {e} — stopping")
                break

        return books

    async def _extract_books(self, page: Page) -> list[BookDetails]:
        """Extract book details from current page"""
        books = []
        
        # Hamelyn uses article elements with schema.org Product markup
        items = await page.query_selector_all('article[itemtype="https://schema.org/Product"]')
        
        if not items:
            console.print("[yellow]⚠[/yellow] Could not find book items on page")
            return books
        
        for item in items:
            book = await self._extract_book_details(item, page)
            if book.price or book.author:  # Only add if we got some valid details
                books.append(book)
        
        return books

    async def _extract_book_details(self, item: Any, page: Page) -> BookDetails:
        """Extract details from a single book element"""
        book = BookDetails()
        
        try:
            # Extract ISBN from the book URL — pattern: /slug-ISBNXXXXXXXX
            link = await item.query_selector('a[href]')
            if link:
                href = await link.get_attribute('href') or ""
                # ISBN is the numeric suffix after the last hyphen
                isbn_match = re.search(r'-(\d{10,13})(?:[/?#].*)?$', href)
                if isbn_match:
                    book.isbn = isbn_match.group(1)

            # Extract author
            author_elems = await item.query_selector_all('p, span')
            for elem in author_elems:
                text = (await elem.text_content()).strip()
                if 'autor' in text.lower():
                    book.author = text.replace('Autor:', '').strip()[:255]
                    break
            
            # Extract price
            price_elem = await item.query_selector('[class*="price"]')
            if price_elem:
                book.price = (await price_elem.text_content()).strip()
            else:
                price_elem = await item.query_selector('[class*="cost"], [class*="amount"]')
                if price_elem:
                    book.price = (await price_elem.text_content()).strip()
            
            # Availability -> stock_status
            stock_elem = await item.query_selector('[class*="stock"], [class*="available"]')
            if stock_elem:
                status_text = (await stock_elem.text_content()).strip()
                book.stock_status = "IN_STOCK" if "disponible" in status_text.lower() else "OUT_OF_STOCK"
            else:
                book.stock_status = "IN_STOCK"
        
        except Exception as e:
            console.print(f"[yellow]⚠[/yellow] Error extracting book details: {e}")
        
        return book

    async def add_result(self, book: BookDetails) -> None:
        """Add result to buffer"""
        async with self.lock:
            self.results.append(book)

    async def flush_to_database(self) -> None:
        """Flush results to MySQL database"""
        if not self.results or not self.mysql_config:
            return
        
        try:
            conn = pymysql.connect(**self.mysql_config)
            cursor = conn.cursor()

            # Migrate existing table to new schema:
            # Drop old columns that no longer exist and add new ones.
            migrations = [
                "ALTER TABLE hamelyn_books DROP COLUMN IF EXISTS title",
                "ALTER TABLE hamelyn_books DROP COLUMN IF EXISTS publisher",
                "ALTER TABLE hamelyn_books DROP COLUMN IF EXISTS availability",
                "ALTER TABLE hamelyn_books DROP COLUMN IF EXISTS scraped_at",
                "ALTER TABLE hamelyn_books DROP COLUMN IF EXISTS description",
                "ALTER TABLE hamelyn_books DROP COLUMN IF EXISTS publish_year",
                "ALTER TABLE hamelyn_books DROP COLUMN IF EXISTS pages",
                "ALTER TABLE hamelyn_books DROP COLUMN IF EXISTS language",
                "ALTER TABLE hamelyn_books DROP COLUMN IF EXISTS url",
                # Add new columns if missing
                "ALTER TABLE hamelyn_books ADD COLUMN IF NOT EXISTS condition VARCHAR(255)",
                "ALTER TABLE hamelyn_books ADD COLUMN IF NOT EXISTS stock_status VARCHAR(50)",
                "ALTER TABLE hamelyn_books ADD COLUMN IF NOT EXISTS last_seen_timestamp DATETIME",
                # Add indexes if missing
                "ALTER TABLE hamelyn_books ADD INDEX IF NOT EXISTS idx_last_seen (last_seen_timestamp)",
            ]
            for sql in migrations:
                try:
                    cursor.execute(sql)
                except Exception:
                    pass  # Column may already be removed/added
            conn.commit()

            # Ensure table exists with correct schema (fresh installs)
            create_table_sql = """
            CREATE TABLE IF NOT EXISTS hamelyn_books (
                id INT AUTO_INCREMENT PRIMARY KEY,
                isbn VARCHAR(50),
                price VARCHAR(50),
                format VARCHAR(100),
                `condition` VARCHAR(255),
                stock_status VARCHAR(50),
                author VARCHAR(255),
                last_seen_timestamp DATETIME,
                INDEX idx_isbn (isbn),
                INDEX idx_last_seen (last_seen_timestamp)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
            cursor.execute(create_table_sql)
            conn.commit()
            
            # Insert results
            insert_sql = """
            INSERT INTO hamelyn_books 
            (isbn, price, format, `condition`, stock_status, author, last_seen_timestamp)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """
            
            rows_inserted = 0
            for book in self.results:
                try:
                    cursor.execute(insert_sql, (
                        book.isbn,
                        book.price,
                        book.format,
                        book.condition,
                        book.stock_status,
                        book.author,
                        book.last_seen_timestamp
                    ))
                    rows_inserted += 1
                except Exception as e:
                    console.print(f"[yellow]⚠[/yellow] Error inserting book: {e}")
            
            conn.commit()
            cursor.close()
            conn.close()
            
            console.print(f"[green]✓[/green] Inserted {rows_inserted} books into MySQL")
            
        except Exception as e:
            console.print(f"[red]✗[/red] Database error: {e}")

    async def write_progress(self, processed_count: int) -> None:
        """Write progress for UI parsing"""
        progress_log = self.output_dir / "progress.log"
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} total={processed_count}"
        with progress_log.open("a", encoding="utf-8") as file:
            file.write(line + "\n")

    async def flush_to_csv(self) -> None:
        """Flush results to CSV"""
        if not self.results:
            return
        
        csv_path = self.output_dir / "hamelyn_results.csv"
        try:
            write_header = not csv_path.exists() or csv_path.stat().st_size == 0
            with open(csv_path, 'a', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=asdict(self.results[0]).keys())
                if write_header:
                    writer.writeheader()
                for book in self.results:
                    writer.writerow(asdict(book))
            console.print(f"[green]✓[/green] Saved {len(self.results)} books to {csv_path}")
        except Exception as e:
            console.print(f"[red]✗[/red] CSV error: {e}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape Hamelyn books from URLs")
    parser.add_argument("--urls-file", type=Path, default=DEFAULT_URLS_FILE, help="File with URLs to scrape")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Output directory")
    parser.add_argument("--rpm", type=int, default=10, help="Requests per minute (rate limiting)")
    parser.add_argument("--mysql-host", default="localhost", help="MySQL host")
    parser.add_argument("--mysql-port", type=int, default=3306, help="MySQL port")
    parser.add_argument("--mysql-user", default="scrape_user", help="MySQL user")
    parser.add_argument("--mysql-password", default="", help="MySQL password")
    parser.add_argument("--mysql-db", default="scrape_db", help="MySQL database")
    
    args = parser.parse_args()
    
    # Check if urls.txt exists
    if not args.urls_file.exists():
        console.print(f"[red]✗[/red] URLs file not found: {args.urls_file}")
        console.print(f"[yellow]ℹ[/yellow] Create {args.urls_file} with one URL per line")
        return
    
    # Read URLs
    urls = []
    try:
        for line in args.urls_file.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line.startswith('http://') or line.startswith('https://'):
                urls.append(line)
    except Exception as e:
        console.print(f"[red]✗[/red] Error reading URLs file: {e}")
        return
        
    if not urls:
        console.print(f"[red]✗[/red] No valid HTTP URLs found in {args.urls_file}")
        return
    
    console.print(f"[blue]ℹ[/blue] Found {len(urls)} URLs to scrape")
    
    # MySQL config
    mysql_config = {
        "host": args.mysql_host,
        "port": args.mysql_port,
        "user": args.mysql_user,
        "password": args.mysql_password,
        "database": args.mysql_db,
    }
    
    # Test MySQL connection
    try:
        conn = pymysql.connect(**mysql_config)
        conn.close()
        console.print("[green]✓[/green] MySQL connection successful")
    except Exception as e:
        console.print(f"[red]✗[/red] MySQL connection failed: {e}")
        console.print("[yellow]⚠[/yellow] Continuing without MySQL (CSV only)")
        mysql_config = None
    
    rate_limiter = RateLimiter(args.rpm)
    scraper = BookScraper(mysql_config, args.output_dir, rate_limiter)
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TimeElapsedColumn(),
        ) as progress:
            task = progress.add_task("[cyan]Scraping Hamelyn books...", total=len(urls))
            
            for i, url in enumerate(urls):
                try:
                    context = await browser.new_context(user_agent=USER_AGENT)
                    page = await context.new_page()
                    
                    await rate_limiter.wait()
                    
                    console.print(f"[blue]→[/blue] Scraping: {url}")
                    books = await scraper.scrape_page(page, url)
                    # Results already flushed per-page inside scrape_page
                    console.print(f"[green]✓[/green] Finished URL: {len(books)} books total")
                    await scraper.write_progress(i + 1)
                    await context.close()
                
                except Exception as e:
                    console.print(f"[red]✗[/red] Error processing URL: {e}")
                
                finally:
                    progress.update(task, advance=1)
        
        await browser.close()
    
    # Final flush for any remaining results (e.g. if error happened mid-URL)
    if scraper.results:
        await scraper.flush_to_csv()
        await scraper.flush_to_database()
    
    console.print(f"[green]✓[/green] Scraping complete!")


if __name__ == "__main__":
    asyncio.run(main())
