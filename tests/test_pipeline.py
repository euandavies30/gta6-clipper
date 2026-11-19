from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

from clipper import captions, scheduler
from clipper.config import Account, Settings, load_app_config
from clipper.db import DB
from clipper.posters import get_poster
from clipper.posters.tiktok import TikTokPoster

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "t.db")


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DRY_RUN", "true")
    s = Settings()
    s.ensure_dirs()
    return s


def acc(name="tt_a", platform="tiktok", n=4):
    return Account(name=name, platform=platform, poster="dryrun", posts_per_day=n,
                   active_hours=(9, 23), timezone="Europe/London", min_gap_minutes=60)


def add_clips(db, n, status="approved"):
    sid = db.add_source("inbox", "x.mp4", title="t", local_path="/x.mp4")
    return [db.add_clip(sid, 0, 20, 10 - i, f"/clips/c{i}.mp4", "", "cap", status) for i in range(n)]


def test_shipped_config_loads():
    app = load_app_config(ROOT / "config" / "accounts.yaml")
    assert {a.platform for a in app.accounts} == {"tiktok", "instagram"}
    assert all(3 <= a.posts_per_day <= 6 for a in app.accounts)


def test_slots_inside_window_and_stable():
    a = acc(n=5)
    day = datetime(2026, 11, 18, tzinfo=timezone.utc)
    slots = scheduler.daily_slots(a, day)
    assert len(slots) == 5
    assert all(9 <= s.hour < 23 for s in slots)
    assert slots == scheduler.daily_slots(a, day)
    assert slots == sorted(slots)


def test_due_respects_slots_and_gap(db):
    a = acc(n=4)
    slots = scheduler.daily_slots(a, datetime(2026, 11, 18))
    before = slots[0] - timedelta(minutes=1)
    assert not scheduler.is_due(a, db, before.astimezone(timezone.utc))
    after = slots[0] + timedelta(minutes=1)
    assert scheduler.is_due(a, db, after.astimezone(timezone.utc))


def test_tick_posts_once_per_slot_and_no_duplicates_on_platform(db, settings):
    from clipper.config import AppConfig, SourceConfig
    add_clips(db, 5)
    a1, a2 = acc("tt_a"), acc("tt_b")
    app = AppConfig(accounts=[a1, a2], sources=SourceConfig(), caption_templates=["x"])
    late = datetime(2026, 11, 18, 23, 30, tzinfo=timezone.utc)
    with mock.patch("clipper.db.now_iso", return_value=late.isoformat()):
        ev = scheduler.tick(settings, app, db, late)
        assert len(ev) == 2
        # second tick in the same instant: min gap blocks
        assert scheduler.tick(settings, app, db, late) == []
    posted = [p["clip_id"] for p in db.recent_posts()]
    assert len(posted) == len(set(posted)), "same clip went to two TikTok accounts"


def test_clip_can_go_to_each_platform_once(db):
    add_clips(db, 1)
    db.record_post(1, "tt_a", "tiktok", "ok")
    assert db.next_clip_for("tt_b", "tiktok") is None
    assert db.next_clip_for("ig_a", "instagram")["id"] == 1


def test_pending_clips_are_never_posted(db):
    add_clips(db, 2, status="pending")
    assert db.next_clip_for("tt_a", "tiktok") is None


def test_captions_and_hashtags(tmp_path):
    chunks = captions.chunk_words([{"start": 0, "end": .3, "word": "a"}, {"start": .4, "end": .6, "word": "b"},
                                   {"start": .7, "end": .9, "word": "c"}, {"start": 1.0, "end": 1.2, "word": "d"}])
    assert [c["text"] for c in chunks] == ["a b c", "d"]
    ass = captions.build_ass(chunks, "hook {x}", 10, tmp_path / "s.ass").read_text()
    assert "HOOK (X)" in ass and "A B C" in ass
    cap = captions.post_caption("{title}\n{credit}", "Trailer", None)
    assert cap == "Trailer"
    assert captions.with_hashtags("hi", ["gta6", "#fyp"]) == "hi\n\n#gta6 #fyp"


