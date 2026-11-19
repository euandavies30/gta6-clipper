"""Turn a short Twitch clip into a ~1 minute moment from the streamer's own VOD.

Twitch clips are 5-60s, and TikTok only pays for videos over a minute. A clip's metadata
says when it was made, and the channel's past broadcasts say when each stream started,
so the clip's place in the VOD is (clip created - stream start). We take a window around it.
Everything goes through yt-dlp's normal Twitch extractors.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

CLIP_RE = re.compile(r"https?://(?:clips\.twitch\.tv/|(?:www\.|m\.)?twitch\.tv/[^/]+/clip/)([\w-]+)")
LEAD_IN = 0.65  # share of the extra time placed before the clip, so the setup is included


@dataclass
class Moment:
    vod_url: str
    start: float
    title: str | None
    channel: str | None


def is_clip(url: str) -> bool:
    return bool(CLIP_RE.match(url or ""))


def window(clip_ts: float, clip_dur: float, vod_ts: float, length: float) -> float:
    """Start of a `length`-second window in the VOD that contains the clip with some lead-in."""
    clip_end = clip_ts - vod_ts
    clip_start = clip_end - clip_dur
    extra = max(0.0, length - clip_dur)
    return max(0.0, clip_start - extra * LEAD_IN)


def find_moment(url: str, length: float, max_vods: int = 8) -> Moment | None:
    """Return the VOD link and start time for a clip, or None if its broadcast isn't found."""
    import yt_dlp

    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
        clip = ydl.extract_info(url, download=False, process=False)
        ts, dur = clip.get("timestamp"), float(clip.get("duration") or 30)
        channel = clip.get("channel")
        if not ts or not channel:
            return None
        listing = ydl.extract_info(f"https://www.twitch.tv/{channel.lower()}/videos?filter=archives&sort=time",
                                   download=False, process=False)
        for i, entry in enumerate(listing.get("entries") or []):
            if i >= max_vods:
                break
            vod_url = entry.get("url") or entry.get("webpage_url")
            if not vod_url:
                continue
            vod = ydl.extract_info(vod_url, download=False, process=False)
            vts, vdur = vod.get("timestamp"), vod.get("duration") or 0
            if vts and vts <= ts <= vts + vdur + 900:
                start = window(ts, dur, vts, length)
                log.info("clip %s is at %.0fs of %s", url, start, vod_url)
                return Moment(vod_url, start, clip.get("title"), channel)
            if vts and vts + vdur + 900 < ts:
                break  # older broadcasts can't contain it
    return None
