"""
AWX Analytics Portal – FastAPI entrypoint v2
Adds: OAuth2 auth router, ROI metrics router.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

import yaml
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import database
from .routers import summary, orgs, jobs, rbac, roi, export
from . import auth as auth_module

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
    auth_enabled = cfg.get("auth", {}).get("enabled", False)
    log.info("AWX Portal API started (auth=%s)", "enabled" if auth_enabled else "disabled")
    yield
    database.close_pool()
    log.info("AWX Portal API stopped")

app = FastAPI(
    title="AWX Analytics Portal API",
    version="2.0.0",
    description="Backend API for AWX Analytics Portal — with OAuth2 auth and ROI metrics",
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
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────
app.include_router(auth_module.router, prefix="/api")   # /api/auth/*
app.include_router(summary.router,     prefix="/api")
app.include_router(orgs.router,        prefix="/api")
app.include_router(jobs.router,        prefix="/api")
app.include_router(rbac.router,        prefix="/api")
app.include_router(roi.router,         prefix="/api")   # /api/roi/*
app.include_router(export.router,     prefix="/api")   # /api/export/*

@app.get("/api/health", tags=["health"])
def health():
    return {"status": "ok", "version": "2.0.0"}