def test_dry_run_forces_dryrun_poster(settings):
    a = acc()
    a.poster = "tiktok"
    assert get_poster(a, settings).name == "dryrun"


def test_tiktok_direct_post_flow(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "k")
    monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "s")
    monkeypatch.setenv("TT_RT", "refresh")
    a = acc()
    a.poster, a.env = "tiktok", {"refresh_token": "TT_RT"}
    video = tmp_path / "v.mp4"
    video.write_bytes(b"0" * 1000)

    def fake_post(url, **kw):
        r = mock.Mock(status_code=200)
        if url.endswith("/oauth/token/"):
            r.json.return_value = {"access_token": "AT", "refresh_token": "RT2", "expires_in": 86400}
        elif "creator_info" in url:
            r.json.return_value = {"data": {"privacy_level_options": ["SELF_ONLY"]}, "error": {"code": "ok"}}
        elif "video/init" in url:
            assert kw["json"]["post_info"]["privacy_level"] == "SELF_ONLY"
            assert kw["json"]["source_info"]["video_size"] == 1000
            r.json.return_value = {"data": {"publish_id": "P1", "upload_url": "https://up"}, "error": {"code": "ok"}}
        elif "status/fetch" in url:
            r.json.return_value = {"data": {"status": "PUBLISH_COMPLETE"}, "error": {"code": "ok"}}
        return r

    put = mock.Mock(return_value=mock.Mock(status_code=201))
    with mock.patch("clipper.posters.tiktok.requests.post", side_effect=fake_post), \
         mock.patch("clipper.posters.tiktok.requests.put", put):
        res = TikTokPoster(a, settings).post(video, "caption")
    assert res.remote_id == "P1"
    assert put.call_args.kwargs["headers"]["Content-Range"] == "bytes 0-999/1000"


def test_rockstar_media_parsing(db):
    from clipper.sources import rockstar
    html = ('x "https://media-rockstargames-com.akamaized.net/VI/downloads/videos/GTAVI_Launch_Trailer/GTAVI_Launch_Trailer.mp4" '
            '"https://media-rockstargames-com.akamaized.net/VI/downloads/videos/GTAVI_Launch_Trailer/GTAVI_Launch_Trailer.mp4" '
            '"https://media-rockstargames-com.akamaized.net/VI/downloads/videos/GTAVI_Official_Cover_Art_Landscape/GTAVI_Official_Cover_Art_Landscape.mp4"')
    vids = rockstar.list_videos(html)
    assert [v["title"] for v in vids] == ["GTA 6 Launch Trailer", "GTA 6 Official Cover Art Landscape"]
    with mock.patch.object(rockstar, "list_videos", return_value=vids):
        assert rockstar.discover(db, 5) == 1


def test_direct_video_links_are_streamed_not_downloaded(db, settings):
    from clipper import pipeline
    url = "https://media-rockstargames-com.akamaized.net/VI/downloads/videos/GTAVI_Launch_Trailer/GTAVI_Launch_Trailer.mp4"
    assert pipeline.is_direct_video(url) and not pipeline.is_direct_video("https://youtu.be/abc")
    sid = db.add_source("rockstar", "GTAVI_Launch_Trailer", title="t", url=url)
    with mock.patch("clipper.sources.youtube.download") as dl:
        pipeline.download_new(settings, db)
    dl.assert_not_called()
    assert db.get_source(sid)["local_path"] == url


