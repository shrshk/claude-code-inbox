"""FastAPI: ``GET /api/items``, ``POST /api/items/{id}`` and the static PWA under ``web/``."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ccinbox import config, db


class ActionBody(BaseModel):
    action: str
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


def create_app() -> FastAPI:
    app = FastAPI(title="Claude Code Inbox", docs_url=None, redoc_url=None)

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
