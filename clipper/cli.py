"""Command line entry point: `python -m clipper <command>`."""
from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path

from . import pipeline, scheduler
from .config import Settings, load_app_config
from .db import DB


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="clipper", description="GTA 6 short-clip pipeline")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ingest", help="discover, download, cut and caption new videos")
    a = sub.add_parser("add", help="clip a local video file now")
    a.add_argument("file", type=Path)
    a.add_argument("--title")
    a.add_argument("--credit")
    a.add_argument("--no-captions", action="store_true")
    sub.add_parser("tick", help="post to every account that is due (run every few minutes)")
    sub.add_parser("schedule", help="show today's posting slots per account")
    sub.add_parser("status", help="counts of clips and recent posts")
    ap = sub.add_parser("approve", help="approve clips by id (or 'all' pending)")
    ap.add_argument("ids", nargs="+")
    s = sub.add_parser("serve", help="run the review web app and background worker")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=int(__import__("os").getenv("PORT", "8000")))

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings()
    settings.ensure_dirs()

    if args.cmd == "serve":
        import uvicorn
        uvicorn.run("clipper.review.app:app", host=args.host, port=args.port)
        return

    app = load_app_config(settings.accounts_file)
    db = DB(settings.db_path)

    if args.cmd == "ingest":
        print(pipeline.ingest(settings, app, db))
    elif args.cmd == "add":
        src_id = db.add_source("inbox", f"{args.file.name}:{args.file.stat().st_mtime_ns}",
                               title=args.title or args.file.stem, credit=args.credit,
                               local_path=str(args.file.resolve()))
        if src_id is None:
            print("already added")
            return
        ids = pipeline.cut_source(settings, app, db, db.get_source(src_id), not args.no_captions)
        db.update_source(src_id, status="cut")
        print(f"made clips: {ids}")
    elif args.cmd == "tick":
        for e in scheduler.tick(settings, app, db) or ["nothing due"]:
            print(e)
    elif args.cmd == "schedule":
        now = datetime.now(timezone.utc)
        for acc in app.accounts:
            from zoneinfo import ZoneInfo
            local = now.astimezone(ZoneInfo(acc.timezone))
            slots = ", ".join(s.strftime("%H:%M") for s in scheduler.daily_slots(acc, local))
            print(f"{acc.name:<14} {acc.platform:<10} {acc.poster:<9} {slots} ({acc.timezone})")
    elif args.cmd == "status":
        for st in ("pending", "approved", "posted", "rejected"):
            print(f"{st:<9} {len(db.clips(st, limit=100000))}")
        for row in db.recent_posts(10):
            print(f"{row['posted_at']} {row['account']} clip#{row['clip_id']} {row['status']} {row['error'] or ''}")
    elif args.cmd == "approve":
        ids = [c["id"] for c in db.clips("pending", 100000)] if args.ids == ["all"] else [int(i) for i in args.ids]
        for i in ids:
            db.update_clip(i, status="approved")
        print(f"approved {len(ids)} clips")


if __name__ == "__main__":
    main()
