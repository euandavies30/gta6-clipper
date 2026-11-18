"""Decides when each account posts and sends the next approved clip.

Each account gets `posts_per_day` slots spread across its active hours, with
random jitter that stays the same for a given account and day (so restarts
don't reshuffle). On every tick, an account posts if a slot has passed that it
hasn't used yet and its last post is older than `min_gap_minutes`.
"""
from __future__ import annotations

import hashlib
import logging
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .captions import with_hashtags
from .config import Account, AppConfig, Settings
from .db import DB
from .posters import PostError, get_poster

log = logging.getLogger(__name__)
FAILED_RETRY_MINUTES = 20


def daily_slots(account: Account, day: datetime) -> list[datetime]:
    tz = ZoneInfo(account.timezone)
    start_h, end_h = account.active_hours
    if end_h <= start_h:
        end_h += 24
    day_start = datetime(day.year, day.month, day.day, tzinfo=tz)
    window = (end_h - start_h) * 60
    n = account.posts_per_day
    seed = int(hashlib.sha256(f"{account.name}:{day.date()}".encode()).hexdigest(), 16)
    rng = random.Random(seed)
    step = window / n
    slots = []
    for i in range(n):
        jitter = rng.uniform(0.25, 0.75) * step
        slots.append(day_start + timedelta(hours=start_h, minutes=i * step + jitter))
    return slots


def is_due(account: Account, db: DB, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    local_now = now.astimezone(ZoneInfo(account.timezone))
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    passed = sum(1 for s in daily_slots(account, local_now) if s <= local_now)
    posted = db.posts_for_account_since(account.name, local_midnight.astimezone(timezone.utc).isoformat())
    if len(posted) >= passed:
        return False
    last = db.last_attempt(account.name)
    if last:
        last_at = datetime.fromisoformat(last["posted_at"])
        gap = FAILED_RETRY_MINUTES if last["status"] == "failed" else account.min_gap_minutes
        if now - last_at < timedelta(minutes=gap):
            return False
    return True


def tick(settings: Settings, app: AppConfig, db: DB, now: datetime | None = None) -> list[str]:
    """Post at most one clip per due account. Returns a log of what happened."""
    events = []
    for account in app.accounts:
        if not account.enabled or not is_due(account, db, now):
            continue
        clip = db.next_clip_for(account.name, account.platform)
        if clip is None:
            events.append(f"{account.name}: due but no approved clips left")
            continue
        caption = with_hashtags(clip["caption"] or "", account.hashtags)
        poster = get_poster(account, settings)
        try:
            result = poster.post(Path(clip["path"]), caption)
            status = "dry_run" if poster.name == "dryrun" else "ok"
            db.record_post(clip["id"], account.name, account.platform, status, result.remote_id)
            db.update_clip(clip["id"], status="posted")
            events.append(f"{account.name}: posted clip {clip['id']} ({result.note})")
        except (PostError, Exception) as e:  # one account failing must not stop the others
            log.exception("post failed for %s", account.name)
            db.record_post(clip["id"], account.name, account.platform, "failed", error=str(e)[:1000])
            events.append(f"{account.name}: FAILED clip {clip['id']}: {e}")
    for e in events:
        log.info(e)
    return events
