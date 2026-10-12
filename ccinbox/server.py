"""FastAPI: ``GET /api/items``, ``POST /api/items/{id}`` and the static PWA under ``web/``."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ccinbox import config, db


class ActionBody(BaseModel):
    action: str
    until: str | None = None


class BulkBody(BaseModel):
    action: str
    ids: list[int]
    until: str | None = None


def _authorized(request: Request) -> bool:
    token = config.TOKEN
    if not token:
        return True
    supplied = request.headers.get("x-token") or request.query_params.get("token") or ""
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        supplied = supplied or auth[7:].strip()
    return supplied == token


class SweepRunner:
    """Runs one ``ccinbox sweep`` at a time as a child process and remembers the last result."""

    def __init__(self, spawn=None):
        self._spawn = spawn or self._default_spawn
        self._lock = threading.RLock()  # start() reads state() under the same lock
        self.proc = None
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.result: dict | None = None
        self.error: str | None = None

    @staticmethod
    def _default_spawn():
        cmd = [sys.executable, "-m", "ccinbox.cli", "sweep", "--idle-min", "0", "--debounce", str(config.REFRESH_DEBOUNCE_SEC), "--json"]
        return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=dict(os.environ))

    def start(self) -> dict:
        with self._lock:
            self._reap()
            if self.proc is None:
                self.proc = self._spawn()
                self.started_at, self.finished_at, self.result, self.error = time.time(), None, None, None
            return self.state()

    def _reap(self) -> None:
        if self.proc is not None and self.proc.poll() is not None:
            out, err = self.proc.communicate()
            self.finished_at = time.time()
            try:
                self.result = json.loads(out.strip().splitlines()[-1]) if out and out.strip() else None
            except (ValueError, IndexError):
                self.result = None
            if self.proc.returncode != 0 or self.result is None:
                self.error = (err or out or "").strip()[-300:] or f"sweep exited {self.proc.returncode}"
            self.proc = None

    def state(self) -> dict:
        with self._lock:
            self._reap()
            running = self.proc is not None
            return {
                "running": running,
                "started_at": self.started_at,
                "elapsed": round(time.time() - self.started_at, 1) if self.started_at and running else None,
                "finished_at": self.finished_at,
                "result": self.result,
                "error": self.error,
            }


def create_app(sweeper: SweepRunner | None = None) -> FastAPI:
    app = FastAPI(title="Claude Code Inbox", docs_url=None, redoc_url=None)
    runner = sweeper or SweepRunner()

    @app.middleware("http")
    async def require_token(request: Request, call_next):
        if request.url.path.startswith("/api/") and not _authorized(request):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        response = await call_next(request)
        # Shell files change in place (editable install); make browsers revalidate instead of heuristically caching.
        if not request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/items")
    def list_items():
        conn = db.connect()
        try:
            items = [db.row_to_dict(r) for r in db.inbox(conn)]
            snoozed = [db.row_to_dict(r) for r in db.snoozed_items(conn)]
            resolved = [db.row_to_dict(r) for r in db.recently_resolved(conn)]
        finally:
            conn.close()
        return {
            "items": items,
            "snoozed": snoozed,
            "resolved": resolved,
            "counts": {"open": len(items), "snoozed": len(snoozed), "resolved": len(resolved)},
        }

    @app.post("/api/items/{item_id}")
    def act(item_id: int, body: ActionBody):
        if body.action not in db.ACTIONS:
            raise HTTPException(400, f"unknown action {body.action!r}")
        conn = db.connect()
        try:
            try:
                row = db.apply_action(conn, item_id, body.action, body.until)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        finally:
            conn.close()
        if row is None:
            raise HTTPException(404, "no such item")
        return db.row_to_dict(row)

    @app.post("/api/sweep")
    def start_sweep():
        return runner.start()

    @app.get("/api/sweep")
    def sweep_state():
        return runner.state()

    @app.post("/api/bulk")
    def act_bulk(body: BulkBody):
        if body.action not in db.ACTIONS:
            raise HTTPException(400, f"unknown action {body.action!r}")
        if not body.ids or len(body.ids) > 200:
            raise HTTPException(400, "ids must hold 1-200 item ids")
        conn = db.connect()
        try:
            updated, missing = [], []
            for item_id in body.ids:
                try:
                    row = db.apply_action(conn, item_id, body.action, body.until)
                except ValueError as exc:
                    raise HTTPException(400, str(exc)) from exc
                (updated if row is not None else missing).append(item_id if row is None else db.row_to_dict(row))
        finally:
            conn.close()
        return {"updated": updated, "missing": missing}

    @app.get("/api/sessions")
    def list_sessions(limit: int = 50):
        conn = db.connect()
        try:
            return {"sessions": [db.row_to_dict(r) for r in db.sessions(conn, limit)], "stats": db.stats(conn)}
        finally:
            conn.close()

    if config.WEB_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(config.WEB_DIR), html=True), name="web")
    return app


app = create_app()
