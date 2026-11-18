"""SQLite store: source videos, clips, and posts."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,              -- youtube | inbox
    external_id TEXT NOT NULL,       -- youtube video id or file name
    title TEXT,
    url TEXT,
    credit TEXT,                     -- who to credit in captions
    local_path TEXT,
    status TEXT NOT NULL DEFAULT 'new',  -- new | downloaded | cut | failed
    error TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(kind, external_id)
);
CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    start_s REAL NOT NULL,
    end_s REAL NOT NULL,
    score REAL,
    path TEXT,
    transcript TEXT,
    caption TEXT,                    -- post caption (editable in review UI)
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected | posted
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY,
    clip_id INTEGER NOT NULL REFERENCES clips(id),
    account TEXT NOT NULL,
    platform TEXT NOT NULL,
    status TEXT NOT NULL,            -- ok | failed | dry_run
    remote_id TEXT,
    error TEXT,
    posted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clips_status ON clips(status);
CREATE INDEX IF NOT EXISTS idx_posts_account ON posts(account, posted_at);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # sources -------------------------------------------------------------
    def add_source(self, kind, external_id, title=None, url=None, credit=None, local_path=None) -> int | None:
        """Insert a source; returns its id, or None if it was already known."""
        with self.conn() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO sources(kind, external_id, title, url, credit, local_path, status, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (kind, external_id, title, url, credit, local_path,
                 "downloaded" if local_path else "new", now_iso()),
            )
            return cur.lastrowid if cur.rowcount else None

    def sources_with_status(self, status: str):
        with self.conn() as c:
            return c.execute("SELECT * FROM sources WHERE status=? ORDER BY id", (status,)).fetchall()

    def update_source(self, source_id: int, **fields):
        self._update("sources", source_id, fields)

    def get_source(self, source_id: int):
        with self.conn() as c:
            return c.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()

    # clips ---------------------------------------------------------------
    def add_clip(self, source_id, start_s, end_s, score, path, transcript, caption, status) -> int:
        with self.conn() as c:
            cur = c.execute(
                "INSERT INTO clips(source_id, start_s, end_s, score, path, transcript, caption, status, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (source_id, start_s, end_s, score, str(path), transcript, caption, status, now_iso()),
            )
            return cur.lastrowid

    def clips(self, status: str | None = None, limit: int = 200):
        q = "SELECT clips.*, sources.title AS source_title, sources.url AS source_url FROM clips" \
            " JOIN sources ON sources.id = clips.source_id"
        args: tuple = ()
        if status:
            q += " WHERE clips.status=?"
            args = (status,)
        q += " ORDER BY clips.score DESC, clips.id LIMIT ?"
        with self.conn() as c:
            return c.execute(q, args + (limit,)).fetchall()

    def get_clip(self, clip_id: int):
        with self.conn() as c:
            return c.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()

    def update_clip(self, clip_id: int, **fields):
        self._update("clips", clip_id, fields)

    # posts ---------------------------------------------------------------
    def record_post(self, clip_id, account, platform, status, remote_id=None, error=None):
        with self.conn() as c:
            c.execute(
                "INSERT INTO posts(clip_id, account, platform, status, remote_id, error, posted_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (clip_id, account, platform, status, remote_id, error, now_iso()),
            )

    def posts_for_account_since(self, account: str, since_iso: str):
        with self.conn() as c:
            return c.execute(
                "SELECT * FROM posts WHERE account=? AND posted_at>=? AND status IN ('ok','dry_run')"
                " ORDER BY posted_at",
                (account, since_iso),
            ).fetchall()

    def last_attempt(self, account: str):
        with self.conn() as c:
            return c.execute(
                "SELECT * FROM posts WHERE account=? ORDER BY posted_at DESC LIMIT 1", (account,)
            ).fetchone()

    def next_clip_for(self, account: str, platform: str):
        """Best approved clip not yet posted on this platform by any account.

        One clip goes to at most one account per platform, so two accounts on
        the same platform never post identical videos (a duplicate-content flag).
        """
        with self.conn() as c:
            return c.execute(
                """
                SELECT * FROM clips WHERE status IN ('approved','posted')
                AND id NOT IN (SELECT clip_id FROM posts WHERE platform=? AND status IN ('ok','dry_run'))
                AND id NOT IN (SELECT clip_id FROM posts WHERE account=? AND status='failed'
                               GROUP BY clip_id HAVING COUNT(*) >= 3)
                ORDER BY score DESC, id LIMIT 1
                """,
                (platform, account),
            ).fetchone()

    def recent_posts(self, limit: int = 50):
        with self.conn() as c:
            return c.execute("SELECT * FROM posts ORDER BY posted_at DESC LIMIT ?", (limit,)).fetchall()

    def _update(self, table: str, row_id: int, fields: dict):
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.conn() as c:
            c.execute(f"UPDATE {table} SET {cols} WHERE id=?", (*fields.values(), row_id))