def test_publish_page_and_options(settings, db, monkeypatch, tmp_path):
    monkeypatch.setenv("REVIEW_PASSWORD", "pw")
    from fastapi.testclient import TestClient
    from clipper.config import AppConfig, SourceConfig
    from clipper.review import app as review
    monkeypatch.setattr(review, "db", db)
    add_clips(db, 1)
    c = TestClient(review.app)
    r = c.get("/clips/1/publish?account=tt_main", auth=("admin", "pw"))
    assert r.status_code == 200 and "Send to TikTok drafts" in r.text  # shipped config uses inbox mode
    monkeypatch.setattr(review, "load_app_config", lambda *_: AppConfig(
        accounts=[acc("tt_main")], sources=SourceConfig(), caption_templates=["x"]))
    r = c.get("/clips/1/publish?account=tt_main", auth=("admin", "pw"))
    assert "Who can view this video" in r.text and "Music Usage Confirmation" in r.text
    assert '<option value="" selected disabled>' in r.text
    r = c.post("/clips/1/publish", auth=("admin", "pw"), follow_redirects=False,
               data={"account": "tt_main", "caption": "hi", "privacy": "SELF_ONLY", "allow_comment": "true"})
    assert r.status_code == 303


def test_tiktok_inbox_mode_uses_draft_endpoint(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "k")
    monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "s")
    monkeypatch.setenv("TT_RT", "refresh")
    a = acc()
    a.poster, a.env, a.mode = "tiktok", {"refresh_token": "TT_RT"}, "inbox"
    video = tmp_path / "v.mp4"
    video.write_bytes(b"0" * 10)
    calls = []

    def fake_post(url, **kw):
        calls.append(url)
        r = mock.Mock(status_code=200)
        if url.endswith("/oauth/token/"):
            r.json.return_value = {"access_token": "AT", "expires_in": 86400}
        elif "inbox/video/init" in url:
            r.json.return_value = {"data": {"publish_id": "P2", "upload_url": "https://up"}, "error": {"code": "ok"}}
        elif "status/fetch" in url:
            r.json.return_value = {"data": {"status": "SEND_TO_USER_INBOX"}, "error": {"code": "ok"}}
        return r

    with mock.patch("clipper.posters.tiktok.requests.post", side_effect=fake_post), \
         mock.patch("clipper.posters.tiktok.requests.put", return_value=mock.Mock(status_code=201)):
        res = TikTokPoster(a, settings).post(video, "c")
    assert res.remote_id == "P2" and "inbox" in res.note
    assert not any("creator_info" in u for u in calls)


def test_moment_start_times():
    from clipper.sources import youtube as yt
    assert yt.parse_time("1:02:03") == 3723 and yt.parse_time("62:03") == 3723
    assert yt.parse_time("1h2m3s") == 3723 and yt.parse_time("2m") == 120 and yt.parse_time("90") == 90
    assert yt.parse_time("") is None
    assert yt.start_from_url("https://www.twitch.tv/videos/123?t=1h2m3s") == 3723
    assert yt.start_from_url("https://youtu.be/abc?t=95") == 95
    assert yt.start_from_url("https://www.twitch.tv/videos/123") is None
    with pytest.raises(ValueError):
        yt.parse_time("soon")
    assert captions.clean_hook("Jynxzi crashes 😭  into a tower") == "Jynxzi crashes into a tower"


def test_moment_link_downloads_only_the_window_and_makes_one_clip(settings, monkeypatch, tmp_path):
    import subprocess, threading
    from fastapi.testclient import TestClient
    from clipper import pipeline
    from clipper.review import app as review

    vid = tmp_path / "moment.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30:duration=4",
                    "-f", "lavfi", "-i", "sine=duration=4", "-shortest", str(vid)], check=True)
    seen = {}

    def fake_download(url, out_dir, video_id, cookies=None, section=None):
        seen["section"] = section
        return vid
    monkeypatch.setattr("clipper.sources.youtube.download", fake_download)
    monkeypatch.setattr(review.threading, "Thread",
                        lambda target, **kw: type("T", (), {"start": staticmethod(target)})())
    monkeypatch.setattr(pipeline.captions, "transcribe", lambda *a, **k: [])
    monkeypatch.setattr(pipeline, "free_raw", lambda *a, **k: None)
    db = review.db
    client = TestClient(review.app)
    r = client.post("/add-url", data={"url": "https://www.twitch.tv/videos/42?t=1h0m10s",
                                      "title": "Jynxzi forgets there's no autosave 😭", "credit": "Jynxzi"},
                    auth=("admin", review.settings.review_password or ""), follow_redirects=False)
    assert r.status_code == 303, r.text
    assert seen["section"] == (3610.0, 3675.0)
    with db.conn() as c:
        clips = c.execute("SELECT * FROM clips").fetchall()
    assert len(clips) == 1 and clips[0]["status"] == "pending"
    assert "Follow for daily GTA 6 streamer clips" in clips[0]["caption"]


