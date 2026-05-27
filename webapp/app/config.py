"""Webapp configuration.

M0: paths only. M1: adds pipeline invocation paths + env vars.
M3 (cloud): switch PIPELINE_ENV to "prod", load secrets from .env (decision #42).
"""
from __future__ import annotations
import os
from pathlib import Path

WEBAPP_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = WEBAPP_ROOT.parent

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
# M1: --env test (laptop), M3: --env prod (cloud).
PIPELINE_ENV = os.environ.get("WEBAPP_PIPELINE_ENV", "test")
PIPELINE_CHIP_LIST = os.environ.get(
    "WEBAPP_PIPELINE_CHIP_LIST",
    str(PROJECT_ROOT / "ref" / "Raw_chip_list_20260523_cleaned.xlsx"),
)
# Optional extra args passed to pipeline (for dev: limit sources, etc.)
# Default: limit to DIGIKEY for fast M1 smoke; remove for full sweep.
PIPELINE_API_ARGS = os.environ.get("WEBAPP_PIPELINE_API_ARGS", "--only DIGIKEY")
PIPELINE_SCRAPER_ARGS = os.environ.get(
    "WEBAPP_PIPELINE_SCRAPER_ARGS", "--sequential --only DIGIKEY"
)

# M1: bypass real pipeline and use canned data — useful for UI iteration.
FAKE_PIPELINE = os.environ.get("WEBAPP_FAKE_PIPELINE", "0") == "1"
M0_FAKE_PHASE_DURATION_SEC = 6  # used only in fake-pipeline mode

# Hardcoded user for M1 (M3 will read from session cookie).
DEV_OWNER_EMAIL = "demo@versuni.com"
