"""Inbox source: drop video files into DATA_DIR/inbox to have them clipped.

Use it for clips you have permission to post (e.g. a streamer said yes).
Name files like `Credit Name - Title.mp4` to set the on-caption credit;
otherwise the file name becomes the title and there is no credit.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from ..db import DB

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}


def scan(db: DB, inbox: Path, raw_dir: Path) -> int:
    added = 0
    for f in sorted(inbox.iterdir()) if inbox.exists() else []:
        if f.suffix.lower() not in VIDEO_EXT or not f.is_file():
            continue
        stem = f.stem
        credit, title = (s.strip() for s in stem.split(" - ", 1)) if " - " in stem else (None, stem)
        dest = raw_dir / f.name
        shutil.move(str(f), dest)
        if db.add_source("inbox", f.name, title=title, credit=credit, local_path=str(dest)):
            added += 1
    return added
