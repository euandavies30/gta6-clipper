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


@app.middleware("http")
async def collapse_slashes(request: Request, call_next):
    """Treat //path like /path (TikTok's URL check adds a slash after a prefix that already ends in one)."""
    path = request.scope["path"]
    if "//" in path:
        request.scope["path"] = re.sub(r"/{2,}", "/", path)
    # A URL prefix saved with a stray space makes TikTok fetch "/%20/tiktok....txt": serve the last segment.
    last = request.scope["path"].rstrip("/").rsplit("/", 1)[-1]
    if last.lower().startswith("tiktok") and request.scope["path"] != f"/{last}":
        request.scope["path"] = f"/{last}"
    if request.scope["path"].lower().startswith("/tiktok"):
        log.info("verification check: %s from %s", path, request.headers.get("user-agent", "?")[:80])
    return await call_next(request)


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
    connections = {}
    for a in app_cfg.accounts:
        tok = TokenStore(settings, a).load()
        connections[a.name] = tok.get("display_name") or ("connected" if tok.get("refresh_token") or tok.get("access_token") else None)
    return templates.TemplateResponse(request, "queue.html", {
        "clips": db.clips(status), "status": status, "counts": counts,
        "review_mode": settings.review_mode, "dry_run": settings.dry_run, "accounts": app_cfg.accounts,
        "connections": connections, "flash": request.query_params.get("msg"),
    })


@app.post("/clips/{clip_id}/post")
def post_now(clip_id: int, account: str = Form(...), caption: str | None = Form(None), _=Depends(auth)):
    """Post one clip to one account right now (used for testing and the TikTok review demo)."""
    clip = db.get_clip(clip_id)
    acc = next((a for a in load_app_config(settings.accounts_file).accounts if a.name == account), None)
    if not clip or not acc:
        raise HTTPException(404)
    if caption is not None:
        db.update_clip(clip_id, caption=caption)
    db.update_clip(clip_id, status="approved") if clip["status"] == "pending" else None

    def _run():
        from ..captions import with_hashtags
        from ..posters import get_poster
        poster = get_poster(acc, settings)
        text = with_hashtags((caption if caption is not None else clip["caption"]) or "", acc.hashtags)
        try:
            res = poster.post(Path(clip["path"]), text)
            db.record_post(clip_id, acc.name, acc.platform, "dry_run" if poster.name == "dryrun" else "ok",
                           res.remote_id or res.note)
            db.update_clip(clip_id, status="posted")
        except Exception as e:
            log.exception("post now failed")
            db.record_post(clip_id, acc.name, acc.platform, "failed", error=str(e)[:1000])

    threading.Thread(target=_run, daemon=True, name=f"post-{clip_id}").start()
    return RedirectResponse("/posts?msg=" + urllib.parse.quote(
        f"Posting clip #{clip_id} to {acc.name}. Refresh in a minute to see the result."), status_code=303)


PRIVACY_LABELS = {"PUBLIC_TO_EVERYONE": "Everyone", "MUTUAL_FOLLOW_FRIENDS": "Friends",
                  "FOLLOWER_OF_CREATOR": "Followers", "SELF_ONLY": "Only me"}


def _account(name: str):
    acc = next((a for a in load_app_config(settings.accounts_file).accounts if a.name == name), None)
    if not acc:
        raise HTTPException(404, "unknown account")
    return acc


from ..captions import with_hashtags  # noqa: E402


@app.get("/clips/{clip_id}/publish", response_class=HTMLResponse)
def publish_form(request: Request, clip_id: int, account: str, _=Depends(auth)):
    """TikTok's required pre-post screen: creator, privacy (no default), interactions, disclosure, consent."""
    clip, acc = db.get_clip(clip_id), _account(account)
    if not clip:
        raise HTTPException(404)
    creator, error = {}, None
    inbox = (acc.mode or os.getenv("TIKTOK_MODE", "direct")).lower() == "inbox"
    if acc.platform == "tiktok" and not settings.dry_run and not inbox:
        from ..posters.tiktok import TikTokPoster
        try:
            creator = TikTokPoster(acc, settings).creator_info()
        except Exception as e:
            error = str(e)
    duration = clip["end_s"] - clip["start_s"]
    max_dur = creator.get("max_video_post_duration_sec")
    return templates.TemplateResponse(request, "publish.html", {
        "clip": clip, "account": acc, "creator": creator, "error": error, "labels": PRIVACY_LABELS,
        "privacy_options": creator.get("privacy_level_options") or ["SELF_ONLY"],
        "too_long": bool(max_dur and duration > max_dur), "duration": duration, "max_dur": max_dur,
        "caption": with_hashtags(clip["caption"] or "", acc.hashtags), "inbox": inbox,
    })


