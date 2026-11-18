"""TikTok Content Posting API (Direct Post, FILE_UPLOAD).

Until TikTok audits the app, every post must be SELF_ONLY (private). Once the
app passes the audit, set TIKTOK_PRIVACY=PUBLIC_TO_EVERYONE.

Env (app-level, shared by all TikTok accounts):
  TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET, TIKTOK_PRIVACY (default SELF_ONLY)
Per account (names set in accounts.yaml `env:`):
  refresh_token  -> e.g. TIKTOK_MAIN_REFRESH_TOKEN (or use /oauth/tiktok/start in the review app)
"""
from __future__ import annotations

import logging
import math
import os
import time
from pathlib import Path

import requests

from .base import Poster, PostError, PostResult, TokenStore

log = logging.getLogger(__name__)
API = "https://open.tiktokapis.com/v2"
AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
SCOPES = "user.info.basic,video.publish,video.upload"
MB = 1024 * 1024


def exchange_code(code: str, redirect_uri: str) -> dict:
    r = requests.post(f"{API}/oauth/token/", data={
        "client_key": os.environ["TIKTOK_CLIENT_KEY"],
        "client_secret": os.environ["TIKTOK_CLIENT_SECRET"],
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }, timeout=30)
    data = r.json()
    if "access_token" not in data:
        raise PostError(f"TikTok token exchange failed: {data}")
    return data


class TikTokPoster(Poster):
    name = "tiktok"

    def __init__(self, account, settings):
        super().__init__(account, settings)
        self.tokens = TokenStore(settings, account)

    # auth ------------------------------------------------------------------
    def _access_token(self) -> str:
        tok = self.tokens.load()
        if tok.get("access_token") and time.time() < tok.get("saved_at", 0) + tok.get("expires_in", 0) - 300:
            return tok["access_token"]
        refresh = tok.get("refresh_token") or self.account.secret("refresh_token")
        if not refresh:
            raise PostError(f"{self.account.name}: no TikTok refresh token; connect the account first")
        r = requests.post(f"{API}/oauth/token/", data={
            "client_key": os.environ["TIKTOK_CLIENT_KEY"],
            "client_secret": os.environ["TIKTOK_CLIENT_SECRET"],
            "grant_type": "refresh_token",
            "refresh_token": refresh,
        }, timeout=30)
        data = r.json()
        if "access_token" not in data:
            raise PostError(f"TikTok token refresh failed: {data}")
        self.tokens.save(data)
        return data["access_token"]

    def _api(self, path: str, token: str, body: dict) -> dict:
        r = requests.post(f"{API}{path}", json=body, timeout=60, headers={
            "Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"})
        data = r.json()
        err = data.get("error", {})
        if r.status_code >= 400 or (err.get("code") not in (None, "ok")):
            raise PostError(f"TikTok {path} failed ({r.status_code}): {err or data}")
        return data.get("data", {})

    # posting -----------------------------------------------------------------
    def post(self, video: Path, caption: str) -> PostResult:
        token = self._access_token()
        creator = self._api("/post/publish/creator_info/query/", token, {})
        options = creator.get("privacy_level_options") or ["SELF_ONLY"]
        wanted = os.getenv("TIKTOK_PRIVACY", "SELF_ONLY")
        privacy = wanted if wanted in options else ("SELF_ONLY" if "SELF_ONLY" in options else options[0])

        size = video.stat().st_size
        if size <= 64 * MB:
            chunk, count = size, 1
        else:
            chunk = 10 * MB
            count = math.floor(size / chunk)  # last chunk absorbs the remainder (allowed up to 128MB)

        init = self._api("/post/publish/video/init/", token, {
            "post_info": {
                "title": caption[:2200],
                "privacy_level": privacy,
                "disable_duet": False,
                "disable_comment": False,
                "disable_stitch": False,
                "video_cover_timestamp_ms": 1000,
            },
            "source_info": {"source": "FILE_UPLOAD", "video_size": size,
                            "chunk_size": chunk, "total_chunk_count": count},
        })
        publish_id, upload_url = init["publish_id"], init["upload_url"]

        with video.open("rb") as f:
            for i in range(count):
                start = i * chunk
                end = size - 1 if i == count - 1 else start + chunk - 1
                f.seek(start)
                body = f.read(end - start + 1)
                r = requests.put(upload_url, data=body, timeout=300, headers={
                    "Content-Type": "video/mp4",
                    "Content-Length": str(len(body)),
                    "Content-Range": f"bytes {start}-{end}/{size}",
                })
                if r.status_code not in (200, 201, 206):
                    raise PostError(f"TikTok chunk {i} upload failed: {r.status_code} {r.text[:300]}")

        status = self._wait(token, publish_id)
        return PostResult(remote_id=publish_id, note=f"{status} ({privacy})")

    def _wait(self, token: str, publish_id: str, timeout: int = 300) -> str:
        deadline = time.time() + timeout
        status = "PROCESSING_UPLOAD"
        while time.time() < deadline:
            data = self._api("/post/publish/status/fetch/", token, {"publish_id": publish_id})
            status = data.get("status", status)
            if status == "FAILED":
                raise PostError(f"TikTok publish failed: {data.get('fail_reason')}")
            if status in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"):
                return status
            time.sleep(5)
        return status  # still processing; TikTok will finish it on its side
