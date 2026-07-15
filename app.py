from __future__ import annotations

import csv
import subprocess
import sys
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

LOGIN_USERNAME = "admin"
LOGIN_PASSWORD = "scrape123"

app = FastAPI(title="Scrape Console")
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

active_job: subprocess.Popen[str] | None = None


class LoginRequest(BaseModel):
    username: str
    password: str


class JobRequest(BaseModel):
    limit: int = Field(default=780, ge=1, le=100_000)
    requests_per_minute: int = Field(default=20, ge=1, le=60)
    concurrency: int = Field(default=3, ge=1, le=5)
    batch_size: int = Field(default=25, ge=1, le=500)
    rescrape_hours: int = Field(default=12, ge=0, le=720)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.post("/api/login")
def login(payload: LoginRequest) -> dict[str, bool]:
    if payload.username == LOGIN_USERNAME and payload.password == LOGIN_PASSWORD:
        return {"ok": True}
    raise HTTPException(status_code=401, detail="Invalid username or password")


@app.get("/api/summary")
def summary() -> dict[str, Any]:
    result_csv = RESULTS_DIR / "thriftbooks_results.csv"
    progress_log = RESULTS_DIR / "progress.log"
    isbn_count = count_input_values()
    result_count = count_csv_rows(result_csv)
    return {
        "isbn_count": isbn_count,
        "result_count": result_count,
        "progress_exists": progress_log.exists(),
        "job_running": is_job_running(),
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
    return {
        "login": {"username": LOGIN_USERNAME, "password": LOGIN_PASSWORD},
        "mysql": {
            "host": "127.0.0.1",
            "port": 3306,
            "database": "scrape_db",
            "user": "scrape_user",
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


@app.post("/api/jobs/start")
def start_job(payload: JobRequest) -> dict[str, Any]:
    global active_job
    if is_job_running():
        raise HTTPException(status_code=409, detail="A scrape job is already running")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_log = RESULTS_DIR / "frontend_job.out.log"
    err_log = RESULTS_DIR / "frontend_job.err.log"

    command = [
        str(PYTHON if PYTHON.exists() else Path(sys.executable)),
        "thriftbooks_scraper.py",
        "--limit",
        str(payload.limit),
        "--write-mysql",
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
    return {"ok": True, "pid": active_job.pid, "command": " ".join(command)}


@app.get("/api/jobs/status")
def job_status() -> dict[str, Any]:
    return {"running": is_job_running(), "pid": active_job.pid if active_job else None}


def is_job_running() -> bool:
    return active_job is not None and active_job.poll() is None


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
