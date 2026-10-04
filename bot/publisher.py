"""Publishes approved items one by one, with a gap between posts and a daily cap."""
import asyncio
import logging
import random
import time
from pathlib import Path
from typing import Awaitable, Callable

from . import media_utils
from .config import Config
from .db import DB
from .ig import IGService, describe_error

log = logging.getLogger(__name__)


class Publisher:
    def __init__(self, ig: IGService, db: DB, notify: Callable[..., Awaitable]):
        self.ig = ig
        self.db = db
        self.notify = notify
        self.queue: asyncio.Queue[int] = asyncio.Queue()
        self._next_allowed = 0.0

    def schedule(self, item_id: int) -> None:
        self.db.set_status(item_id, "scheduled")
        self.queue.put_nowait(item_id)

    def pending(self) -> int:
        return self.queue.qsize()

    async def run_forever(self) -> None:
        while True:
            item_id = await self.queue.get()
            item = self.db.item(item_id)
            if not item or item["status"] != "scheduled":
                continue
            if self.db.published_last_24h() >= Config.DAILY_PUBLISH_LIMIT:
                self.db.set_status(item_id, "queued")
                await self.notify(
                    f"⏸ سقف {Config.DAILY_PUBLISH_LIMIT} انتشار در ۲۴ ساعت پر شده. "
                    f"آیتم #{item_id} برگشت توی صف؛ بعداً با /publish منتشرش کن.")
                continue
            # keep a gap between posts so the account behaves like a person
            wait = self._next_allowed - time.monotonic()
            if wait > 0:
                await self.notify(f"⏳ آیتم #{item_id} حدود {int(wait // 60) + 1} دقیقه دیگه منتشر می‌شه.")
                await asyncio.sleep(wait)
            if await self._publish(item):
                self._next_allowed = time.monotonic() + Config.PUBLISH_GAP_SECONDS + random.randint(0, 60)

    async def _publish(self, item) -> bool:
        item_id = item["id"]
        path = Path(item["file_path"])
        if not path.exists():
            self.db.set_status(item_id, "failed", error="file missing")
            await self.notify(f"❌ فایل آیتم #{item_id} پیدا نشد.")
            return False
        self.db.set_status(item_id, "publishing")
        caption = self.db.caption().replace("{source}", item["source_label"] or "")
        try:
            if item["media_type"] == "video":
                thumb = await asyncio.to_thread(media_utils.thumbnail, path)
                thumb = thumb if thumb.exists() else None
                if item["kind"] == "story":
                    media = await self.ig.run(lambda c: c.video_upload_to_story(path, thumbnail=thumb))
                else:
                    media = await self.ig.run(lambda c: c.clip_upload(path, caption, thumbnail=thumb))
            else:
                if item["kind"] == "story":
                    media = await self.ig.run(lambda c: c.photo_upload_to_story(path))
                else:
                    media = await self.ig.run(lambda c: c.photo_upload(path, caption))
        except Exception as exc:
            log.exception("Publish failed for item %s", item_id)
            err = describe_error(exc)
            self.db.set_status(item_id, "failed", error=err)
            await self.notify(f"❌ انتشار #{item_id} ناموفق بود:\n{err}", retry_item=item_id)
            return False

        code = getattr(media, "code", None)
        self.db.set_status(item_id, "published", ig_code=code)
        media_utils.delete_media(path)
        if item["kind"] == "story":
            await self.notify(f"✅ استوری #{item_id} منتشر شد.")
        else:
            link = f"\nhttps://www.instagram.com/reel/{code}/" if code else ""
            await self.notify(f"✅ ریلز #{item_id} منتشر شد.{link}")
        return True