@app.post("/clips/{clip_id}/publish")
def publish(clip_id: int, account: str = Form(...), caption: str = Form(""), privacy: str = Form("SELF_ONLY"),
            allow_comment: bool = Form(False), allow_duet: bool = Form(False), allow_stitch: bool = Form(False),
            disclose: bool = Form(False), brand_organic: bool = Form(False), brand_content: bool = Form(False),
            _=Depends(auth)):
    clip, acc = db.get_clip(clip_id), _account(account)
    if not clip:
        raise HTTPException(404)
    if privacy not in PRIVACY_LABELS:
        raise HTTPException(400, "choose who can view this video")
    if disclose and not (brand_organic or brand_content):
        raise HTTPException(400, "pick Your brand and/or Branded content, or turn disclosure off")
    db.update_clip(clip_id, caption=caption)
    opts = dict(privacy=privacy, allow_comment=allow_comment, allow_duet=allow_duet, allow_stitch=allow_stitch,
                brand_organic=disclose and brand_organic, brand_content=disclose and brand_content)

    def _run():
        from ..posters import get_poster
        poster = get_poster(acc, settings)
        try:
            res = poster.post(Path(clip["path"]), caption, **opts)
            db.record_post(clip_id, acc.name, acc.platform, "dry_run" if poster.name == "dryrun" else "ok",
                           res.remote_id or res.note)
            db.update_clip(clip_id, status="posted")
        except Exception as e:
            log.exception("publish failed")
            db.record_post(clip_id, acc.name, acc.platform, "failed", error=str(e)[:1000])

    threading.Thread(target=_run, daemon=True, name=f"publish-{clip_id}").start()
    return RedirectResponse("/posts?msg=" + urllib.parse.quote(
        f"Sent clip #{clip_id} to {acc.name}. TikTok can take a few minutes to process it before it shows "
        "on the profile."), status_code=303)


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
    return templates.TemplateResponse(request, "posts.html", {
        "posts": db.recent_posts(100), "flash": request.query_params.get("msg"), "dry_run": settings.dry_run})


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
        "disable_auto_auth": 1,  # always show TikTok's consent screen, so you can check which account it is
    })
    return RedirectResponse(f"{tiktok.AUTH_URL}?{q}")


@app.get("/oauth/tiktok/callback", response_class=HTMLResponse)
def tiktok_callback(code: str = "", state: str = "", error: str = ""):
    account_name = _oauth_states.pop(state, None)
    if error or not account_name:
        raise HTTPException(400, f"TikTok auth failed: {error or 'unknown state'}")
    account = next(a for a in load_app_config(settings.accounts_file).accounts if a.name == account_name)
    data = tiktok.exchange_code(code, _redirect_uri())
    data["display_name"] = tiktok.display_name(data["access_token"]) or account_name
    TokenStore(settings, account).save(data)
    msg = f"Connected {account_name} as {data['display_name']}"
    return RedirectResponse("/?status=approved&msg=" + urllib.parse.quote(msg), status_code=303)


# Background worker ---------------------------------------------------------------
def worker_loop(stop: threading.Event) -> None:
    last_ingest = 0.0
    while not stop.is_set():
        try:
            app_cfg = load_app_config(settings.accounts_file)
            if _ingest_now.is_set() or time.time() - last_ingest > settings.ingest_every_hours * 3600:
                _ingest_now.clear()
                log.info("ingest: %s", pipeline.ingest(settings, app_cfg, db))
                last_ingest = time.time()
            scheduler.tick(settings, app_cfg, db)
        except Exception:
            log.exception("worker tick failed")
        _ingest_now.wait(settings.tick_seconds)


_stop = threading.Event()
_ingest_now = threading.Event()  # set to make the worker ingest on its next loop


@app.on_event("startup")
def _start_worker():
    if os.getenv("RUN_WORKER", "true").lower() in {"1", "true", "yes"}:
        # A new deploy usually carries a fix, so give failed sources another go.
        log.info("startup: re-queued %s failed sources", _requeue_failed())
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
            pipeline.free_raw(settings, db.get_source(src_id))
        except Exception as e:
            log.exception("clipping upload %s failed", dest.name)
            db.update_source(src_id, status="failed", error=str(e)[:1000])

    if src_id:
        threading.Thread(target=_cut, daemon=True, name=f"cut-{src_id}").start()
    return RedirectResponse("/?status=pending&uploaded=1", status_code=303)