def test_twitch_clip_maps_into_its_vod():
    from clipper.sources import twitch
    assert twitch.is_clip("https://www.twitch.tv/jynxzi/clip/FastBeautifulTigerBrokeBack-720YxwNhOm5O")
    assert twitch.is_clip("https://clips.twitch.tv/FastBeautifulTigerBrokeBack-720YxwNhOm5O")
    assert not twitch.is_clip("https://www.twitch.tv/videos/2932568279")
    # stream started at t=1000, 40s clip made at t=1000+3600 -> clip is 3560..3600 in the VOD
    start = twitch.window(4600, 40, 1000, 65)
    assert 3530 < start < 3560 and start + 65 > 3600

    infos = {
        "clip": {"timestamp": 4600, "duration": 40, "channel": "Jynxzi", "title": "aligator"},
        "list": {"entries": [{"url": "https://www.twitch.tv/videos/2"}, {"url": "https://www.twitch.tv/videos/1"}]},
        "https://www.twitch.tv/videos/2": {"timestamp": 9000, "duration": 100},
        "https://www.twitch.tv/videos/1": {"timestamp": 1000, "duration": 8000},
    }

    class FakeYDL:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def extract_info(self, url, download=False, process=False):
            return infos["clip"] if "/clip/" in url else infos["list"] if "/videos?" in url else infos[url]

    with mock.patch("yt_dlp.YoutubeDL", FakeYDL):
        m = twitch.find_moment("https://www.twitch.tv/jynxzi/clip/abc", 65)
    assert m.vod_url.endswith("/videos/1") and m.channel == "Jynxzi" and m.title == "aligator"
    assert m.start == start


def test_twitch_clip_link_becomes_vod_window(settings, monkeypatch, tmp_path):
    import subprocess
    from fastapi.testclient import TestClient
    from clipper import pipeline
    from clipper.review import app as review
    from clipper.sources import twitch

    vid = tmp_path / "m.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30:duration=3",
                    "-f", "lavfi", "-i", "sine=duration=3", "-shortest", str(vid)], check=True)
    seen = {}

    def fake_download(url, out_dir, video_id, cookies=None, section=None):
        seen.update(url=url, section=section)
        return vid
    monkeypatch.setattr("clipper.sources.youtube.download", fake_download)
    monkeypatch.setattr(twitch, "find_moment",
                        lambda url, length: twitch.Moment("https://www.twitch.tv/videos/9", 100.0, "aligator", "Jynxzi"))
    monkeypatch.setattr(review.threading, "Thread",
                        lambda target, **kw: type("T", (), {"start": staticmethod(target)})())
    monkeypatch.setattr(pipeline.captions, "transcribe", lambda *a, **k: [])
    monkeypatch.setattr(pipeline, "free_raw", lambda *a, **k: None)
    r = TestClient(review.app).post("/add-url", data={"url": "https://clips.twitch.tv/SomeClip-abc"},
                                    auth=("admin", review.settings.review_password or ""), follow_redirects=False)
    assert r.status_code == 303
    assert seen == {"url": "https://www.twitch.tv/videos/9", "section": (100.0, 165.0)}
    with review.db.conn() as c:
        src = c.execute("SELECT * FROM sources ORDER BY id DESC LIMIT 1").fetchone()
    assert src["title"] == "aligator" and src["credit"] == "Jynxzi"
