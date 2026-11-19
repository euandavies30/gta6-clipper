"""Settings (from env vars) and account config (from config/accounts.yaml).

Secrets only ever come from environment variables. accounts.yaml names the
env vars to read for each account, so the YAML file is safe to commit.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", ROOT / "data")))
    accounts_file: Path = field(
        default_factory=lambda: Path(os.getenv("ACCOUNTS_FILE", ROOT / "config" / "accounts.yaml"))
    )
    # Review mode: new clips wait for a human to approve them before posting.
    review_mode: bool = field(default_factory=lambda: _bool("REVIEW_MODE", True))
    # Dry run: posters log what they would do instead of calling any API.
    dry_run: bool = field(default_factory=lambda: _bool("DRY_RUN", True))
    youtube_api_key: str | None = field(default_factory=lambda: os.getenv("YOUTUBE_API_KEY"))
    whisper_model: str = field(default_factory=lambda: os.getenv("WHISPER_MODEL", "small"))
    review_user: str = field(default_factory=lambda: os.getenv("REVIEW_USER", "admin"))
    review_password: str | None = field(default_factory=lambda: os.getenv("REVIEW_PASSWORD"))
    public_base_url: str | None = field(default_factory=lambda: os.getenv("PUBLIC_BASE_URL"))
    ingest_every_hours: float = field(default_factory=lambda: float(os.getenv("INGEST_EVERY_HOURS", "6")))
    tick_seconds: int = field(default_factory=lambda: int(os.getenv("TICK_SECONDS", "300")))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "clipper.db"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def clips_dir(self) -> Path:
        return self.data_dir / "clips"

    @property
    def inbox_dir(self) -> Path:
        return self.data_dir / "inbox"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.raw_dir, self.clips_dir, self.inbox_dir):
            d.mkdir(parents=True, exist_ok=True)
        # Half-finished downloads (e.g. after a full disk) only waste the volume.
        for junk in list(self.raw_dir.glob("*.part")) + list(self.raw_dir.glob("*.ytdl")):
            junk.unlink(missing_ok=True)


@dataclass
class Account:
    name: str
    platform: str  # "tiktok" | "instagram"
    poster: str  # "tiktok" | "instagram" | "webhook" | "dryrun"
    posts_per_day: int = 4
    active_hours: tuple[int, int] = (9, 23)  # local hours, [start, end)
    timezone: str = "Europe/London"
    min_gap_minutes: int = 90
    hashtags: list[str] = field(default_factory=list)
    enabled: bool = True
    env: dict[str, str] = field(default_factory=dict)  # logical key -> ENV VAR NAME

    def secret(self, key: str) -> str | None:
        var = self.env.get(key)
        return os.getenv(var) if var else None


@dataclass
class SourceConfig:
    rockstar_media: bool = True  # official downloads from rockstargames.com/VI/media/videos
    youtube_channels: list[str] = field(default_factory=list)  # handles like "@RockstarGames"
    title_keywords: list[str] = field(default_factory=list)
    max_videos_per_ingest: int = 5
    clips_per_video: int = 6
    clip_min_seconds: int = 12
    clip_max_seconds: int = 35


@dataclass
class AppConfig:
    accounts: list[Account]
    sources: SourceConfig
    caption_templates: list[str]


def load_app_config(path: Path | None = None) -> AppConfig:
    path = path or Settings().accounts_file
    raw = yaml.safe_load(Path(path).read_text()) or {}
    accounts = []
    for a in raw.get("accounts", []):
        hours = a.pop("active_hours", [9, 23])
        accounts.append(Account(active_hours=(int(hours[0]), int(hours[1])), **a))
    names = [a.name for a in accounts]
    if len(names) != len(set(names)):
        raise ValueError("account names in accounts.yaml must be unique")
    for a in accounts:
        if not 1 <= a.posts_per_day <= 10:
            raise ValueError(f"{a.name}: posts_per_day must be 1-10 (keep it low to avoid spam flags)")
    return AppConfig(
        accounts=accounts,
        sources=SourceConfig(**raw.get("sources", {})),
        caption_templates=raw.get("caption_templates") or ["{title}"],
    )
