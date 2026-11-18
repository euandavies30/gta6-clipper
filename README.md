# GTA 6 Clipper

Finds GTA 6 videos you're allowed to use, cuts them into vertical 9:16 clips
with burned-in captions, queues them for review, then posts 3 to 6 times a day
to each TikTok and Instagram account.

```
sources ──> download ──> highlight picker ──> Whisper captions ──> 1080x1920 render
(YouTube API,                (loudness +            (word-by-word,       (blurred bg,
 inbox folder)                scene cuts)            hook banner)         centered frame)
                                                                              │
                     review app (approve / edit caption / reject) <───────────┘
                                         │
             scheduler: per-account slots, min gap, one clip per platform
                                         │
              poster: tiktok | instagram | webhook (Buffer/Make/Zapier) | dryrun
```

## Quick start (local)

```bash
pip install -r requirements.txt          # also needs ffmpeg on PATH
cp .env.example .env                     # fill in what you have; DRY_RUN=true is safe
python -m clipper add ~/Videos/trailer.mp4 --title "GTA 6 Trailer 2" --credit "Rockstar Games"
REVIEW_PASSWORD=pick-one python -m clipper serve   # open http://localhost:8000
```

Other commands: `ingest` (pull new videos from sources), `tick` (post whatever
is due), `schedule` (today's slots), `status`, `approve <ids|all>`.

## How it decides what and when to post

* **Clips:** each source video is scored every 0.5s on loudness and scene-cut
  density; the best non-overlapping 12 to 35s windows become clips, snapped to
  clean cuts. Tune in `config/accounts.yaml` under `sources`.
* **Review mode** (`REVIEW_MODE=true`): new clips sit in *Pending* until you
  approve them. Set it to `false` after the first week to auto-approve.
* **Schedule:** each account gets `posts_per_day` slots spread over its
  `active_hours`, with stable random jitter, and never posts within
  `min_gap_minutes` of its last post. Failed posts retry after 20 minutes, up
  to 3 times per clip.
* **No duplicates:** a clip goes to at most one account per platform, so your
  two TikToks never post the same video. The same clip can go to one TikTok
  and one Instagram.
* **Captions:** clip captions come from `caption_templates`; each account's
  `hashtags` are added at post time so the accounts don't look identical.

## Sources

* **YouTube** (`YOUTUBE_API_KEY`): new uploads from `youtube_channels` whose
  titles match `title_keywords`. Downloaded with yt-dlp.
* **Inbox:** drop files into `$DATA_DIR/inbox/`, named `Credit - Title.mp4`.
  Use this for streamer clips you have permission to post.

Rockstar's trailers are still Rockstar's copyright. Accounts that repost
them can get takedowns or strikes, so the captions and hooks are there to make
each clip more than a straight repost. Only add channels you're comfortable
with.

## Connecting accounts

Credentials live only in environment variables; `accounts.yaml` just names
which variable each account uses.

**TikTok** (Content Posting API)
1. Create an app at developers.tiktok.com, add *Login Kit* and *Content Posting API*
   (Direct Post), scopes `user.info.basic, video.publish, video.upload`.
2. Set the redirect URI to `$PUBLIC_BASE_URL/oauth/tiktok/callback`.
3. Set `TIKTOK_CLIENT_KEY` / `TIKTOK_CLIENT_SECRET`, deploy, then open
   `/oauth/tiktok/start?account=tt_main` (and `tt_second`) while logged into
   each TikTok account.
4. Until TikTok audits the app, posts are **private (SELF_ONLY)**. After the
   audit, set `TIKTOK_PRIVACY=PUBLIC_TO_EVERYONE`.

**Instagram** (Graph API, Reels)
1. Switch each account to a Professional (Creator) account.
2. Create a Meta app with *Instagram API with Instagram Login*, add both
   accounts as testers, and generate a long-lived token for each.
3. Set `IG_MAIN_USER_ID` / `IG_MAIN_ACCESS_TOKEN` (and `_SECOND_`). Tokens
   refresh automatically every 30 days.

**Simpler route:** set an account's `poster: webhook` and point its URL at a
Make/Zapier/n8n scenario or a Buffer/Metricool automation. The pipeline sends
`{account, platform, caption, video_url}` and the scheduler tool does the
posting, which avoids the TikTok audit and Meta app review.

Flip `DRY_RUN=false` only once accounts are connected.

## Deploy (Railway)

1. Push this repo to GitHub and create a Railway service from it (uses the Dockerfile).
2. Add a volume mounted at `/data`.
3. Set variables from `.env.example` (`DATA_DIR=/data`, `PUBLIC_BASE_URL` = the Railway URL,
   `REVIEW_PASSWORD`, API keys).
4. The one service runs the review app plus a background worker that ingests
   every `INGEST_EVERY_HOURS` and checks the schedule every `TICK_SECONDS`.

Whisper `small` runs fine on CPU for 30s clips; use `base` on a tiny instance.

## Tests

```bash
pytest -q
```
