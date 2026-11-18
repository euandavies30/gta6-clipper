"""Whisper transcription -> word-by-word style ASS subtitles, plus post captions."""
from __future__ import annotations

import logging
import os
import random
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

FONT = os.getenv("CAPTION_FONT", "DejaVu Sans")


@lru_cache(maxsize=1)
def _model(name: str):
    from faster_whisper import WhisperModel

    return WhisperModel(name, device="auto", compute_type="int8")


def transcribe(audio: Path, model_name: str = "small") -> list[dict]:
    """Return a list of {start, end, word} relative to the clip start.

    Trailers are mostly music, so VAD is on and low-confidence speech is dropped
    to avoid Whisper inventing lyrics.
    """
    segments, _ = _model(model_name).transcribe(
        str(audio), word_timestamps=True, vad_filter=True, language="en",
        condition_on_previous_text=False,
    )
    words = []
    for seg in segments:
        if seg.no_speech_prob > 0.6 or seg.avg_logprob < -1.0:
            continue
        for w in seg.words or []:
            text = w.word.strip()
            if text:
                words.append({"start": float(w.start), "end": float(w.end), "word": text})
    return words


def chunk_words(words: list[dict], max_words: int = 3, max_gap: float = 0.6, max_len: float = 1.4) -> list[dict]:
    chunks, cur = [], []
    for w in words:
        if cur and (len(cur) >= max_words or w["start"] - cur[-1]["end"] > max_gap
                    or w["end"] - cur[0]["start"] > max_len):
            chunks.append(cur)
            cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return [{"start": c[0]["start"], "end": max(c[-1]["end"], c[0]["start"] + 0.4),
             "text": " ".join(x["word"] for x in c)} for c in chunks]


def _ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _esc(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def build_ass(chunks: list[dict], hook: str | None, duration: float, out: Path) -> Path:
    """Captions sit in the lower third; an optional hook banner sits at the top for the first seconds."""
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{FONT},86,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,6,2,2,80,80,520,1
Style: Hook,{FONT},70,&H0000E5FF,&H000000FF,&H00000000,&HAA000000,-1,0,0,0,100,100,0,0,3,4,0,8,60,60,230,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    if hook:
        lines.append(f"Dialogue: 1,{_ts(0)},{_ts(min(duration, 4.0))},Hook,,0,0,0,,{_esc(hook.upper())}")
    for c in chunks:
        if c["start"] >= duration:
            continue
        lines.append(
            f"Dialogue: 0,{_ts(c['start'])},{_ts(min(c['end'], duration))},Cap,,0,0,0,,"
            f"{{\\fad(60,40)}}{_esc(c['text'].upper())}"
        )
    out.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    return out


DEFAULT_HOOKS = [
    "Wait for it...",
    "GTA 6 detail you missed",
    "Vice City is BACK",
    "This looks unreal",
    "Rockstar cooked here",
]


def pick_hook(title: str | None) -> str:
    return random.choice(DEFAULT_HOOKS)


def post_caption(template: str, title: str | None, credit: str | None) -> str:
    """Caption body stored on the clip. Each account's hashtags are appended at post time."""
    text = template.format(title=title or "GTA 6", credit=f"🎥 {credit}" if credit else "")
    return "\n".join(line.rstrip() for line in text.strip().splitlines() if line.strip())


def with_hashtags(caption: str, hashtags: list[str], limit: int = 2200) -> str:
    tags = " ".join(h if h.startswith("#") else f"#{h}" for h in hashtags)
    full = f"{caption}\n\n{tags}".strip() if tags else caption
    return full[:limit]
