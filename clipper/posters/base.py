"""Poster interface. Every platform/scheduler integration implements `post`."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from ..config import Account, Settings

log = logging.getLogger(__name__)


class PostError(Exception):
    pass


@dataclass
class PostResult:
    remote_id: str | None
    note: str = ""


class Poster:
    name = "base"

    def __init__(self, account: Account, settings: Settings):
        self.account = account
        self.settings = settings

    def post(self, video: Path, caption: str) -> PostResult:  # pragma: no cover - interface
        raise NotImplementedError

    def public_url(self, video: Path) -> str:
        """URL where platforms can fetch the clip (served by the review app at /media/)."""
        if not self.settings.public_base_url:
            raise PostError("PUBLIC_BASE_URL is not set, so platforms can't fetch the video by URL")
        return f"{self.settings.public_base_url.rstrip('/')}/media/{video.name}"


class TokenStore:
    """Per-account OAuth tokens in DATA_DIR/tokens/<account>.json (seeded from env on first use)."""

    def __init__(self, settings: Settings, account: Account):
        self.path = settings.data_dir / "tokens" / f"{account.name}.json"

    def load(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text())
        return {}

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {**data, "saved_at": time.time()}
        self.path.write_text(json.dumps(data, indent=2))
        try:
            self.path.chmod(0o600)
        except OSError:
            pass


class DryRunPoster(Poster):
    name = "dryrun"

    def post(self, video: Path, caption: str) -> PostResult:
        log.info("[dry run] %s (%s) would post %s\n%s", self.account.name, self.account.platform, video.name, caption)
        return PostResult(remote_id=None, note="dry run")
