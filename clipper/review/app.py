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
import re
import secrets
import shutil
import threading
import time
import urllib.parse
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
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


_LEGAL = {
    "privacy": ("Privacy Policy", """
<p>GTA VI Clips is a private tool that publishes short video clips to social media accounts owned by its operator.</p>
<p><b>What we access.</b> When an account owner connects a TikTok or Instagram account, we receive an access token and
basic profile information (such as the account's display name). We use these only to publish videos that the
account owner has approved.</p>
<p><b>What we store.</b> Access tokens and post history are stored on our server and are never sold or shared with
third parties. We do not collect data about viewers or other users.</p>
<p><b>Removal.</b> You can disconnect at any time from your TikTok or Instagram settings. To have stored data deleted,
contact {email}.</p>"""),
    "terms": ("Terms of Service", """
<p>GTA VI Clips is a private publishing tool used only by its operator to post to accounts they own.</p>
<p>By connecting an account you authorise the tool to upload and publish videos to it on your behalf. You are
responsible for the content posted and for following TikTok's and Instagram's terms and community guidelines.</p>
<p>The service is provided as is, without warranty. Questions: {email}.</p>"""),
}


@app.get("/privacy", response_class=HTMLResponse)
@app.get("/terms", response_class=HTMLResponse)
def legal(request: Request):
    title, body = _LEGAL[request.url.path.strip("/")]
    email = os.getenv("CONTACT_EMAIL", "the account owner")
    return (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            f"<title>{title}</title><body style='font:16px/1.6 system-ui;max-width:680px;margin:40px auto;"
            f"padding:0 16px'><h1>{title}</h1>{body.format(email=email)}</body>")


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


# Manual upload ---------------------------------------------------------------------
@app.post("/upload")
def upload(file: UploadFile = File(...), title: str = Form(""), credit: str = Form(""), _=Depends(auth)):
    """Upload a video (e.g. a trailer you downloaded, or a streamer clip you have permission for).

    The file is saved and clipped in the background; new clips show up in Pending a few minutes later.
    """
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(file.filename or "upload.mp4").name)
    if Path(name).suffix.lower() not in {".mp4", ".mov", ".mkv", ".webm", ".m4v"}:
        raise HTTPException(400, "Upload an .mp4, .mov, .mkv, .webm or .m4v video")
    dest = settings.raw_dir / f"{int(time.time())}_{name}"
    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out, length=8 * 1024 * 1024)
    src_id = db.add_source("inbox", dest.name, title=title.strip() or Path(name).stem,
                           credit=credit.strip() or None, local_path=str(dest))

    def _cut():
        try:
            app_cfg = load_app_config(settings.accounts_file)
            pipeline.cut_source(settings, app_cfg, db, db.get_source(src_id))
            db.update_source(src_id, status="cut")
        except Exception as e:
            log.exception("clipping upload %s failed", dest.name)
            db.update_source(src_id, status="failed", error=str(e)[:1000])

    if src_id:
        threading.Thread(target=_cut, daemon=True, name=f"cut-{src_id}").start()
    return RedirectResponse("/?status=pending&uploaded=1", status_code=303)


# Domain verification (must stay the last route: it matches any top-level path) --------
@app.get("/{name}", response_class=PlainTextResponse)
def site_verification(name: str):
    """Serves the URL-verification file TikTok's developer portal asks for.

    Set TIKTOK_VERIFY_FILENAME (e.g. tiktokAbC123.txt) and TIKTOK_VERIFY_CONTENT to the file's text.
    Works with or without the .txt on the end of the URL.
    """
    def base(n: str) -> str:
        n = n.strip().strip('"').rsplit("/", 1)[-1]
        return n[:-4] if n.lower().endswith(".txt") else n

    wanted = base(os.getenv("TIKTOK_VERIFY_FILENAME") or "")
    content = (os.getenv("TIKTOK_VERIFY_CONTENT") or "").strip().strip('"')
    if not wanted or not content or base(name).lower() != wanted.lower():
        if name.lower().startswith("tiktok"):
            log.warning("verification file %s requested; configured name is %r", name, wanted or None)
        raise HTTPException(404)
    return content
