"""Telegram bot: the control panel for the whole pipeline."""
import asyncio
import logging
import re
import time
from pathlib import Path

from telegram import (BotCommand, InlineKeyboardButton, InlineKeyboardMarkup,
                      KeyboardButton, ReplyKeyboardMarkup, Update)
from telegram.constants import ParseMode
from telegram.ext import (Application, ApplicationBuilder, CallbackQueryHandler,
                          CommandHandler, ContextTypes, MessageHandler, filters)

from . import collectors, media_utils
from .config import Config
from .db import DB
from .ig import IGService
from .publisher import Publisher

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

TG_UPLOAD_LIMIT = 49 * 1024 * 1024
REVIEW_EXPIRY_SECONDS = 3 * 86400

# ---------- reply keyboard ----------
BTN_POSTS = "📥 دریافت پست‌ها"
BTN_STORIES = "📸 دریافت استوری‌ها"
BTN_QUEUE = "📋 صف انتشار"
BTN_PUBLISH_ALL = "🚀 انتشار کل صف"
BTN_DM = "📨 چک دایرکت"
BTN_STATUS = "📊 وضعیت"
BTN_SOURCES = "📚 پیج‌ها"
BTN_HELP = "❓ راهنما"
KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton(BTN_POSTS), KeyboardButton(BTN_STORIES)],
     [KeyboardButton(BTN_QUEUE), KeyboardButton(BTN_PUBLISH_ALL)],
     [KeyboardButton(BTN_DM), KeyboardButton(BTN_STATUS)],
     [KeyboardButton(BTN_SOURCES), KeyboardButton(BTN_HELP)]],
    resize_keyboard=True,
)

HELP = """🤖 <b>راهنمای ربات</b>

<b>پیج‌های مبدأ</b>
/add_ig username — افزودن پیج اینستاگرام (چندتا هم با فاصله)
/add_yt @channel — افزودن کانال یوتیوب (پیش‌فرض بخش Shorts؛ برای ویدیوهای عادی لینک کامل با /videos بده)
/sources — لیست پیج‌ها
/remove 3 — حذف پیج شماره ۳

<b>کپشن</b>
/caption — دیدن کپشن فعلی
/caption متن کپشن — تنظیم کپشن ثابت (می‌تونه چندخطی باشه؛ {source} جای اسم پیج مبدأ می‌شینه)

<b>دریافت و انتشار</b>
/fetch_posts — دانلود ویدیوهای جدید و فرستادن برای تأیید (مثلاً /fetch_posts 5)
/fetch_stories — دانلود استوری‌های جدید و فرستادن برای تأیید
/queue — دیدن صف انتشار
/publish 12 — انتشار آیتم ۱۲ — /publish all برای کل صف

<b>دایرکت</b>
ریلزی که از اکانت شخصیت به دایرکت پیج بفرستی خودش می‌ره توی صف.
/check_dm — چک فوری دایرکت — /dm off یا /dm on — خاموش/روشن کردن چک خودکار

<b>اکانت</b>
/status — وضعیت — /login — ورود دوباره — /code 123456 — فرستادن کد تأیید"""


