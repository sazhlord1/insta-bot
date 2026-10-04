"""Collect new media: Instagram posts/stories of source pages, YouTube channels,
and reels you send to the destination page's DMs."""
import asyncio
import json
import logging
import random
import re
from pathlib import Path
from typing import Optional

import yt_dlp

from . import media_utils
from .config import Config
from .db import DB
from .ig import IGService

log = logging.getLogger(__name__)

IG_LINK_RE = re.compile(r"instagram\.com/(?:[\w.]+/)?(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)")
IG_DEEPLINK_RE = re.compile(r"media\?id=(\d+)")
YT_COOKIE_FILE = Config.DATA_DIR / "yt_cookies.txt"

if Config.YT_COOKIES.strip():
    YT_COOKIE_FILE.write_text(Config.YT_COOKIES.replace("\\n", "\n"))


class Report:
    """Collects human-readable notes about a fetch run."""

    def __init__(self):
        self.lines: list[str] = []
        self.new_items: list[int] = []

    def add(self, text: str) -> None:
        self.lines.append(text)


async def _pause() -> None:
    # small random pause between sources so traffic looks less robotic
    await asyncio.sleep(random.uniform(4, 10))


# ======================================================================
# Instagram sources
# ======================================================================
async def _ig_user_id(ig: IGService, db: DB, src) -> str:
    if src["ig_user_id"]:
        return src["ig_user_id"]
    uid = await ig.run(lambda c: c.user_id_from_username(src["handle"]))
    db.set_source_ig_id(src["id"], str(uid))
    return str(uid)


def _download_video(c, url: str, name: str) -> Path:
    return Path(c.video_download_by_url(url, filename=name, folder=Config.MEDIA_DIR))


async def fetch_ig_posts(ig: IGService, db: DB, limit: int, report: Report) -> None:
    for src in db.sources("instagram"):
        handle = src["handle"]
        try:
            uid = await _ig_user_id(ig, db, src)
            medias = await ig.run(lambda c: c.user_medias_v1(uid, 12))
            videos = sorted((m for m in medias if m.media_type == 2 and m.video_url),
                            key=lambda m: m.taken_at, reverse=True)
            fresh = [m for m in videos if not db.is_seen("ig_post", m.pk)][:limit]
            if not fresh:
                report.add(f"• @{handle}: ویدیوی جدیدی نبود")
            for m in fresh:
                if m.video_duration and m.video_duration > Config.MAX_VIDEO_SECONDS:
                    db.mark_seen("ig_post", m.pk)
                    report.add(f"• @{handle}: یه ویدیو {int(m.video_duration)} ثانیه‌ای رد شد (طولانیه)")
                    continue
                path = await ig.run(_download_video, str(m.video_url), f"ig_{m.pk}")
                path = await asyncio.to_thread(media_utils.ensure_instagram_video, path)
                item_id = db.add_item("reel", "source", f"@{handle}", m.pk, str(path), "video", "review")
                db.mark_seen("ig_post", m.pk)
                report.new_items.append(item_id)
        except Exception as exc:
            log.exception("IG posts failed for %s", handle)
            report.add(f"• @{handle}: خطا — {str(exc)[:200]}")
        await _pause()


async def fetch_ig_stories(ig: IGService, db: DB, report: Report) -> None:
    for src in db.sources("instagram"):
        handle = src["handle"]
        try:
            uid = await _ig_user_id(ig, db, src)
            stories = await ig.run(lambda c: c.user_stories(uid))
            fresh = [s for s in stories if not db.is_seen("ig_story", s.pk)]
            if not fresh:
                report.add(f"• @{handle}: استوری جدیدی نبود")
            for s in sorted(fresh, key=lambda s: s.taken_at):
                is_video = s.media_type == 2 and s.video_url
                url = str(s.video_url if is_video else s.thumbnail_url)
                path = Path(await ig.run(
                    lambda c: c.story_download_by_url(url, filename=f"story_{s.pk}", folder=Config.MEDIA_DIR)))
                if is_video:
                    path = await asyncio.to_thread(media_utils.ensure_instagram_video, path)
                item_id = db.add_item("story", "source", f"@{handle}", s.pk, str(path),
                                      "video" if is_video else "photo", "review")
                db.mark_seen("ig_story", s.pk)
                report.new_items.append(item_id)
        except Exception as exc:
            log.exception("IG stories failed for %s", handle)
            report.add(f"• @{handle}: خطا — {str(exc)[:200]}")
        await _pause()


