"""Instagram session (instagrapi): login, saved session, 2FA codes through Telegram.

instagrapi is synchronous and not thread-safe, so every call goes through
`IGService.run`, which serialises calls with a lock and runs them in a thread.
"""
import asyncio
import logging
import queue
import time
from typing import Any, Awaitable, Callable

import pyotp
from instagrapi import Client
from instagrapi.exceptions import (
    BadPassword,
    ChallengeRequired,
    ClientThrottledError,
    ProxyAddressIsBlocked,
    RateLimitError,
    SentryBlock,
    FeedbackRequired,
    LoginRequired,
    PleaseWaitFewMinutes,
    TwoFactorRequired,
)

from .config import Config
from .db import DB

log = logging.getLogger(__name__)

CODE_WAIT_SECONDS = 600
LOGIN_COOLDOWN_SECONDS = 45 * 60  # after Instagram throttles a login, wait this long
THROTTLE_ERRORS = (ClientThrottledError, RateLimitError, PleaseWaitFewMinutes,
                   ProxyAddressIsBlocked, SentryBlock)
DEVICE_KEYS = ("uuids", "device_settings", "user_agent", "mid")


class CodeTimeout(Exception):
    pass


class IGService:
    def __init__(self, db: DB, notify: Callable[[str], Awaitable[Any]]):
        self.db = db
        self.notify = notify
        self.client: Client | None = None
        self.logged_in = False
        self.lock = asyncio.Lock()
        self._codes: "queue.Queue[str]" = queue.Queue()
        self.awaiting_code = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self.last_error: str | None = None

    def attach_loop(self) -> None:
        self._loop = asyncio.get_running_loop()

    # ---------- code entry (Telegram /code) ----------
    def submit_code(self, code: str) -> bool:
        if not self.awaiting_code:
            return False
        self._codes.put(code.strip().replace(" ", ""))
        return True

    def _notify_sync(self, text: str) -> None:
        if self._loop:
            asyncio.run_coroutine_threadsafe(self.notify(text), self._loop)

    def _ask_code(self, why: str) -> str:
        while not self._codes.empty():
            self._codes.get_nowait()
        self.awaiting_code = True
        self._notify_sync(
            f"🔐 اینستاگرام برای ورود کد تأیید می‌خواد ({why}).\n"
            f"کد رو این‌طوری بفرست:\n/code 123456\n"
            f"(تا {CODE_WAIT_SECONDS // 60} دقیقه منتظر می‌مونم)"
        )
        try:
            return self._codes.get(timeout=CODE_WAIT_SECONDS)
        except queue.Empty:
            raise CodeTimeout("کد تأیید به موقع نرسید. با /login دوباره امتحان کن.")
        finally:
            self.awaiting_code = False

    def _two_factor_code(self) -> str:
        if Config.IG_TOTP_SECRET:
            return pyotp.TOTP(Config.IG_TOTP_SECRET).now()
        return self._ask_code("ورود دومرحله‌ای — کد Google Authenticator")

    # ---------- login ----------
    def _make_client(self, settings: dict | None) -> Client:
        client = Client(
            settings=settings,
            proxy=Config.IG_PROXY,
            delay_range=[2, 6],
            override_app_version=True,
            timezone_offset=12600,
            timezone_name="Asia/Tehran",
        )
        client.challenge_code_handler = lambda username, choice: self._ask_code(
            f"چالش امنیتی — کدی که به {getattr(choice, 'name', choice)} فرستاده شد"
        )
        return client

    def _login_with(self, client: Client) -> None:
        try:
            client.login(Config.IG_USERNAME, Config.IG_PASSWORD)
        except TwoFactorRequired:
            client.login(Config.IG_USERNAME, Config.IG_PASSWORD,
                         verification_code=self._two_factor_code())

    def _login_sync(self) -> None:
        saved = self.db.get("ig_session")
        client = self._make_client(saved)
        try:
            self._login_with(client)
        except (BadPassword, CodeTimeout, ChallengeRequired, TwoFactorRequired) + THROTTLE_ERRORS:
            raise
        except Exception as exc:  # stale saved session → fresh login, same device identity
            if not saved:
                raise
            log.warning("Saved session failed (%s); logging in again", exc)
            device_only = {k: saved[k] for k in DEVICE_KEYS if k in saved}
            client = self._make_client(device_only)
            self._login_with(client)
        self.client = client
        self.logged_in = True
        self.last_error = None
        self.db.set("ig_session", client.get_settings())
        log.info("Instagram login OK as %s", Config.IG_USERNAME)

    async def login(self) -> bool:
        async with self.lock:
            return await self._login_locked()

    def cooldown_left(self) -> int:
        """Seconds until another login attempt is allowed (survives restarts)."""
        return max(0, int(self.db.get("ig_login_cooldown_until", 0) - time.time()))

    async def _login_locked(self) -> bool:
        wait = self.cooldown_left()
        if wait:
            self.last_error = f"اینستاگرام موقتاً ورود رو محدود کرده؛ {wait // 60 + 1} دقیقه دیگه دوباره امتحان می‌کنم."
            await self.notify(f"⏳ {self.last_error}")
            return False
        try:
            await asyncio.to_thread(self._login_sync)
            await self.notify("✅ ورود به اینستاگرام انجام شد.")
            return True
        except Exception as exc:
            self.logged_in = False
            self.last_error = describe_error(exc)
            log.exception("Instagram login failed")
            if isinstance(exc, THROTTLE_ERRORS):
                # retrying right away makes the block longer, so back off
                self.db.set("ig_login_cooldown_until", time.time() + LOGIN_COOLDOWN_SECONDS)
            await self.notify(f"❌ ورود به اینستاگرام ناموفق بود:\n{self.last_error}")
            return False

    # ---------- run any instagrapi call ----------
    async def run(self, fn: Callable[..., Any], *args, **kwargs) -> Any:
        async with self.lock:
            if not self.logged_in and not await self._login_locked():
                raise RuntimeError("به اینستاگرام وصل نیستم. اول /login رو بزن.")
            try:
                result = await asyncio.to_thread(fn, self.client, *args, **kwargs)
            except LoginRequired:
                self.logged_in = False
                if not await self._login_locked():
                    raise
                result = await asyncio.to_thread(fn, self.client, *args, **kwargs)
            self.db.set("ig_session", self.client.get_settings())
            return result


def describe_error(exc: Exception) -> str:
    if isinstance(exc, BadPassword):
        return "رمز اشتباهه (یا اینستاگرام موقتاً ورود رو بسته)."
    if isinstance(exc, THROTTLE_ERRORS):
        return ("اینستاگرام درخواست‌های این سرور رو محدود کرده (Throttled / 429). "
                f"ربات {LOGIN_COOLDOWN_SECONDS // 60} دقیقه صبر می‌کنه و بعد دوباره امتحان می‌کنه. "
                "اگه تکرار شد، باید IG_PROXY (پراکسی residential) تنظیم بشه.")
    if isinstance(exc, FeedbackRequired):
        return "اینستاگرام این کار رو موقتاً محدود کرده (Feedback). چند ساعت دست نگه دار."
    if isinstance(exc, ChallengeRequired):
        return "اینستاگرام تأیید امنیتی خواسته. اپ اینستاگرام رو باز کن، «This was me» رو بزن و بعد /login."
    if isinstance(exc, CodeTimeout):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"[:600]
