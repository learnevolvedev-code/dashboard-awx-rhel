"""
AWX Analytics Portal – FastAPI entrypoint
Lifespan: initialises DB pool on startup, closes on shutdown.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

import yaml
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import database
from .routers import summary, orgs, jobs, rbac

CONFIG_PATH = os.environ.get("AWX_PORTAL_CONFIG", "/opt/awx-portal/config/config.yaml")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("awx_api")

def load_cfg() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)

@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_cfg()
    app.state.cfg = cfg
    database.init_pool(cfg)
    log.info("AWX Portal API started")
    yield
    database.close_pool()
    log.info("AWX Portal API stopped")

app = FastAPI(
    title="AWX Analytics Portal API",
    version="1.0.0",
    description="Backend API for AWX Analytics Portal",
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

# ── CORS ──────────────────────────────────────────────────
def get_cors_origins() -> list[str]:
    try:
        cfg = load_cfg()
        return cfg.get("api", {}).get("cors_origins", ["*"])
    except Exception:
        return ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_cors_origins(),
    allow_credentials=True,
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────
app.include_router(summary.router, prefix="/api")
app.include_router(orgs.router,    prefix="/api")
app.include_router(jobs.router,    prefix="/api")
app.include_router(rbac.router,    prefix="/api")

@app.get("/api/health", tags=["health"])
def health():
    return {"status": "ok"}
