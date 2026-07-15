from __future__ import annotations

import csv
import json
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


ROOT = Path(__file__).resolve().parent
FRONTEND_DIR = ROOT / "frontend"
RESULTS_DIR = ROOT / "results"
ISBN_DIR = ROOT / "isbn"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
CONFIG_PATH = ROOT / "config.json"
UI_DB_PATH = ROOT / "ui_state.db"

app = FastAPI(title="Scrape Console")
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

active_job: subprocess.Popen[str] | None = None
active_job_meta: dict[str, Any] = {}
app_config: dict[str, Any] = {
    "login": {"username": "admin", "password": "scrape123"},
    "mysql": {
        "host": "127.0.0.1",
        "port": 3306,
        "database": "scrape_db",
        "user": "scrape_user",
        "password": "scrape_password",
        "table": "thriftbooks_inv",
    },
    "scraper": {
        "batch_size": 25,
        "requests_per_minute": 20,
        "concurrency": 3,
        "rescrape_hours": 12,
        "input_dir": "isbn/",
        "output_dir": "results/",
    },
}

if CONFIG_PATH.exists():
    try:
        saved_config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        saved_config = {}
    for section, values in saved_config.items():
        if isinstance(values, dict) and section in app_config:
            app_config[section].update(values)



class LoginRequest(BaseModel):
    username: str
    password: str


class JobRequest(BaseModel):
    limit: int = Field(default=780, ge=1, le=100_000)
    requests_per_minute: int = Field(default=20, ge=1, le=60)
    concurrency: int = Field(default=3, ge=1, le=5)
    batch_size: int = Field(default=25, ge=1, le=500)
    rescrape_hours: int = Field(default=12, ge=0, le=720)


class ConfigRequest(BaseModel):
    login: dict[str, Any] | None = None
    mysql: dict[str, Any] | None = None
    scraper: dict[str, Any] | None = None


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.post("/api/login")
def login(payload: LoginRequest) -> dict[str, bool]:
    if payload.username == app_config["login"]["username"] and payload.password == app_config["login"]["password"]:
        return {"ok": True}
    raise HTTPException(status_code=401, detail="Invalid username or password")


@app.get("/api/summary")
def summary() -> dict[str, Any]:
    result_csv = RESULTS_DIR / "thriftbooks_results.csv"
    progress_log = RESULTS_DIR / "progress.log"
    isbn_count = count_input_values()
    result_count = count_csv_rows(result_csv)
    job = current_job_state()
    return {
        "isbn_count": isbn_count,
        "result_count": result_count,
        "progress_exists": progress_log.exists(),
        "job_running": job["running"],
        "job": job,
        "last_progress": read_tail(progress_log, 1)[0] if progress_log.exists() and read_tail(progress_log, 1) else "",
    }


@app.get("/api/progress")
def progress(limit: int = 20) -> dict[str, list[str]]:
    progress_log = RESULTS_DIR / "progress.log"
    if not progress_log.exists():
        return {"lines": []}
    return {"lines": read_tail(progress_log, limit)}


@app.get("/api/inventory")
def inventory(limit: int = 100) -> dict[str, Any]:
    result_csv = RESULTS_DIR / "thriftbooks_results.csv"
    rows: list[dict[str, str]] = []
    if result_csv.exists():
        with result_csv.open("r", encoding="utf-8", errors="ignore", newline="") as file:
            reader = csv.DictReader(file)
            for row in reader:
                rows.append(row)
                if len(rows) >= limit:
                    break
    return {"rows": rows, "count": len(rows)}


@app.get("/api/config")
def config() -> dict[str, Any]:
    return app_config


@app.post("/api/config")
def save_config(payload: ConfigRequest) -> dict[str, Any]:
    if payload.login:
        for key in ("username", "password"):
            if key in payload.login and payload.login[key]:
                app_config["login"][key] = payload.login[key]
    if payload.mysql:
        for key in ("host", "port", "database", "user", "password"):
            if key in payload.mysql and payload.mysql[key] not in (None, ""):
                app_config["mysql"][key] = payload.mysql[key]
    if payload.scraper:
        for key in ("batch_size", "requests_per_minute", "concurrency", "rescrape_hours"):
            if key in payload.scraper and payload.scraper[key] not in (None, ""):
                app_config["scraper"][key] = int(payload.scraper[key])
    save_config_file()
    return app_config


