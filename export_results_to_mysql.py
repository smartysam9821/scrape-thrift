from __future__ import annotations

import argparse
import csv
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pymysql


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.json"
DEFAULT_CSV = ROOT / "results" / "thriftbooks_results.csv"


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"Missing config file: {path}")
    config = json.loads(path.read_text(encoding="utf-8"))
    mysql = config.get("mysql") or {}
    required = ["host", "port", "database", "user", "password"]
    missing = [key for key in required if mysql.get(key) in (None, "")]
    if missing:
        raise SystemExit(f"Missing mysql config values: {', '.join(missing)}")
    return mysql


def ensure_table(connection: pymysql.connections.Connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
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
        )
    connection.commit()


def clean_price(value: str) -> Decimal:
    try:
        return Decimal(str(value or "0").strip() or "0").quantize(Decimal("0.01"))
    except InvalidOperation:
        return Decimal("0.00")


def clean_row(row: dict[str, str]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "isbn": (row.get("isbn") or "").strip()[:20],
        "publisher": ((row.get("publisher") or "").strip() or "Unknown")[:255],
        "price": clean_price(row.get("price") or "0"),
        "format": ((row.get("format") or "").strip() or None),
        "condition": ((row.get("condition") or "").strip() or None),
        "stock_status": ((row.get("stock_status") or "").strip() or None),
        "last_seen_timestamp": ((row.get("last_seen_timestamp") or "").strip() or None),
    }


def read_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise SystemExit(f"Missing CSV file: {path}")
    with path.open("r", encoding="utf-8", errors="ignore", newline="") as file:
        return [clean_row(row) for row in csv.DictReader(file) if row.get("id") and row.get("isbn")]


def export_rows(mysql: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    connection = pymysql.connect(
        host=mysql["host"],
        port=int(mysql["port"]),
        user=mysql["user"],
        password=mysql["password"],
        database=mysql["database"],
        charset="utf8mb4",
        autocommit=False,
    )
    try:
        ensure_table(connection)
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
        with connection.cursor() as cursor:
            cursor.executemany(sql, rows)
        connection.commit()
    finally:
        connection.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export scraper CSV results into configured MySQL database.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    mysql = load_config(args.config)
    rows = read_rows(args.csv)
    if not rows:
        print("No rows found to export.")
        return 0
    export_rows(mysql, rows)
    print(f"Exported {len(rows)} rows to {mysql['host']}:{mysql['port']}/{mysql['database']}.thriftbooks_inv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
