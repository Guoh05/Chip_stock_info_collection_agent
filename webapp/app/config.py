"""Webapp configuration.

Loads `.env` (decision #42) at module import time via python-dotenv.
"""
from __future__ import annotations
import os
from pathlib import Path

from dotenv import load_dotenv

WEBAPP_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = WEBAPP_ROOT.parent

# Load .env from project root (where the user-edited file lives on cloud:
# /home/admin/project/chip-project/.env). Silent if missing — defaults below
# still apply for local dev.
load_dotenv(PROJECT_ROOT / ".env", override=False)

TEMPLATES_DIR = WEBAPP_ROOT / "templates"
STATIC_DIR = WEBAPP_ROOT / "static"
RUNS_DIR = WEBAPP_ROOT / "runs"
LOGS_DIR = WEBAPP_ROOT / "logs"
TMP_DIR = WEBAPP_ROOT / "tmp"
DB_PATH = WEBAPP_ROOT / "webapp.db"

# Detect Python venv path cross-platform (decision #39 shared venv).
_WIN_PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
_NIX_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"
PIPELINE_PYTHON = str(_WIN_PYTHON if _WIN_PYTHON.exists() else _NIX_PYTHON)

# Pipeline invocation (decision #2 subprocess + defensive flag-passing).
PIPELINE_ENV = os.environ.get("WEBAPP_PIPELINE_ENV", "test")
PIPELINE_CHIP_LIST = os.environ.get(
    "WEBAPP_PIPELINE_CHIP_LIST",
    str(PROJECT_ROOT / "ref" / "Raw_chip_list_20260523_cleaned.xlsx"),
)
# Default: empty (all sources). Set "--only DIGIKEY" etc. for fast dev smoke.
PIPELINE_API_ARGS = os.environ.get("WEBAPP_PIPELINE_API_ARGS", "")
# 3-way parallel: cloud test confirmed 2 parallel chromium = +750 MB peak,
# avail still ~1 GB. 3 should land around +1.1 GB, leaving ~650 MB buffer on
# the 3.5 GB cloud VM. Going higher (4+) risks OOM under systemd MemoryMax=2G.
PIPELINE_SCRAPER_ARGS = os.environ.get(
    "WEBAPP_PIPELINE_SCRAPER_ARGS", "--max-parallel 3"
)

# Hardcoded user for local dev (when no session). Cloud uses Magic Link auth.
DEV_OWNER_EMAIL = os.environ.get("WEBAPP_DEV_OWNER_EMAIL", "demo@versuni.com")

# --- Auth (decision #10 Magic Link) ---
ALLOWLIST_EMAILS = {
    e.strip().lower()
    for e in os.environ.get("ALLOWLIST_EMAILS", "").split(",")
    if e.strip()
}
FASTAPI_SECRET_KEY = os.environ.get("FASTAPI_SECRET_KEY", "dev-insecure-key-change-me")
WEBAPP_BASE_URL = os.environ.get("WEBAPP_BASE_URL", "http://localhost:8000")
MAGIC_LINK_TTL_MINUTES = 15
SESSION_TTL_DAYS = 7

# Bypass auth when no allowlist is configured — keeps local dev frictionless.
# As soon as ALLOWLIST_EMAILS is set (e.g. on cloud), auth becomes mandatory.
AUTH_REQUIRED = len(ALLOWLIST_EMAILS) > 0

# --- SMTP (decision #12 Hotmail) ---
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp-mail.outlook.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_CONFIGURED = bool(SMTP_USER and SMTP_PASS)
