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


VOD_RE = re.compile(r"https?://(?:www\.|m\.)?twitch\.tv/videos/(\d+)")


def is_vod(url: str) -> bool:
    return bool(VOD_RE.match(url or ""))


def vod_info(url: str) -> dict:
    """Title, channel, length and the audio-only stream URL of a past broadcast."""
    import yt_dlp

    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "format": "audio_only/worstaudio/worst"}) as ydl:
        info = ydl.extract_info(url, download=False)
    return {"title": info.get("title"), "channel": info.get("channel") or info.get("uploader"),
            "duration": float(info.get("duration") or 0), "audio_url": info.get("url")}


def stream_loudness(media_url: str, sr: int = 8000, step: float = 1.0):
    """Loudness (dB-ish) per `step` seconds of a long stream, read in small chunks.

    A 6-hour VOD would be ~350 MB of PCM in memory, too much for a 1 GB box, so ffmpeg
    streams it and we only keep one number per second.
    """
    import subprocess

    import numpy as np

    hop = int(sr * step) * 2  # bytes per step (16-bit mono)
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-threads", "1", "-i", media_url, "-vn", "-ac", "1",
                             "-ar", str(sr), "-f", "s16le", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    out = []
    buf = b""
    try:
        while True:
            chunk = proc.stdout.read(hop * 60)
            if not chunk:
                break
            buf += chunk
            while len(buf) >= hop:
                pcm = np.frombuffer(buf[:hop], dtype=np.int16).astype(np.float32)
                buf = buf[hop:]
                out.append(20 * np.log10(np.sqrt(np.mean(pcm ** 2)) + 1.0))
    finally:
        proc.stdout.close()
        proc.wait()
    return np.array(out)


def pick_peaks(loud, count: int, length: float, step: float = 1.0, skip_start: float = 300.0,
               min_gap: float = 180.0, lead_in: float = 40.0) -> list[float]:
    """Start times of the `count` loudest reactions (streamer yelling, big moments), spaced apart.

    Loudness is smoothed over ~6s so a single bang doesn't win, and each window starts
    `lead_in` seconds before the peak so the setup is included.
    """
    import numpy as np

    loud = np.asarray(loud, dtype=float)
    if loud.size == 0:
        return []
    k = max(1, int(6 / step))
    smooth = np.convolve(loud, np.ones(k) / k, mode="same")
    order = np.argsort(smooth)[::-1]
    peaks: list[float] = []
    end = loud.size * step
    for i in order:
        t = i * step
        if t < skip_start or t > end - 10:
            continue
        if any(abs(t - p) < min_gap for p in peaks):
            continue
        peaks.append(t)
        if len(peaks) >= count:
            break
    return sorted(max(0.0, p - lead_in) for p in peaks)
