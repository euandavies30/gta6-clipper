"""Pick highlight segments from a long video and render them as 9:16 clips.

Highlights are scored from two cheap signals ffmpeg can give us:
  * audio loudness (action, music drops, dialogue punches)
  * scene-cut density (fast editing = trailer money shots)
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

W, H = 1080, 1920
STEP = 0.5  # seconds per analysis bin
SKIP_EDGES = 3.0  # ignore logos/end cards at the very start and end


@dataclass
class Segment:
    start: float
    end: float
    score: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    log.debug("run: %s", " ".join(cmd))
    return subprocess.run(cmd, check=True, capture_output=True, **kw)


def probe_duration(path: Path) -> float:
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)])
    return float(json.loads(out.stdout)["format"]["duration"])


def loudness_curve(path: Path, duration: float) -> np.ndarray:
    sr = 8000
    try:
        out = run(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"])
    except subprocess.CalledProcessError:
        return np.zeros(int(np.ceil(duration / STEP)))
    pcm = np.frombuffer(out.stdout, dtype=np.int16).astype(np.float32)
    n = int(np.ceil(duration / STEP))
    hop = int(sr * STEP)
    rms = np.zeros(n)
    for i in range(n):
        chunk = pcm[i * hop:(i + 1) * hop]
        if chunk.size:
            rms[i] = np.sqrt(np.mean(chunk ** 2))
    return 20 * np.log10(rms + 1.0)  # dB-ish


def scene_cuts(path: Path, threshold: float = 0.3) -> list[float]:
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-an",
         "-vf", f"scale=320:-2,select='gt(scene,{threshold})',showinfo", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    return [float(m) for m in re.findall(r"pts_time:([0-9.]+)", proc.stderr)]


def _z(x: np.ndarray) -> np.ndarray:
    sd = x.std()
    return (x - x.mean()) / sd if sd > 1e-6 else np.zeros_like(x)


def find_highlights(path: Path, count: int, min_len: float, max_len: float) -> list[Segment]:
    duration = probe_duration(path)
    if duration < min_len:
        return []
    if duration <= max_len + 2 * SKIP_EDGES:
        return [Segment(0.0, duration, 1.0)]

    loud = loudness_curve(path, duration)
    cuts = scene_cuts(path)
    n = len(loud)
    cut_density = np.zeros(n)
    for t in cuts:
        i = int(t / STEP)
        if 0 <= i < n:
            cut_density[i] += 1
    per_bin = _z(loud) + 0.6 * _z(cut_density)

    target = float(np.clip((min_len + max_len) / 2, min_len, max_len))
    win = int(target / STEP)
    lo, hi = int(SKIP_EDGES / STEP), n - int(SKIP_EDGES / STEP) - win
    if hi <= lo:
        return [Segment(0.0, min(duration, max_len), 1.0)]
    csum = np.concatenate([[0.0], np.cumsum(per_bin)])
    scores = np.array([(csum[i + win] - csum[i]) / win for i in range(lo, hi)])

    chosen: list[Segment] = []
    for idx in np.argsort(scores)[::-1]:
        start = (lo + idx) * STEP
        end = start + target
        if any(not (end + 2 <= s.start or start >= s.end + 2) for s in chosen):
            continue
        start = _snap_to_cut(start, cuts)
        end = min(duration - 0.1, _snap_to_cut(end, cuts, before=False) if cuts else end)
        if end - start < min_len:
            end = start + min_len
        if end - start > max_len:
            end = start + max_len
        chosen.append(Segment(round(start, 2), round(end, 2), float(scores[idx])))
        if len(chosen) >= count:
            break
    return sorted(chosen, key=lambda s: s.start)


def _snap_to_cut(t: float, cuts: list[float], before: bool = True, window: float = 2.0) -> float:
    """Move t to a nearby scene cut so clips start/end on a clean edit."""
    near = [c for c in cuts if (t - window <= c <= t if before else t <= c <= t + window)]
    if not near:
        return t
    return max(near) if before else min(near)


def extract_audio(src: Path, start: float, end: float, out: Path) -> Path:
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start}", "-i", str(src), "-t", f"{end - start}",
         "-vn", "-ac", "1", "-ar", "16000", str(out)])
    return out


def render_vertical(src: Path, start: float, end: float, out: Path, ass_file: Path | None = None) -> Path:
    """Blurred-background 9:16 render with the original frame centered, captions burned in."""
    out.parent.mkdir(parents=True, exist_ok=True)
    graph = (
        f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
        f"gblur=sigma=40,eq=brightness=-0.08[bg];"
        f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,fps=30,format=yuv420p"
    )
    if ass_file:
        graph += f",subtitles='{_ff_escape(ass_file)}'"
    graph += "[v]"
    run([
        "ffmpeg", "-y", "-v", "error", "-ss", f"{start}", "-i", str(src), "-t", f"{end - start}",
        "-filter_complex", graph, "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-movflags", "+faststart", str(out),
    ])
    return out


def _ff_escape(p: Path) -> str:
    return str(p).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
