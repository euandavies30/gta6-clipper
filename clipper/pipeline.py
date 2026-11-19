"""Ingest: find sources -> download -> pick highlights -> caption -> render."""
from __future__ import annotations

import logging
import random
import tempfile
from pathlib import Path

from . import captions, cutting
from .config import AppConfig, Settings
from .db import DB
from .sources import inbox, rockstar, youtube

log = logging.getLogger(__name__)


def discover(settings: Settings, app: AppConfig, db: DB) -> int:
    added = inbox.scan(db, settings.inbox_dir, settings.raw_dir)
    if app.sources.rockstar_media:
        added += rockstar.discover(db, app.sources.max_videos_per_ingest)
    if app.sources.youtube_channels:
        if not settings.youtube_api_key:
            log.warning("YOUTUBE_API_KEY not set; skipping YouTube discovery")
        else:
            added += youtube.discover(db, app.sources.youtube_channels, app.sources.title_keywords,
                                      settings.youtube_api_key, app.sources.max_videos_per_ingest)
    return added


def is_direct_video(url: str | None) -> bool:
    return bool(url) and url.split("?", 1)[0].lower().endswith((".mp4", ".mov", ".m4v", ".webm"))


def free_raw(settings: Settings, src) -> None:
    """Delete a downloaded source file once it has been clipped (volumes are small)."""
    lp = src["local_path"] or ""
    if lp and not lp.startswith("http"):
        try:
            p = Path(lp).resolve()
            if p.parent == settings.raw_dir.resolve():
                p.unlink(missing_ok=True)
        except OSError:
            pass


def download_new(settings: Settings, db: DB) -> None:
    cookies = youtube.cookie_file(settings.data_dir)
    # Failed YouTube downloads are retried each ingest (e.g. after cookies are added).
    retry = [s for s in db.sources_with_status("failed") if s["kind"] in ("youtube", "rockstar") and not s["local_path"]]
    for src in list(db.sources_with_status("new")) + retry:
        try:
            if is_direct_video(src["url"]):
                # ffmpeg reads direct MP4 links over HTTP, so big 4K trailers never touch the disk.
                db.update_source(src["id"], local_path=src["url"], status="downloaded")
                continue
            path = youtube.download(src["url"], settings.raw_dir, f"{src['kind']}_{src['id']}", cookies)
            db.update_source(src["id"], local_path=str(path), status="downloaded")
        except Exception as e:
            log.error("download failed: %s: %s", src["url"], str(e).splitlines()[0][:300])
            db.update_source(src["id"], status="failed", error=str(e)[:1000])


MOMENT_CAPTION = "{title} 😭\n{credit}\nFollow for daily GTA 6 streamer clips"


def cut_source(settings: Settings, app: AppConfig, db: DB, src, with_captions: bool = True,
               whole: bool = False) -> list[int]:
    """Cut a source into clips.

    whole=True is for a streamer moment that was already trimmed to the wanted window: it becomes
    exactly one clip, the title is the on-screen hook for the whole clip, and nothing is re-picked.
    """
    cfg = app.sources
    lp = src["local_path"]
    path = lp if lp.startswith("http") else Path(lp)
    if whole:
        dur = cutting.probe_duration(path)
        segments = [cutting.Segment(0.0, round(dur, 2), 1.0)] if dur > 1 else []
    else:
        segments = cutting.find_highlights(path, cfg.clips_per_video, cfg.clip_min_seconds, cfg.clip_max_seconds)
    clip_ids = []
    for i, seg in enumerate(segments):
        out = settings.clips_dir / f"s{src['id']}_c{i}_{int(seg.start)}.mp4"
        transcript = ""
        with tempfile.TemporaryDirectory() as tmp:
            ass = None
            if with_captions:
                chunks = []
                try:
                    wav = cutting.extract_audio(path, seg.start, seg.end, Path(tmp) / "a.wav")
                    words = captions.transcribe(wav, settings.whisper_model)
                    transcript = " ".join(w["word"] for w in words)
                    chunks = captions.chunk_words(words)
                except Exception as e:  # e.g. model download blocked: still ship the clip with its hook
                    log.warning("transcription failed, rendering without speech captions: %s", e)
                if whole:
                    hook, hook_s = captions.clean_hook(src["title"] or ""), seg.duration
                else:
                    hook, hook_s = captions.pick_hook(src["title"]), 4.0
                ass = captions.build_ass(chunks, hook or None, seg.duration, Path(tmp) / "subs.ass", hook_s)
            cutting.render_vertical(path, seg.start, seg.end, out, ass)
        template = MOMENT_CAPTION if whole else random.choice(app.caption_templates)
        caption = captions.post_caption(template, src["title"], src["credit"])
        status = "pending" if settings.review_mode else "approved"
        clip_ids.append(db.add_clip(src["id"], seg.start, seg.end, seg.score, out, transcript, caption, status))
        log.info("clip %s: %.1fs-%.1fs -> %s", clip_ids[-1], seg.start, seg.end, out.name)
    return clip_ids


def cut_downloaded(settings: Settings, app: AppConfig, db: DB, with_captions: bool = True) -> int:
    total = 0
    for src in db.sources_with_status("downloaded"):
        try:
            total += len(cut_source(settings, app, db, src, with_captions))
            db.update_source(src["id"], status="cut")
            free_raw(settings, src)
        except Exception as e:
            log.exception("cutting failed for source %s", src["id"])
            db.update_source(src["id"], status="failed", error=str(e)[:1000])
    return total


def ingest(settings: Settings, app: AppConfig, db: DB, with_captions: bool = True) -> dict:
    settings.ensure_dirs()
    found = discover(settings, app, db)
    download_new(settings, db)
    made = cut_downloaded(settings, app, db, with_captions)
    return {"new_sources": found, "new_clips": made}
