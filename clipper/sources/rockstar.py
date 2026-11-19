"""Rockstar's official GTA VI media page (rockstargames.com/VI/media/videos).

Rockstar publishes trailers there as direct MP4 downloads "to download and share",
so it is the cleanest automatic source, and unlike YouTube it doesn't block servers.
"""
from __future__ import annotations

import logging
import re

import requests

from ..db import DB

log = logging.getLogger(__name__)
PAGE = "https://www.rockstargames.com/VI/media/videos"
MP4 = re.compile(r"https://media-rockstargames-com\.akamaized\.net/VI/downloads/videos/[^\"'\s\\)]+?\.mp4")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"}


def list_videos(html: str | None = None) -> list[dict]:
    if html is None:
        html = requests.get(PAGE, headers=UA, timeout=30).text
    out, seen = [], set()
    for url in MP4.findall(html):
        if url in seen:
            continue
        seen.add(url)
        name = url.rsplit("/", 1)[-1][:-4]
        title = name.replace("GTAVI_", "GTA 6 ").replace("_", " ").strip()
        out.append({"id": name, "url": url, "title": title})
    return out


def discover(db: DB, limit: int, skip: tuple[str, ...] = ("Cover_Art",)) -> int:
    try:
        vids = list_videos()
    except Exception as e:
        log.error("rockstar media page failed: %s", e)
        return 0
    added = 0
    for v in vids:
        if added >= limit:
            break
        if any(s in v["id"] for s in skip):
            continue
        if db.add_source("rockstar", v["id"], v["title"], v["url"], credit="Rockstar Games"):
            log.info("new source: %s", v["title"])
            added += 1
    return added