class BotApp:
    def __init__(self):
        self.db = DB(Config.DB_PATH)
        self.app: Application = (
            ApplicationBuilder()
            .token(Config.TELEGRAM_BOT_TOKEN)
            .read_timeout(60).write_timeout(120).media_write_timeout(300)
            .concurrent_updates(True)
            .post_init(self.post_init)
            .build()
        )
        self.ig = IGService(self.db, self.notify)
        self.publisher = Publisher(self.ig, self.db, self.notify)
        self.busy: str | None = None
        self._last_dm_error = 0.0
        self._register()

    # ======================================================================
    # messaging helpers
    # ======================================================================
    async def notify(self, text: str, retry_item: int | None = None) -> None:
        markup = None
        if retry_item:
            markup = InlineKeyboardMarkup([[InlineKeyboardButton(
                "🔁 تلاش دوباره", callback_data=f"act:pub:{retry_item}")]])
        try:
            await self.app.bot.send_message(Config.ADMIN_TELEGRAM_ID, text, reply_markup=markup,
                                            disable_web_page_preview=True)
        except Exception:
            log.exception("notify failed")

    def _buttons(self, item) -> InlineKeyboardMarkup | None:
        i = item["id"]
        if item["status"] == "review" and item["kind"] == "reel":
            rows = [[InlineKeyboardButton("✅ انتشار", callback_data=f"act:pub:{i}"),
                     InlineKeyboardButton("📥 به صف", callback_data=f"act:queue:{i}"),
                     InlineKeyboardButton("❌ رد", callback_data=f"act:rej:{i}")]]
        elif item["status"] == "review":
            rows = [[InlineKeyboardButton("✅ استوری کن", callback_data=f"act:pub:{i}"),
                     InlineKeyboardButton("❌ رد", callback_data=f"act:rej:{i}")]]
        elif item["status"] in ("queued", "failed"):
            rows = [[InlineKeyboardButton("🚀 انتشار الان", callback_data=f"act:pub:{i}"),
                     InlineKeyboardButton("🗑 حذف", callback_data=f"act:rej:{i}")]]
        else:
            return None
        return InlineKeyboardMarkup(rows)

    def _label(self, item) -> str:
        kind = "استوری" if item["kind"] == "story" else "ریلز"
        origin = "از دایرکت" if item["origin"] == "dm" else "از"
        state = {"review": "منتظر تأیید", "queued": "توی صف", "failed": "ناموفق",
                 "scheduled": "در نوبت انتشار", "publishing": "در حال انتشار",
                 "published": "منتشر شد", "rejected": "رد شد"}.get(item["status"], item["status"])
        return f"#{item['id']} • {kind} {origin} {item['source_label']} • {state}"

    async def send_preview(self, item_id: int) -> None:
        item = self.db.item(item_id)
        if not item:
            return
        path = Path(item["file_path"])
        caption, markup = self._label(item), self._buttons(item)
        bot, chat = self.app.bot, Config.ADMIN_TELEGRAM_ID
        try:
            if item["media_type"] == "photo":
                with path.open("rb") as f:
                    await bot.send_photo(chat, f, caption=caption, reply_markup=markup)
            elif path.stat().st_size <= TG_UPLOAD_LIMIT:
                with path.open("rb") as f:
                    await bot.send_video(chat, f, caption=caption, reply_markup=markup,
                                         supports_streaming=True)
            else:
                thumb = await asyncio.to_thread(media_utils.thumbnail, path)
                with thumb.open("rb") as f:
                    await bot.send_photo(chat, f, caption=caption + "\n(فایل برای تلگرام بزرگه؛ این فقط تصویرشه)",
                                         reply_markup=markup)
        except Exception as exc:
            log.exception("preview failed")
            await bot.send_message(chat, f"{caption}\n(پیش‌نمایش ارسال نشد: {str(exc)[:150]})",
                                   reply_markup=markup)

    # ======================================================================
    # handlers
    # ======================================================================
    def _register(self) -> None:
        admin = filters.User(user_id=Config.ADMIN_TELEGRAM_ID)
        cmds = {
            "start": self.cmd_start, "help": self.cmd_start,
            "add_ig": self.cmd_add_ig, "add_yt": self.cmd_add_yt,
            "sources": self.cmd_sources, "remove": self.cmd_remove,
            "caption": self.cmd_caption,
            "fetch_posts": self.cmd_fetch_posts, "fetch_stories": self.cmd_fetch_stories,
            "queue": self.cmd_queue, "publish": self.cmd_publish,
            "check_dm": self.cmd_check_dm, "dm": self.cmd_dm,
            "status": self.cmd_status, "login": self.cmd_login, "code": self.cmd_code,
        }
        for name, fn in cmds.items():
            self.app.add_handler(CommandHandler(name, fn, filters=admin))
        self.app.add_handler(MessageHandler(admin & filters.TEXT & ~filters.COMMAND, self.on_button))
        self.app.add_handler(CallbackQueryHandler(self.on_callback, pattern=r"^act:"))

    async def post_init(self, app: Application) -> None:
        self.ig.attach_loop()
        restored = self.db.reset_interrupted()
        app.create_task(self.publisher.run_forever())
        await app.bot.set_my_commands([
            BotCommand("fetch_posts", "دریافت ویدیوهای جدید"),
            BotCommand("fetch_stories", "دریافت استوری‌های جدید"),
            BotCommand("queue", "صف انتشار"),
            BotCommand("publish", "انتشار (شماره یا all)"),
            BotCommand("sources", "لیست پیج‌ها"),
            BotCommand("caption", "دیدن/تنظیم کپشن"),
            BotCommand("check_dm", "چک دایرکت"),
            BotCommand("status", "وضعیت"),
            BotCommand("help", "راهنما"),
        ])
        app.job_queue.run_repeating(self.dm_job, interval=Config.DM_POLL_MINUTES * 60, first=120)
        app.job_queue.run_repeating(self.cleanup_job, interval=6 * 3600, first=600)
        note = f"\n({restored} آیتمِ نیمه‌کاره برگشت توی صف)" if restored else ""
        await self.notify(f"🤖 ربات روشن شد. دارم وارد اینستاگرام می‌شم...{note}")
        app.create_task(self.ig.login())

    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(HELP, parse_mode=ParseMode.HTML, reply_markup=KEYBOARD)

    async def on_button(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        text = update.message.text.strip()
        ctx.args = []
        routes = {BTN_POSTS: self.cmd_fetch_posts, BTN_STORIES: self.cmd_fetch_stories,
                  BTN_QUEUE: self.cmd_queue, BTN_DM: self.cmd_check_dm, BTN_STATUS: self.cmd_status,
                  BTN_SOURCES: self.cmd_sources, BTN_HELP: self.cmd_start}
        if text == BTN_PUBLISH_ALL:
            ctx.args = ["all"]
            return await self.cmd_publish(update, ctx)
        if text in routes:
            return await routes[text](update, ctx)
        if re.fullmatch(r"\d{6,8}", text) and self.ig.awaiting_code:
            ctx.args = [text]
            return await self.cmd_code(update, ctx)
        await update.message.reply_text("دستور رو نشناختم. /help رو بزن.", reply_markup=KEYBOARD)

    # ---------- sources ----------
    async def cmd_add_ig(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not ctx.args:
            return await update.message.reply_text("مثال: /add_ig natgeo nasa")
        added = []
        for raw in ctx.args:
            m = re.search(r"instagram\.com/([\w.]+)", raw)
            name = (m.group(1) if m else raw).strip("@/ ").lower()
            if name and self.db.add_source("instagram", name):
                added.append("@" + name)
        await update.message.reply_text(
            f"✅ اضافه شد: {' '.join(added)}" if added else "چیزی اضافه نشد (شاید تکراری بود).")

    async def cmd_add_yt(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not ctx.args:
            return await update.message.reply_text(
                "مثال: /add_yt @MrBeast\nیا لینک کامل: /add_yt https://www.youtube.com/@MrBeast/videos")
        handle = ctx.args[0].strip()
        ok = self.db.add_source("youtube", handle)
        await update.message.reply_text(
            f"✅ اضافه شد: {handle}\n(از این آدرس دانلود می‌کنم: {collectors.youtube_url(handle)})"
            if ok else "تکراری بود.", disable_web_page_preview=True)

    async def cmd_sources(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        rows = self.db.sources()
        if not rows:
            return await update.message.reply_text("هنوز پیجی اضافه نکردی. /add_ig یا /add_yt")
        lines = [f"{r['id']}. {'📷' if r['platform'] == 'instagram' else '▶️'} "
                 f"{'@' if r['platform'] == 'instagram' else ''}{r['handle']}" for r in rows]
        await update.message.reply_text("📚 پیج‌ها:\n" + "\n".join(lines) + "\n\nحذف: /remove شماره",
                                        disable_web_page_preview=True)

    async def cmd_remove(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not ctx.args or not ctx.args[0].isdigit():
            return await update.message.reply_text("مثال: /remove 3")
        ok = self.db.remove_source(int(ctx.args[0]))
        await update.message.reply_text("🗑 حذف شد." if ok else "همچین شماره‌ای نیست.")

    async def cmd_caption(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        parts = update.message.text.split(maxsplit=1)
        if len(parts) < 2:
            return await update.message.reply_text(f"📝 کپشن فعلی:\n\n{self.db.caption()}")
        self.db.set("caption", parts[1].strip())
        await update.message.reply_text("✅ کپشن ذخیره شد.")

    # ---------- fetching ----------
    async def _run_fetch(self, label: str, job) -> None:
        self.busy = label
        try:
            report = collectors.Report()
            await job(report)
            for item_id in report.new_items:
                await self.send_preview(item_id)
            summary = f"🏁 {label} تموم شد: {len(report.new_items)} مورد جدید برای تأیید."
            if report.lines:
                summary += "\n\n" + "\n".join(report.lines)
            await self.notify(summary)
        except Exception as exc:
            log.exception("fetch failed")
            await self.notify(f"❌ {label} با خطا متوقف شد: {str(exc)[:300]}")
        finally:
            self.busy = None

    def _start_fetch(self, update: Update, label: str, job) -> bool:
        if self.busy:
            asyncio.create_task(update.message.reply_text(f"⏳ صبر کن، «{self.busy}» هنوز در حال اجراست."))
            return False
        self.app.create_task(self._run_fetch(label, job))
        return True

    async def cmd_fetch_posts(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        limit = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else Config.MAX_ITEMS_PER_SOURCE
        limit = max(1, min(limit, 10))

        async def job(report):
            await collectors.fetch_ig_posts(self.ig, self.db, limit, report)
            await collectors.fetch_youtube(self.db, limit, report)

        if self._start_fetch(update, "دریافت پست‌ها", job):
            await update.message.reply_text(
                f"📥 شروع شد (از هر پیج حداکثر {limit} ویدیو). هرکدوم آماده بشه برات می‌فرستم.")

    async def cmd_fetch_stories(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        async def job(report):
            await collectors.fetch_ig_stories(self.ig, self.db, report)

        if self._start_fetch(update, "دریافت استوری‌ها", job):
            await update.message.reply_text("📸 دارم استوری‌ها رو می‌گیرم...")

    # ---------- queue & publish ----------
    async def cmd_queue(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        items = self.db.items_by_status("queued", "scheduled", "publishing", "failed")
        if not items:
            return await update.message.reply_text("📋 صف خالیه.")
        lines = [self._label(i) for i in items]
        await update.message.reply_text(
            "📋 صف:\n" + "\n".join(lines) + "\n\nانتشار یکی: /publish شماره — همه: /publish all")

    async def cmd_publish(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        arg = ctx.args[0].lower() if ctx.args else ""
        if arg == "all":
            items = self.db.items_by_status("queued")
            for it in items:
                self.publisher.schedule(it["id"])
            return await update.message.reply_text(
                f"🚀 {len(items)} آیتم رفت توی نوبت انتشار (با فاصله‌ی حدود "
                f"{Config.PUBLISH_GAP_SECONDS // 60} دقیقه بین هرکدوم)." if items else "صف خالیه.")
        if not arg.isdigit():
            return await update.message.reply_text("مثال: /publish 12 یا /publish all")
        item = self.db.item(int(arg))
        if not item or item["status"] not in ("queued", "review", "failed"):
            return await update.message.reply_text("این آیتم توی صف نیست.")
        self.publisher.schedule(item["id"])
        await update.message.reply_text(f"🚀 #{item['id']} رفت توی نوبت انتشار.")

    async def on_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q.from_user.id != Config.ADMIN_TELEGRAM_ID:
            return await q.answer("اجازه نداری.", show_alert=True)
        _, action, raw_id = q.data.split(":")
        item = self.db.item(int(raw_id))
        if not item:
            return await q.answer("پیدا نشد.")
        if action == "pub" and item["status"] in ("review", "queued", "failed"):
            self.publisher.schedule(item["id"])
            note = "🚀 رفت توی نوبت انتشار"
        elif action == "queue" and item["status"] == "review":
            self.db.set_status(item["id"], "queued")
            note = "📥 رفت توی صف (با /publish منتشرش کن)"
        elif action == "rej" and item["status"] in ("review", "queued", "failed"):
            self.db.set_status(item["id"], "rejected")
            media_utils.delete_media(item["file_path"])
            note = "❌ رد شد"
        else:
            return await q.answer("این آیتم قبلاً تعیین تکلیف شده.")
        await q.answer(note)
        try:
            if q.message.caption is not None:
                await q.edit_message_caption(caption=f"{q.message.caption}\n{note}", reply_markup=None)
            else:
                await q.edit_message_text(f"{q.message.text}\n{note}", reply_markup=None)
        except Exception:
            pass

    # ---------- DM ----------
    async def _dm_check(self) -> int:
        new_ids = await collectors.poll_dm(self.ig, self.db)
        for item_id in new_ids:
            await self.send_preview(item_id)
        return len(new_ids)

    async def dm_job(self, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not Config.DM_SENDER_USERNAME or not self.db.get("dm_enabled", True) or not self.ig.logged_in:
            return
        try:
            await self._dm_check()
        except Exception as exc:
            log.exception("DM poll failed")
            if time.time() - self._last_dm_error > 3600:  # at most one alert per hour
                self._last_dm_error = time.time()
                await self.notify(f"⚠️ چک دایرکت خطا داد: {str(exc)[:200]}")

    async def cmd_check_dm(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not Config.DM_SENDER_USERNAME:
            return await update.message.reply_text("متغیر DM_SENDER_USERNAME توی Railway تنظیم نشده.")
        await update.message.reply_text("📨 دارم دایرکت رو چک می‌کنم...")
        try:
            n = await self._dm_check()
            await update.message.reply_text(f"✅ {n} ریلز جدید به صف اضافه شد." if n else "چیز جدیدی نبود.")
        except Exception as exc:
            await update.message.reply_text(f"❌ خطا: {str(exc)[:300]}")

    async def cmd_dm(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        arg = ctx.args[0].lower() if ctx.args else ""
        if arg in ("on", "off"):
            self.db.set("dm_enabled", arg == "on")
        state = "روشن" if self.db.get("dm_enabled", True) else "خاموش"
        await update.message.reply_text(
            f"چک خودکار دایرکت: {state} (هر {Config.DM_POLL_MINUTES} دقیقه)\nتغییر: /dm on یا /dm off")

    # ---------- account ----------
    async def cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        s = self.db.stats()
        login = "✅ وصل" if self.ig.logged_in else f"❌ قطع ({self.ig.last_error or 'هنوز وارد نشده'})"
        text = (f"📊 وضعیت\n"
                f"اینستاگرام (@{Config.IG_USERNAME}): {login}\n"
                f"منتظر تأیید: {s.get('review', 0)} | توی صف: {s.get('queued', 0)} | "
                f"در نوبت: {s.get('scheduled', 0) + s.get('publishing', 0)} | ناموفق: {s.get('failed', 0)}\n"
                f"منتشرشده در ۲۴ ساعت اخیر: {self.db.published_last_24h()} از {Config.DAILY_PUBLISH_LIMIT}\n"
                f"دایرکت: {'@' + Config.DM_SENDER_USERNAME if Config.DM_SENDER_USERNAME else 'تنظیم نشده'}"
                f" ({'روشن' if self.db.get('dm_enabled', True) else 'خاموش'})\n"
                f"کار در حال اجرا: {self.busy or 'هیچی'}")
        await update.message.reply_text(text, reply_markup=KEYBOARD)

    async def cmd_login(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text("🔐 دارم دوباره وارد می‌شم...")
        self.ig.logged_in = False
        self.app.create_task(self.ig.login())

    async def cmd_code(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        code = "".join(ctx.args or [])
        if not code.isdigit():
            return await update.message.reply_text("مثال: /code 123456")
        ok = self.ig.submit_code(code)
        await update.message.reply_text("👍 کد رو فرستادم." if ok else "الان منتظر کدی نیستم.")
        try:
            await update.message.delete()  # don't leave the code lying in the chat
        except Exception:
            pass

    # ---------- housekeeping ----------
    async def cleanup_job(self, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        cutoff = time.time() - REVIEW_EXPIRY_SECONDS
        expired = [i for i in self.db.items_by_status("review") if i["created_at"] < cutoff]
        for item in expired:
            self.db.set_status(item["id"], "rejected", error="expired")
            media_utils.delete_media(item["file_path"])
        if expired:
            log.info("Expired %d unreviewed items", len(expired))

    def run(self) -> None:
        self.app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


def main() -> None:
    BotApp().run()


if __name__ == "__main__":
    main()
