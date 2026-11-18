"""Instagram Reels via the Instagram Graph API.

Works with "Instagram API with Instagram Login" (graph.instagram.com, no
Facebook Page needed) or the Facebook Login flavour (graph.facebook.com).
The account must be a Professional (Creator or Business) account.

Env (app-level): IG_GRAPH_HOST (default graph.instagram.com), IG_API_VERSION (default v21.0),
  IG_UPLOAD_MODE = url | resumable (default: url when PUBLIC_BASE_URL is set)
Per account (names in accounts.yaml `env:`):
  user_id       -> e.g. IG_MAIN_USER_ID
  access_token  -> e.g. IG_MAIN_ACCESS_TOKEN (long-lived, refreshed automatically)
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import requests

from .base import Poster, PostError, PostResult, TokenStore

log = logging.getLogger(__name__)
REFRESH_AFTER_DAYS = 30


class InstagramPoster(Poster):
    name = "instagram"

    def __init__(self, account, settings):
        super().__init__(account, settings)
        self.host = os.getenv("IG_GRAPH_HOST", "graph.instagram.com")
        self.version = os.getenv("IG_API_VERSION", "v21.0")
        self.base = f"https://{self.host}/{self.version}"
        self.tokens = TokenStore(settings, account)

    def _token(self) -> str:
        tok = self.tokens.load()
        access = tok.get("access_token") or self.account.secret("access_token")
        if not access:
            raise PostError(f"{self.account.name}: no Instagram access token configured")
        age_days = (time.time() - tok.get("saved_at", 0)) / 86400
        if self.host == "graph.instagram.com" and (not tok or age_days > REFRESH_AFTER_DAYS):
            r = requests.get(f"https://{self.host}/refresh_access_token",
                             params={"grant_type": "ig_refresh_token", "access_token": access}, timeout=30)
            if r.ok and "access_token" in r.json():
                self.tokens.save(r.json())
                access = r.json()["access_token"]
            else:
                log.warning("%s: IG token refresh failed: %s", self.account.name, r.text[:200])
        return access

    def _user_id(self) -> str:
        uid = self.account.secret("user_id")
        if not uid:
            raise PostError(f"{self.account.name}: no Instagram user id configured")
        return uid

    def _check(self, r: requests.Response) -> dict:
        data = r.json() if r.content else {}
        if r.status_code >= 400 or "error" in data:
            raise PostError(f"Instagram API error ({r.status_code}): {data.get('error', data)}")
        return data

    def post(self, video: Path, caption: str) -> PostResult:
        token, uid = self._token(), self._user_id()
        mode = os.getenv("IG_UPLOAD_MODE") or ("url" if self.settings.public_base_url else "resumable")
        params = {"media_type": "REELS", "caption": caption[:2200], "share_to_feed": "true",
                  "access_token": token}
        if mode == "url":
            params["video_url"] = self.public_url(video)
            container = self._check(requests.post(f"{self.base}/{uid}/media", data=params, timeout=60))["id"]
        else:
            params["upload_type"] = "resumable"
            created = self._check(requests.post(f"{self.base}/{uid}/media", data=params, timeout=60))
            container = created["id"]
            upload_uri = created.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{self.version}/{container}"
            size = video.stat().st_size
            with video.open("rb") as f:
                self._check(requests.post(upload_uri, data=f, timeout=600, headers={
                    "Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size)}))

        self._wait(container, token)
        published = self._check(requests.post(f"{self.base}/{uid}/media_publish",
                                              data={"creation_id": container, "access_token": token}, timeout=60))
        return PostResult(remote_id=published.get("id"), note="published")

    def _wait(self, container: str, token: str, timeout: int = 600) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            data = self._check(requests.get(f"{self.base}/{container}",
                                            params={"fields": "status_code,status", "access_token": token},
                                            timeout=30))
            code = data.get("status_code")
            if code == "FINISHED":
                return
            if code in ("ERROR", "EXPIRED"):
                raise PostError(f"Instagram processing failed: {data.get('status')}")
            time.sleep(10)
        raise PostError("Instagram processing timed out")
