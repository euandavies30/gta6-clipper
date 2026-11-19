"""Hand the post to a scheduler instead of calling TikTok/Instagram directly.

Sends JSON to a webhook (Make, Zapier, n8n, or a Buffer/Metricool/Later
automation): {account, platform, caption, video_url, file_name}.
That route skips the TikTok audit and Meta app review entirely.

Per account (names in accounts.yaml `env:`): url -> e.g. WEBHOOK_TT_MAIN_URL
Optional shared secret header: WEBHOOK_SECRET (sent as X-Clipper-Secret).
"""
from __future__ import annotations

import os
from pathlib import Path

import requests

from .base import Poster, PostError, PostResult


class WebhookPoster(Poster):
    name = "webhook"

    def post(self, video: Path, caption: str, **options) -> PostResult:
        url = self.account.secret("url")
        if not url:
            raise PostError(f"{self.account.name}: no webhook url configured")
        headers = {}
        if os.getenv("WEBHOOK_SECRET"):
            headers["X-Clipper-Secret"] = os.environ["WEBHOOK_SECRET"]
        r = requests.post(url, timeout=60, headers=headers, json={
            "account": self.account.name,
            "platform": self.account.platform,
            "caption": caption,
            "video_url": self.public_url(video),
            "file_name": video.name,
        })
        if r.status_code >= 400:
            raise PostError(f"webhook returned {r.status_code}: {r.text[:300]}")
        return PostResult(remote_id=None, note=f"webhook {r.status_code}")
