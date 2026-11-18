"""Review web app + background worker.

  /                 review queue (approve / reject / edit caption)
  /posts            recent posts and failures
  /media/<file>     public clip files (platforms fetch videos from here)
  /oauth/tiktok/... connect a TikTok account and store its tokens
  /health           liveness check

Everything except /media, /health and the TikTok callback needs basic auth
(REVIEW_USER / REVIEW_PASSWORD).
"""
from __future__ import annotations

import logging
import os
import secrets
import threading
import time
import urllib.parse
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from .. import pipeline, scheduler
from ..config import Settings, load_app_config
from ..db import DB
from ..posters.base import TokenStore
from ..posters import tiktok

log = logging.getLogger(__name__)
settings = Settings()
settings.ensure_dirs()
db = DB(settings.db_path)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
security = HTTPBasic()
app = FastAPI(title="GTA 6 Clipper")
_oauth_states: dict[str, str] = {}


def auth(creds: HTTPBasicCredentials = Depends(security)) -> str:
    if not settings.review_password:
        raise HTTPException(503, "Set REVIEW_PASSWORD to use the review app")
    ok = secrets.compare_digest(creds.username, settings.review_user) and \
        secrets.compare_digest(creds.password, settings.review_password)
    if not ok:
        raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})
    return creds.username


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def queue(request: Request, status: str = "pending", _=Depends(auth)):
    app_cfg = load_app_config(settings.accounts_file)
    counts = {s: len(db.clips(s, limit=10000)) for s in ("pending", "approved", "posted", "rejected")}
    return templates.TemplateResponse(request, "queue.html", {
        "clips": db.clips(status), "status": status, "counts": counts,
        "review_mode": settings.review_mode, "dry_run": settings.dry_run, "accounts": app_cfg.accounts,
    })


@app.post("/clips/{clip_id}/{action}")
def act(clip_id: int, action: str, caption: str | None = Form(None), back: str = Form("pending"), _=Depends(auth)):
    if not db.get_clip(clip_id):
        raise HTTPException(404)
    if action == "approve":
        db.update_clip(clip_id, status="approved", **({"caption": caption} if caption is not None else {}))
    elif action == "reject":
        db.update_clip(clip_id, status="rejected")
    elif action == "caption" and caption is not None:
        db.update_clip(clip_id, caption=caption)
    else:
        raise HTTPException(400, "unknown action")
    return RedirectResponse(f"/?status={urllib.parse.quote(back)}", status_code=303)


@app.get("/posts", response_class=HTMLResponse)
def posts(request: Request, _=Depends(auth)):
    return templates.TemplateResponse(request, "posts.html", {"posts": db.recent_posts(100)})


@app.get("/media/{name}")
def media(name: str):
    path = (settings.clips_dir / name).resolve()
    if path.parent != settings.clips_dir.resolve() or not path.is_file() or path.suffix != ".mp4":
        raise HTTPException(404)
    return FileResponse(path, media_type="video/mp4")


# TikTok OAuth ------------------------------------------------------------------
def _redirect_uri() -> str:
    if not settings.public_base_url:
        raise HTTPException(500, "PUBLIC_BASE_URL must be set for OAuth")
    return f"{settings.public_base_url.rstrip('/')}/oauth/tiktok/callback"


@app.get("/oauth/tiktok/start")
def tiktok_start(account: str, _=Depends(auth)):
    names = [a.name for a in load_app_config(settings.accounts_file).accounts if a.platform == "tiktok"]
    if account not in names:
        raise HTTPException(404, f"no TikTok account named {account}")
    state = secrets.token_urlsafe(16)
    _oauth_states[state] = account
    q = urllib.parse.urlencode({
        "client_key": os.environ.get("TIKTOK_CLIENT_KEY", ""), "scope": tiktok.SCOPES,
        "response_type": "code", "redirect_uri": _redirect_uri(), "state": state,
    })
    return RedirectResponse(f"{tiktok.AUTH_URL}?{q}")


@app.get("/oauth/tiktok/callback", response_class=HTMLResponse)
def tiktok_callback(code: str = "", state: str = "", error: str = ""):
    account_name = _oauth_states.pop(state, None)
    if error or not account_name:
        raise HTTPException(400, f"TikTok auth failed: {error or 'unknown state'}")
    account = next(a for a in load_app_config(settings.accounts_file).accounts if a.name == account_name)
    TokenStore(settings, account).save(tiktok.exchange_code(code, _redirect_uri()))
    return f"<p>Connected TikTok account <b>{account_name}</b>. You can close this tab.</p>"


# Background worker ---------------------------------------------------------------
def worker_loop(stop: threading.Event) -> None:
    last_ingest = 0.0
    while not stop.is_set():
        try:
            app_cfg = load_app_config(settings.accounts_file)
            if time.time() - last_ingest > settings.ingest_every_hours * 3600:
                log.info("ingest: %s", pipeline.ingest(settings, app_cfg, db))
                last_ingest = time.time()
            scheduler.tick(settings, app_cfg, db)
        except Exception:
            log.exception("worker tick failed")
        stop.wait(settings.tick_seconds)


_stop = threading.Event()


@app.on_event("startup")
def _start_worker():
    if os.getenv("RUN_WORKER", "true").lower() in {"1", "true", "yes"}:
        threading.Thread(target=worker_loop, args=(_stop,), daemon=True, name="worker").start()


@app.on_event("shutdown")
def _stop_worker():
    _stop.set()
