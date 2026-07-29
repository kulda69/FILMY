"""Aplikacni vstup pro FastAPI server FILMY."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from filmy.app_shared import background_supervisor
from filmy.db import ASSETS_DIR, PEOPLE_ASSETS_DIR, ensure_database
from filmy.routers.api import router as api_router
from filmy.routers.ui import router as ui_router
from filmy.routers.web import router as web_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Own startup/shutdown of local DB schema checks and supervised background jobs."""
    background_supervisor.cleanup_orphan_processes()
    ensure_database()
    background_supervisor.start()
    try:
        yield
    finally:
        background_supervisor.stop()


app = FastAPI(lifespan=lifespan)
app.mount("/assets/tmdb", StaticFiles(directory=ASSETS_DIR.as_posix()), name="tmdb_assets")
app.mount("/assets/people", StaticFiles(directory=PEOPLE_ASSETS_DIR.as_posix()), name="people_assets")

app.include_router(web_router)
app.include_router(ui_router)
app.include_router(api_router)