@app.post("/add-url")
def add_url(url: str = Form(...), title: str = Form(""), credit: str = Form(""),
            start: str = Form(""), length: str = Form(""), _=Depends(auth)):
    """Clip a video from a link (YouTube, Twitch clip/VOD, or a direct .mp4) in the background.

    With a start time (or a ?t= in the link, e.g. a Twitch VOD timestamp) it becomes a single
    "moment" clip of `length` seconds (default 65, so it clears TikTok's 1-minute rewards bar),
    and only that part of the VOD is downloaded.
    """
    url = url.strip()
    if not re.match(r"https?://", url):
        raise HTTPException(400, "Paste a full http(s) link")
    from ..sources import youtube as yt
    try:
        t0 = yt.parse_time(start)
        if t0 is None:
            t0 = yt.start_from_url(url)
        secs = float(length) if length.strip() else 65.0
    except ValueError as e:
        raise HTTPException(400, str(e))
    secs = max(10.0, min(secs, 180.0))
    section = (t0, t0 + secs) if t0 is not None else None
    from ..sources import twitch
    expand_clip = section is None and twitch.is_clip(url)

    def _run():
        nonlocal url, section
        src_id = None
        try:
            key = re.sub(r"[^A-Za-z0-9]+", "_", url)[-70:] + (f"_{int(section[0])}" if section else "")
            src_id = db.add_source("link", key, title=title.strip() or None, url=url, credit=credit.strip() or None)
            if not src_id:
                return
            if expand_clip:
                # A short Twitch clip -> the full minute around it from the streamer's VOD.
                try:
                    m = twitch.find_moment(url, secs)
                except Exception as e:
                    log.warning("couldn't find the VOD for %s, using the clip itself: %s", url, e)
                    m = None
                if m:
                    url, section = m.vod_url, (m.start, m.start + secs)
                    db.update_source(src_id, url=url)
                    if not title.strip() and m.title:
                        db.update_source(src_id, title=m.title)
                    if not credit.strip() and m.channel:
                        db.update_source(src_id, credit=m.channel)
            if pipeline.is_direct_video(url) and not section:
                path = url  # streamed by ffmpeg, never stored
            else:
                path = str(yt.download(url, settings.raw_dir, f"link{src_id}", yt.cookie_file(settings.data_dir),
                                       section=section))
            db.update_source(src_id, local_path=path, status="downloaded")
            src = db.get_source(src_id)
            if not src["title"]:
                db.update_source(src_id, title=Path(path.split("?")[0]).stem)
            pipeline.cut_source(settings, load_app_config(settings.accounts_file), db, db.get_source(src_id),
                                whole=bool(section))
            db.update_source(src_id, status="cut")
            pipeline.free_raw(settings, db.get_source(src_id))
        except Exception as e:
            log.error("clip from link failed: %s: %s", url, str(e).splitlines()[0][:300])
            if src_id:
                db.update_source(src_id, status="failed", error=str(e)[:1000])

    threading.Thread(target=_run, daemon=True, name="add-url").start()
    return RedirectResponse("/?status=pending&msg=" + urllib.parse.quote(
        "Downloading and clipping that link. Clips appear here in a few minutes; refresh to check."), status_code=303)


@app.get("/sources", response_class=PlainTextResponse)
def sources(_=Depends(auth)):
    """Plain-text list of sources and their status (handy for debugging failed downloads)."""
    with db.conn() as c:
        rows = c.execute("SELECT id, kind, status, title, url, substr(error,-400) e FROM sources ORDER BY id DESC LIMIT 50").fetchall()
    return "\n".join(f"#{r['id']} [{r['kind']}/{r['status']}] {r['title']} {r['url'] or ''} {r['e'] or ''}" for r in rows)


def _requeue_failed() -> int:
    with db.conn() as c:
        return c.execute("UPDATE sources SET status = CASE WHEN local_path IS NOT NULL AND local_path != '' "
                         "THEN 'downloaded' ELSE 'new' END, error = NULL WHERE status = 'failed'").rowcount


@app.post("/sources/retry", response_class=PlainTextResponse)
def retry_sources(_=Depends(auth)):
    """Re-queue failed sources and ask the worker to ingest now."""
    n = _requeue_failed()
    _ingest_now.set()
    return f"re-queued {n} failed sources; ingest starting"


# Domain verification (must stay the last route: it matches any top-level path) --------
@app.get("/{name}", response_class=PlainTextResponse)
def site_verification(name: str):
    """Serves the URL-verification file TikTok's developer portal asks for.

    No setup needed: the signature is derived from the file name TikTok asks for.
    """
    # TikTok's signature file "tiktok<TOKEN>.txt" contains "tiktok-developers-site-verification=<TOKEN>",
    # so the reply is derived from the requested name: every URL property verifies without new variables.
    m = re.fullmatch(r"tiktok([A-Za-z0-9]{8,64})(?:\.txt)?", name.strip())
    if not m:
        raise HTTPException(404)
    return f"tiktok-developers-site-verification={m.group(1)}"
