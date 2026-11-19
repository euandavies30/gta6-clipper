"""Find new uploads on allow-listed YouTube channels and download them.

Discovery uses the official YouTube Data API v3 (needs YOUTUBE_API_KEY).
Download uses yt-dlp, because the Data API has no download endpoint.
Only list channels you have the right to repost from in accounts.yaml.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import requests

from ..db import DB

log = logging.getLogger(__name__)
API = "https://www.googleapis.com/youtube/v3"


def _get(path: str, api_key: str, **params) -> dict:
    r = requests.get(f"{API}/{path}", params={**params, "key": api_key}, timeout=30)
    r.raise_for_status()
    return r.json()


def uploads_playlist(channel: str, api_key: str) -> tuple[str, str]:
    """Return (uploads playlist id, channel title) for '@handle' or a UC... channel id."""
    params = {"part": "contentDetails,snippet"}
    if channel.startswith("@"):
        params["forHandle"] = channel
    else:
        params["id"] = channel
    items = _get("channels", api_key, **params).get("items", [])
    if not items:
        raise ValueError(f"YouTube channel not found: {channel}")
    it = items[0]
    return it["contentDetails"]["relatedPlaylists"]["uploads"], it["snippet"]["title"]


def latest_videos(channel: str, api_key: str, max_results: int = 25) -> list[dict]:
    playlist, channel_title = uploads_playlist(channel, api_key)
    data = _get("playlistItems", api_key, part="snippet", playlistId=playlist, maxResults=min(max_results, 50))
    out = []
    for it in data.get("items", []):
        sn = it["snippet"]
        vid = sn["resourceId"]["videoId"]
        out.append({
            "id": vid,
            "title": sn["title"],
            "published_at": sn.get("publishedAt"),
            "url": f"https://www.youtube.com/watch?v={vid}",
            "channel": channel_title,
        })
    return out


def matches(title: str, keywords: list[str]) -> bool:
    if not keywords:
        return True
    return any(re.search(k, title, re.IGNORECASE) for k in keywords)


def discover(db: DB, channels: list[str], keywords: list[str], api_key: str, limit: int) -> int:
    added = 0
    for ch in channels:
        try:
            vids = latest_videos(ch, api_key)
        except Exception as e:  # keep going for other channels
            log.error("discover %s failed: %s", ch, e)
            continue
        for v in vids:
            if added >= limit:
                return added
            if not matches(v["title"], keywords):
                continue
            if db.add_source("youtube", v["id"], v["title"], v["url"], credit=v["channel"]):
                log.info("new source: %s", v["title"])
                added += 1
    return added


def cookie_file(data_dir: Path) -> Path | None:
    """YouTube makes logged-out servers "sign in to confirm your age" for trailers.

    Put the contents of a Netscape-format cookies.txt (exported from a browser that is
    signed in to an 18+ YouTube account) in YTDLP_COOKIES and downloads use it.
    """
    raw = os.getenv("YTDLP_COOKIES", "").strip()
    if not raw:
        return None
    path = data_dir / "yt-cookies.txt"
    if not raw.startswith("# Netscape"):
        raw = "# Netscape HTTP Cookie File\n" + raw
    path.write_text(raw.replace("\\t", "\t") + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def parse_time(value: str | None) -> float | None:
    """'1:02:03', '62:03', '3723', '1h2m3s' or '2m' -> seconds. Blank -> None."""
    v = (value or "").strip().lower()
    if not v:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", v):
        return float(v)
    if re.fullmatch(r"\d+(:\d{1,2}){1,2}", v):
        secs = 0.0
        for part in v.split(":"):
            secs = secs * 60 + int(part)
        return secs
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", v)
    if m and any(m.groups()):
        h, mi, s = (int(g or 0) for g in m.groups())
        return float(h * 3600 + mi * 60 + s)
    raise ValueError(f"Can't read the start time {value!r}. Use 1:02:03 or 1h2m3s.")


def start_from_url(url: str) -> float | None:
    """Twitch/YouTube links carry the moment as ?t=1h2m3s (Twitch) or ?t=3723 (YouTube)."""
    m = re.search(r"[?&#]t=([0-9hms:.]+)", url)
    return parse_time(m.group(1)) if m else None


def download(url: str, out_dir: Path, video_id: str, cookies: Path | None = None,
             section: tuple[float, float] | None = None) -> Path:
    """Download a video. With section=(start, end) only that part of a long VOD is fetched."""
    import yt_dlp

    out_dir.mkdir(parents=True, exist_ok=True)
    opts = {
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080]/b",
        "merge_output_format": "mp4",
        "outtmpl": str(out_dir / f"{video_id}.%(ext)s"),
        "quiet": True,
        "noprogress": True,
    }
    if cookies:
        opts["cookiefile"] = str(cookies)
    if section:
        from yt_dlp.utils import download_range_func
        opts["download_ranges"] = download_range_func(None, [section])
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    path = out_dir / f"{video_id}.mp4"
    if not path.exists():
        matches_ = list(out_dir.glob(f"{video_id}.*"))
        if not matches_:
            raise FileNotFoundError(f"download produced no file for {video_id}")
        path = matches_[0]
    return path