@app.post("/api/jobs/start")
def start_job(payload: JobRequest) -> dict[str, Any]:
    global active_job, active_job_meta
    if is_job_running():
        raise HTTPException(status_code=409, detail="A scrape job is already running")

    payload = JobRequest(
        limit=payload.limit,
        requests_per_minute=payload.requests_per_minute,
        concurrency=payload.concurrency,
        batch_size=payload.batch_size,
        rescrape_hours=payload.rescrape_hours,
    )

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_log = RESULTS_DIR / "frontend_job.out.log"
    err_log = RESULTS_DIR / "frontend_job.err.log"
    progress_log = RESULTS_DIR / "progress.log"
    progress_log.unlink(missing_ok=True)

    command = [
        str(PYTHON if PYTHON.exists() else Path(sys.executable)),
        "thriftbooks_scraper.py",
        "--limit",
        str(payload.limit),
        "--write-mysql",
        "--mysql-host",
        str(app_config["mysql"]["host"]),
        "--mysql-port",
        str(app_config["mysql"]["port"]),
        "--mysql-database",
        str(app_config["mysql"]["database"]),
        "--mysql-user",
        str(app_config["mysql"]["user"]),
        "--mysql-password",
        str(app_config["mysql"]["password"]),
        "--batch-size",
        str(payload.batch_size),
        "--requests-per-minute",
        str(payload.requests_per_minute),
        "--concurrency",
        str(payload.concurrency),
        "--min-delay-ms",
        "1000",
        "--max-delay-ms",
        "3000",
        "--rescrape-hours",
        str(payload.rescrape_hours),
    ]

    stdout = out_log.open("w", encoding="utf-8")
    stderr = err_log.open("w", encoding="utf-8")
    active_job = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=stdout,
        stderr=stderr,
        text=True,
    )
    job_id = insert_job_record(payload, active_job.pid)
    active_job_meta = {
        "id": job_id,
        "limit": payload.limit,
        "requests_per_minute": payload.requests_per_minute,
        "concurrency": payload.concurrency,
        "batch_size": payload.batch_size,
        "rescrape_hours": payload.rescrape_hours,
        "pid": active_job.pid,
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }
    return {"ok": True, "pid": active_job.pid, "command": " ".join(command)}


@app.get("/api/jobs/status")
def job_status() -> dict[str, Any]:
    return current_job_state()


@app.get("/api/jobs/history")
def jobs_history() -> dict[str, Any]:
    update_finished_job_history()
    return {"jobs": read_job_history()}


def is_job_running() -> bool:
    return active_job is not None and active_job.poll() is None


def current_job_state() -> dict[str, Any]:
    update_finished_job_history()
    running = is_job_running()
    progress = parse_progress_total(RESULTS_DIR / "progress.log") if running else 0
    limit = int(active_job_meta.get("limit") or 0) if running else 0
    return {
        "running": running,
        "pid": active_job.pid if active_job else None,
        "limit": limit,
        "processed": progress,
        "percent": round((progress / limit) * 100, 1) if limit else 0,
        "requests_per_minute": active_job_meta.get("requests_per_minute", app_config["scraper"]["requests_per_minute"]),
        "concurrency": active_job_meta.get("concurrency", app_config["scraper"]["concurrency"]),
        "started_at": active_job_meta.get("started_at"),
    }


def update_finished_job_history() -> None:
    global active_job, active_job_meta
    if active_job is None or active_job.poll() is None or active_job_meta.get("recorded"):
        return

    processed = parse_progress_total(RESULTS_DIR / "progress.log")
    started_at = active_job_meta.get("started_at")
    duration = ""
    if started_at:
        try:
            seconds = int((datetime.now() - datetime.fromisoformat(started_at)).total_seconds())
            duration = f"{max(seconds // 60, 0)}m {seconds % 60}s"
        except ValueError:
            duration = ""

    update_job_record(
        job_id=int(active_job_meta.get("id", 0)),
        status="complete" if active_job.returncode == 0 else "failed",
        processed=processed,
        limit_value=int(active_job_meta.get("limit", 0)),
        blocks=count_blocked_rows(RESULTS_DIR / "thriftbooks_results.csv"),
        duration=duration,
    )
    active_job_meta["recorded"] = True


