"""Ingest: find sources -> download -> pick highlights -> caption -> render."""
from __future__ import annotations

import logging
import random
import tempfile
from pathlib import Path

from . import captions, cutting
from .config import AppConfig, Settings
from .db import DB
from .sources import inbox, youtube

log = logging.getLogger(__name__)


def discover(settings: Settings, app: AppConfig, db: DB) -> int:
    added = inbox.scan(db, settings.inbox_dir, settings.raw_dir)
    if app.sources.youtube_channels:
        if not settings.youtube_api_key:
            log.warning("YOUTUBE_API_KEY not set; skipping YouTube discovery")
        else:
            added += youtube.discover(db, app.sources.youtube_channels, app.sources.title_keywords,
                                      settings.youtube_api_key, app.sources.max_videos_per_ingest)
    return added


def download_new(settings: Settings, db: DB) -> None:
    for src in db.sources_with_status("new"):
        try:
            path = youtube.download(src["url"], settings.raw_dir, src["external_id"])
            db.update_source(src["id"], local_path=str(path), status="downloaded")
        except Exception as e:
            log.exception("download failed: %s", src["url"])
            db.update_source(src["id"], status="failed", error=str(e)[:1000])


def cut_source(settings: Settings, app: AppConfig, db: DB, src, with_captions: bool = True) -> list[int]:
    cfg = app.sources
    path = Path(src["local_path"])
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
                ass = captions.build_ass(chunks, captions.pick_hook(src["title"]), seg.duration,
                                         Path(tmp) / "subs.ass")
            cutting.render_vertical(path, seg.start, seg.end, out, ass)
        caption = captions.post_caption(random.choice(app.caption_templates), src["title"], src["credit"])
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
