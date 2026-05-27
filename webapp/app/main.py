"""FastAPI entry — M1.

Run from project root:
    .venv/Scripts/python.exe -m uvicorn webapp.app.main:app --reload --port 8000
"""
from __future__ import annotations
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import storage
from .config import LOGS_DIR, STATIC_DIR
from .routers import history, query, runs
from .services import pipeline_runner

# Basic logging (M3 will rotate to webapp/logs/app.log)
LOGS_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    storage.init_db()
    pipeline_runner.start_worker()
    yield


app = FastAPI(title="Chip Stock Webapp", version="M2", lifespan=lifespan)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(query.router)
app.include_router(runs.router)
app.include_router(history.router)


@app.get("/")
async def root():
    return RedirectResponse(url="/query")


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "phase": "M2"}