def count_blocked_rows(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8", errors="ignore", newline="") as file:
        return sum(1 for row in csv.DictReader(file) if (row.get("stock_status") or "").startswith("BLOCKED"))


def db_connect() -> sqlite3.Connection:
    connection = sqlite3.connect(UI_DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_ui_db() -> None:
    with db_connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS job_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                duration TEXT,
                processed INTEGER NOT NULL DEFAULT 0,
                limit_value INTEGER NOT NULL DEFAULT 0,
                blocks INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                requests_per_minute INTEGER NOT NULL DEFAULT 0,
                concurrency INTEGER NOT NULL DEFAULT 0,
                batch_size INTEGER NOT NULL DEFAULT 0,
                rescrape_hours INTEGER NOT NULL DEFAULT 0,
                pid INTEGER
            )
            """
        )
        connection.execute(
            """
            UPDATE job_history
            SET status = 'stopped', finished_at = COALESCE(finished_at, ?)
            WHERE status = 'running'
            """,
            (datetime.now().isoformat(timespec="seconds"),),
        )


def insert_job_record(payload: JobRequest, pid: int) -> int:
    with db_connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO job_history (
                started_at, processed, limit_value, status, requests_per_minute,
                concurrency, batch_size, rescrape_hours, pid
            )
            VALUES (?, 0, ?, 'running', ?, ?, ?, ?, ?)
            """,
            (
                datetime.now().isoformat(timespec="seconds"),
                payload.limit,
                payload.requests_per_minute,
                payload.concurrency,
                payload.batch_size,
                payload.rescrape_hours,
                pid,
            ),
        )
        return int(cursor.lastrowid)


def update_job_record(
    job_id: int,
    status: str,
    processed: int,
    limit_value: int,
    blocks: int,
    duration: str,
) -> None:
    if not job_id:
        return
    with db_connect() as connection:
        connection.execute(
            """
            UPDATE job_history
            SET status = ?, processed = ?, limit_value = ?, blocks = ?,
                duration = ?, finished_at = ?
            WHERE id = ?
            """,
            (
                status,
                processed,
                limit_value,
                blocks,
                duration,
                datetime.now().isoformat(timespec="seconds"),
                job_id,
            ),
        )


def read_job_history(limit: int = 20) -> list[dict[str, Any]]:
    with db_connect() as connection:
        rows = connection.execute(
            """
            SELECT id, started_at, finished_at, duration, processed,
                   limit_value, blocks, status, requests_per_minute,
                   concurrency, batch_size, rescrape_hours, pid
            FROM job_history
            WHERE status <> 'running'
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [
        {
            "id": row["id"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "duration": row["duration"],
            "processed": row["processed"],
            "limit": row["limit_value"],
            "blocks": row["blocks"],
            "status": row["status"],
            "requests_per_minute": row["requests_per_minute"],
            "concurrency": row["concurrency"],
            "batch_size": row["batch_size"],
            "rescrape_hours": row["rescrape_hours"],
            "pid": row["pid"],
        }
        for row in rows
    ]


def parse_progress_total(path: Path) -> int:
    if not path.exists():
        return 0
    lines = read_tail(path, 1)
    if not lines:
        return 0
    for part in lines[0].split():
        if part.startswith("total="):
            try:
                return int(part.split("=", 1)[1])
            except ValueError:
                return 0
    return 0


def count_csv_rows(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8", errors="ignore", newline="") as file:
        return max(sum(1 for _ in csv.reader(file)) - 1, 0)


def count_input_values() -> int:
    total = 0
    for path in ISBN_DIR.rglob("*.txt"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        total += sum(1 for value in text.replace(",", " ").split() if value.strip())
    return total


def read_tail(path: Path, limit: int) -> list[str]:
    return path.read_text(encoding="utf-8", errors="ignore").splitlines()[-limit:]


def save_config_file() -> None:
    CONFIG_PATH.write_text(json.dumps(app_config, indent=2), encoding="utf-8")


init_ui_db()