# ======================================================================
# YouTube (yt-dlp)
# ======================================================================
def youtube_url(handle: str) -> str:
    """'@name' or bare channel url → its Shorts tab (Reels-sized videos)."""
    h = handle.strip()
    if not h.startswith("http"):
        return f"https://www.youtube.com/@{h.lstrip('@')}/shorts"
    if re.search(r"youtube\.com/(@[^/]+|channel/[^/]+|c/[^/]+)/?$", h):
        return h.rstrip("/") + "/shorts"
    return h


def _ydl_opts(**extra) -> dict:
    opts = {"quiet": True, "no_warnings": True, "noprogress": True}
    if YT_COOKIE_FILE.exists():
        opts["cookiefile"] = str(YT_COOKIE_FILE)
    if Config.YT_PROXY:
        opts["proxy"] = Config.YT_PROXY
    opts.update(extra)
    return opts


def _yt_list(url: str, n: int) -> list[str]:
    with yt_dlp.YoutubeDL(_ydl_opts(extract_flat="in_playlist", playlistend=n)) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = info.get("entries") or [info]
    return [e["id"] for e in entries
            if e and e.get("id") and e.get("live_status") not in ("is_live", "is_upcoming")]


def _yt_download(url: str, name: str) -> tuple[Optional[Path], float]:
    """Returns (path, duration). path is None when the video is too long."""
    opts = _ydl_opts(
        format="bv*[vcodec^=avc1][height<=1920]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
        merge_output_format="mp4",
        outtmpl=str(Config.MEDIA_DIR / f"{name}.%(ext)s"),
        noplaylist=True,
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        duration = float(info.get("duration") or 0)
        if duration > Config.MAX_VIDEO_SECONDS:
            return None, duration
        info = ydl.extract_info(url, download=True)
        files = info.get("requested_downloads") or []
        path = Path(files[0]["filepath"]) if files else Path(ydl.prepare_filename(info))
    return path, duration


async def fetch_youtube(db: DB, limit: int, report: Report) -> None:
    for src in db.sources("youtube"):
        label = src["handle"]
        try:
            ids = await asyncio.to_thread(_yt_list, youtube_url(label), limit * 3)
            fresh = [v for v in ids if not db.is_seen("yt", v)][:limit]
            if not fresh:
                report.add(f"• {label}: ویدیوی جدیدی نبود")
            for vid in fresh:
                path, duration = await asyncio.to_thread(
                    _yt_download, f"https://www.youtube.com/watch?v={vid}", f"yt_{vid}")
                db.mark_seen("yt", vid)
                if path is None:
                    report.add(f"• {label}: ویدیوی {int(duration)} ثانیه‌ای رد شد (طولانیه)")
                    continue
                path = await asyncio.to_thread(media_utils.ensure_instagram_video, path)
                item_id = db.add_item("reel", "source", label, vid, str(path), "video", "review", platform="youtube")
                report.new_items.append(item_id)
        except Exception as exc:
            log.exception("YouTube failed for %s", label)
            msg = str(exc)
            if "Sign in to confirm" in msg or "bot" in msg.lower():
                msg = "یوتیوب سرور رو بلاک کرده؛ YT_COOKIES یا YT_PROXY رو تنظیم کن."
            report.add(f"• {label}: خطا — {msg[:200]}")
        await _pause()


# ======================================================================
# DM queue: reels you send from your personal account
# ======================================================================
def _dm_candidates(m) -> tuple[Optional[str], Optional[str]]:
    """Return (direct_video_url, media_pk_or_code) found in a direct message."""
    for media in (m.clip, m.media_share):
        if media is not None and media.media_type == 2:
            return (str(media.video_url) if media.video_url else None), str(media.pk)
    if m.media is not None and m.media.media_type == 2 and m.media.video_url:
        return str(m.media.video_url), str(m.media.id)

    blobs = [m.text or ""]
    if m.xma_share:
        blobs.append(m.xma_share.video_url)
    for x in m.generic_xma or []:
        blobs.append(x.video_url)
    if m.link:
        blobs.append(m.link.link_context.link_url)
    if m.raw_xma:
        blobs.append(json.dumps(m.raw_xma))
    joined = " ".join(b for b in blobs if b)
    if hit := IG_LINK_RE.search(joined):
        return None, "code:" + hit.group(1)
    if hit := IG_DEEPLINK_RE.search(joined):
        return None, hit.group(1)
    return None, None


def _dm_download(c, m) -> Optional[tuple[Path, str]]:
    """Returns (file, media_pk) for a reel/video in the message, else None."""
    url, ref = _dm_candidates(m)
    if url is None and ref is None:
        return None
    if url is None:
        pk = c.media_pk_from_code(ref[5:]) if ref.startswith("code:") else ref
        info = c.media_info(pk)
        if info.media_type != 2 or not info.video_url:
            return None
        url, ref = str(info.video_url), str(info.pk)
    safe = re.sub(r"[^\w]", "_", ref)
    return Path(c.video_download_by_url(url, filename=f"dm_{safe}", folder=Config.MEDIA_DIR)), ref


async def poll_dm(ig: IGService, db: DB) -> list[int]:
    """Check DMs from DM_SENDER_USERNAME; new reels go straight to the queue."""
    if not Config.DM_SENDER_USERNAME:
        return []
    sender = db.get("dm_sender_pk")
    if not sender or db.get("dm_sender_name") != Config.DM_SENDER_USERNAME:
        sender = str(await ig.run(lambda c: c.user_id_from_username(Config.DM_SENDER_USERNAME)))
        db.set("dm_sender_pk", sender)
        db.set("dm_sender_name", Config.DM_SENDER_USERNAME)
        db.set("dm_baseline_done", False)

    def _collect(c):
        threads = list(c.direct_threads(amount=20))
        try:
            threads += list(c.direct_pending_inbox(amount=10))
        except Exception:  # pending inbox is optional
            pass
        mine = [t for t in threads if len(t.users) == 1 and str(t.users[0].pk) == sender]
        msgs = []
        for t in mine:
            msgs += c.direct_messages(t.id, amount=20)
        return msgs

    messages = await ig.run(_collect)
    baseline_done = db.get("dm_baseline_done", False)
    new_ids: list[int] = []
    for m in sorted(messages, key=lambda x: x.timestamp):
        if str(m.user_id) != sender or db.is_seen("dm_msg", m.id):
            continue
        db.mark_seen("dm_msg", m.id)
        if not baseline_done:
            continue  # first run: ignore old history, only count new messages
        try:
            got = await ig.run(_dm_download, m)
        except Exception as exc:
            log.exception("DM download failed")
            await ig.notify(f"⚠️ یه پیام دایرکت دریافت شد ولی دانلودش نشد: {str(exc)[:200]}")
            continue
        if got is None:
            continue  # plain text / photo etc.
        path, media_pk = got
        path = await asyncio.to_thread(media_utils.ensure_instagram_video, path)
        new_ids.append(db.add_item("reel", "dm", f"@{Config.DM_SENDER_USERNAME}", media_pk,
                                   str(path), "video", "queued"))
    if not baseline_done:
        db.set("dm_baseline_done", True)
        await ig.notify(f"👀 دایرکت‌های @{Config.DM_SENDER_USERNAME} زیر نظره. "
                        "از این به بعد هر ریلزی بفرستی می‌ره توی صف.")
    return new_ids


# ======================================================================
# Re-download (hosts like Render free wipe files on every restart)
# ======================================================================
async def ensure_file(ig: IGService, db: DB, item) -> Path:
    """Return the item's media file, downloading it again if it was wiped."""
    path = Path(item["file_path"])
    if path.exists():
        return path
    log.info("File for item %s is missing; downloading again", item["id"])
    ext_id = item["ext_id"]
    if item["platform"] == "youtube":
        path, _ = await asyncio.to_thread(
            _yt_download, f"https://www.youtube.com/watch?v={ext_id}", f"yt_{ext_id}")
        if path is None:
            raise RuntimeError("ویدیو یوتیوب دیگه در دسترس نیست.")
    elif item["kind"] == "story":
        def _story(c):
            s = c.story_info(ext_id)
            is_video = s.media_type == 2 and s.video_url
            url = str(s.video_url if is_video else s.thumbnail_url)
            return Path(c.story_download_by_url(url, filename=f"story_{ext_id}", folder=Config.MEDIA_DIR))
        path = await ig.run(_story)
    else:
        def _reel(c):
            info = c.media_info(ext_id)
            if not info.video_url:
                raise RuntimeError("ویدیو دیگه در دسترس نیست.")
            return _download_video(c, str(info.video_url), f"ig_{ext_id}")
        path = await ig.run(_reel)
    if item["media_type"] == "video":
        path = await asyncio.to_thread(media_utils.ensure_instagram_video, path)
    db.set_file(item["id"], str(path))
    return path
